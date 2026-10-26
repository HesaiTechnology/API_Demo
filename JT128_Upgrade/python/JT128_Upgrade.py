#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone JT128 upgrade utility (no swap).

Usage: python JT128_Upgrade.py <package.patch> [ip] [port] [netcard]
Check installation without connecting: python JT128_Upgrade.py --check
Keep the supplied lib directory next to this script. Python standard library only.
Exit 0 means the upload and reboot command were acknowledged; it does not verify
which firmware is running after reboot.
"""

import argparse
import copy
import ctypes
import datetime
import os
import platform
import socket
import struct
import sys
import time


VERSION = 'jt128_upgrade_2.0.0'
PTC_COMMAND_GET_INVENTORY_INFO = 0x07
PTC_COMMAND_REBOOT = 0x10
PTC_COMMAND_FOTA_REQUEST_UPGRADE = 0x83
IP = '192.168.1.201'
PORT = 9347
NETCARD = 'default'

# -------------------------------- 辅助功能 --------------------------------
def castBytes2HexStrs(data, sep=''):
    return sep.join('{:02x}'.format(value) for value in data)


def printInfo(text, overLine=False, newLine=True, isPure=False):
    prefix = '' if isPure else '\033[32m{}[INFO] \033[0m'.format(
        datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print(prefix + str(text), end='\r' if overLine else ('\n' if newLine else ''), flush=True)


def printError(text, overLine=False, newLine=True, isPure=False):
    prefix = '' if isPure else '\033[31m{}[ERROR] \033[0m'.format(
        datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print(prefix + str(text), end='\r' if overLine else ('\n' if newLine else ''), flush=True)


def _makePtcCrcTable():
    table = []
    for value in range(256):
        crc = value << 24
        for _ in range(8):
            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
        table.append(crc)
    return tuple(table)


_PTC_CRC_TABLE = _makePtcCrcTable()


def calCrc32(data):
    """PTC 块 CRC：MSB-first / init=FFFFFFFF / 无 xorout，不能用 zlib 代替。"""
    crc = 0xFFFFFFFF
    for value in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _PTC_CRC_TABLE[((crc >> 24) ^ value) & 0xFF]
    return crc


class NativeUpgrade:
    """Load the supplied native library using a versioned, Python-independent C ABI."""

    ERRORS = {
        1: 'Invalid native API input',
        2: 'Upgrade package invalid or corrupted',
        3: 'Unable to recognize the device firmware information',
        4: 'Upgrade package is not compatible with this device',
        5: 'Native output buffer is too small',
        6: 'Native upgrade preparation failed',
    }

    @staticmethod
    def platform_tag():
        system = platform.system()
        machine = platform.machine().lower()
        if struct.calcsize('P') != 8:
            raise RuntimeError('This distribution requires a 64-bit Python interpreter')
        if system == 'Windows' and machine in ('amd64', 'x86_64'):
            return 'windows-x86_64'
        if system == 'Linux':
            if machine in ('amd64', 'x86_64'):
                return 'linux-x86_64'
            if machine in ('aarch64', 'arm64'):
                return 'linux-aarch64'
        raise RuntimeError('Unsupported system/architecture: {}/{}'.format(system, machine))

    def __init__(self):
        self.tag = self.platform_tag()
        filename = 'jt128_upgrade.dll' if self.tag.startswith('windows-') else 'libjt128_upgrade.so'
        self.path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lib', self.tag, filename)
        if not os.path.isfile(self.path):
            raise RuntimeError('Native library is missing: {}. Extract the complete distribution.'.format(self.path))
        try:
            self.library = ctypes.CDLL(self.path)
            self.library.jt128_api_version.argtypes = []
            self.library.jt128_api_version.restype = ctypes.c_uint32
            if self.library.jt128_api_version() != 1:
                raise RuntimeError('Native library API version mismatch; use files from the same distribution')
            self.library.jt128_validate.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
            self.library.jt128_validate.restype = ctypes.c_int32
            self.library.jt128_prepare.argtypes = [
                ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64,
                ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64)]
            self.library.jt128_prepare.restype = ctypes.c_int32
        except (OSError, AttributeError) as error:
            raise RuntimeError('Cannot load native library {}: {}'.format(self.path, error))

    @classmethod
    def _check_status(cls, status):
        if status:
            raise ValueError(cls.ERRORS.get(status, 'Unknown native error {}'.format(status)))

    def validate(self, data):
        data = bytes(data)
        self._check_status(self.library.jt128_validate(ctypes.c_char_p(data), len(data)))

    def prepare(self, data, inventory):
        data, inventory = bytes(data), bytes(inventory)
        source, device = ctypes.c_char_p(data), ctypes.c_char_p(inventory)
        required = ctypes.c_uint64()
        status = self.library.jt128_prepare(
            source, len(data), device, len(inventory), None, 0, ctypes.byref(required))
        if status != 5:
            self._check_status(status)
            raise ValueError('Native library returned an invalid size response')
        if not 0 < required.value <= len(data):
            raise ValueError('Native library returned an invalid output size')
        output = ctypes.create_string_buffer(required.value)
        capacity = required.value
        self._check_status(self.library.jt128_prepare(
            source, len(data), device, len(inventory), output, capacity, ctypes.byref(required)))
        if not 0 < required.value <= capacity:
            raise ValueError('Native library returned an invalid output length')
        return output.raw[:required.value]


# ----------------------------- PTC 结果与传输 -----------------------------
class Result:
    MAP_ERROR = [
        'success', 'invalid input parameter', 'failure to connect to server',
        'no valid data returned', 'server does not have enough memory',
        'server does not support this command yet',
        'server fails to communicate with the inner FPGA',
        'some inner errors occur', 'cannot activate lidar because of current fault code',
    ]
    HEAD_REQLEN = HEAD_REPLEN = 8

    def __init__(self, cmd, userData=None):
        self.command = cmd
        self.userData = userData
        self.direction = False
        self.finished = False
        self.innerCode = self.errCode = 0
        self.beginTime = time.monotonic()
        self.endTime = self.beginTime
        self.reqBlock = 1024
        self.reqTotal = self.repTotal = 0
        self.curSend = self.curRecv = 0
        self.payload = b''
        self.errorMessage = ''

    @property
    def errInfo(self):
        if 0 <= self.errCode < len(self.MAP_ERROR):
            return self.MAP_ERROR[self.errCode]
        return 'unknown device error 0x{:02X}'.format(self.errCode)

    @property
    def wasteTime(self):
        return (self.endTime - self.beginTime) * 1000

    @property
    def indexOfBlock(self):
        return (self.curSend + self.reqBlock - 1) // self.reqBlock

    @property
    def totalOfBlock(self):
        return (self.reqTotal + self.reqBlock - 1) // self.reqBlock

    def isSucceed(self):
        return (self.finished and self.innerCode == 0 and self.errCode == 0
                and self.curSend == self.reqTotal and self.curRecv == self.repTotal)

    def isFailed(self):
        return self.finished and not self.isSucceed()

    def clone(self):
        return copy.copy(self)


class PtcCore:
    def __init__(self, defaultTimeout=5, printPayload=True):
        self.__clientSocket = None
        self.__defaultTimeout = defaultTimeout
        self.__printPayload = printPayload
        self.lastRet = None

    def connectLidar(self, ip, port, netcard='default', timeout=None):
        self.disconnectLidar(False)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            printInfo('connect lidar {}:{}:{} ...'.format(netcard, ip, port))
            if netcard != 'default':
                if not hasattr(socket, 'SO_BINDTODEVICE'):
                    raise OSError('Binding a named network interface requires Linux')
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, netcard.encode() + b'\0')
            sock.settimeout(self.__defaultTimeout if timeout is None else timeout)
            sock.connect((ip, port))
        except (OSError, ValueError, OverflowError) as error:
            sock.close()
            printError('connect failed: {}'.format(error))
            return False
        self.__clientSocket = sock
        printInfo('connect successfully')
        return True

    def disconnectLidar(self, normal=True):
        sock, self.__clientSocket = self.__clientSocket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            if normal:
                printInfo('disconnect lidar')

    def isConnected(self):
        return self.__clientSocket is not None

    def __recvExact(self, length, deadline):
        data = bytearray()
        while len(data) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('PTC response timeout')
            self.__clientSocket.settimeout(remaining)
            chunk = self.__clientSocket.recv(length - len(data))
            if not chunk:
                raise ConnectionError('Lidar closed the connection before a complete response')
            data.extend(chunk)
        return bytes(data)

    def __exchange(self, isExt, cmd, data, timeout=None):
        ret = Result(cmd)
        ret.reqTotal = len(data)
        try:
            if not self.isConnected():
                raise ConnectionError('Lidar is not connected')
            wire_cmd = 0xFF if isExt else cmd
            payload = struct.pack('>I', cmd) + data if isExt else data
            frame = struct.pack('>2sBBI', b'\x47\x74', wire_cmd, 0, len(payload)) + payload
            timeout = self.__defaultTimeout if timeout is None else timeout
            self.__clientSocket.settimeout(timeout)
            deadline = time.monotonic() + timeout
            if self.__printPayload:
                printInfo('send: ' + castBytes2HexStrs(frame, ' '))
            self.__clientSocket.sendall(frame)
            ret.curSend = ret.reqTotal
            ret.direction = True
            header = self.__recvExact(8, deadline)
            magic, response_cmd, ret.errCode, ret.repTotal = struct.unpack('>2sBBI', header)
            if magic != b'\x47\x74' or response_cmd != wire_cmd:
                raise ValueError('Invalid PTC response magic or command')
            if ret.repTotal > 1024 * 1024:
                raise ValueError('PTC response exceeds 1 MiB')
            ret.payload = self.__recvExact(ret.repTotal, deadline)
            ret.curRecv = len(ret.payload)
            if self.__printPayload:
                printInfo('recv: ' + castBytes2HexStrs(header + ret.payload, ' '))
            if isExt and ret.errCode == 0:
                if len(ret.payload) < 4 or struct.unpack('>I', ret.payload[:4])[0] != cmd:
                    raise ValueError('Invalid PTC response subcommand')
                ret.payload = ret.payload[4:]
        except (OSError, ValueError, struct.error) as error:
            ret.innerCode = 1
            ret.errorMessage = str(error)
            self.disconnectLidar(False)
        ret.endTime = time.monotonic()
        ret.finished = True
        return ret

    def __printResult(self, ret):
        self.lastRet = ret
        text = ('ret: {{cmd:{:08X}, innerCode:{}, errCode:{}, request:{}/{}, '
                'response:{}/{}, totalTime:{:.3f}ms}}').format(
                    ret.command, ret.innerCode, ret.errCode, ret.curSend, ret.reqTotal,
                    ret.curRecv, ret.repTotal, ret.wasteTime)
        if ret.isFailed():
            printError(text)
            printError(ret.errorMessage or ret.errInfo)
        else:
            printInfo(text)

    def hookProcBigPara(self, subData, result, addr=None):
        # Enable CRC validation for each upload block.
        header = struct.pack('>IIII', 1, result.indexOfBlock + 1,
                             result.totalOfBlock, calCrc32(subData))
        return header + (struct.pack('>I', addr) if addr is not None else b'') + subData

    def sendData1(self, isExt, cmd, data, enCrc=False, hook=None, userData=None, timeout=None):
        """Send one PTC request; upload block CRC is supplied by the send hook."""
        if hook and hook.get('send'):
            proc, arg = hook['send']
            state = Result(cmd, userData)
            state.reqTotal = len(data)
            data = proc(data, state, arg)
        ret = self.__exchange(isExt, cmd, data, timeout)
        ret.userData = userData
        if ret.isSucceed() and hook and hook.get('recv'):
            proc, arg = hook['recv']
            proc(ret.payload, ret, arg)
        self.__printResult(ret)
        return ret

    def sendFile2(self, isExt, cmd, file, enCrc=False, block=1024, hook=None,
                  userData=None, timeout=None):
        """分包发送文件路径或内存 bytes，任何一帧失败立即停止，不自动重发。"""
        ret = Result(cmd, userData)
        try:
            if not 1 <= block <= 1024:
                raise ValueError('Block size must be between 1 and 1024')
            if isinstance(file, (bytes, bytearray)):
                data = bytes(file)
            else:
                with open(file, 'rb') as stream:
                    data = stream.read()
            if not data:
                raise ValueError('Cannot upload an empty file')
            ret.reqTotal, ret.reqBlock = len(data), block
            for offset in range(0, len(data), block):
                chunk = data[offset:offset + block]
                payload = chunk
                if hook and hook.get('send'):
                    proc, arg = hook['send']
                    payload = proc(chunk, ret, arg)
                reply = self.__exchange(isExt, cmd, payload, timeout)
                ret.errCode, ret.innerCode = reply.errCode, reply.innerCode
                ret.errorMessage = reply.errorMessage
                # 每帧的响应独立处理，不能将前一帧 payload 混入后续响应。
                ret.payload, ret.repTotal, ret.curRecv = reply.payload, reply.repTotal, reply.curRecv
                if not reply.isSucceed():
                    break
                ret.curSend += len(chunk)  # 只统计已确认的文件字节
                if hook and hook.get('recv'):
                    proc, arg = hook['recv']
                    proc(ret.payload, ret, arg)
                elapsed = max(time.monotonic() - ret.beginTime, 0.000001)
                printInfo('ptc transfer: Send={}/{} --- {:.2f}%({:.2f}K/s)'.format(
                    ret.curSend, ret.reqTotal, 100 * ret.curSend / ret.reqTotal,
                    ret.curSend / elapsed / 1024), overLine=ret.curSend < ret.reqTotal)
        except (OSError, ValueError) as error:
            ret.innerCode = 1
            ret.errorMessage = str(error)
        ret.endTime = time.monotonic()
        ret.finished = True
        self.__printResult(ret)
        return ret


# ------------------------------- User Class -------------------------------
class Ptc(PtcCore):
    def getInventoryInfo(self):
        return self.sendData1(False, PTC_COMMAND_GET_INVENTORY_INFO, b'')

    def rebootLidar(self):
        return self.sendData1(False, PTC_COMMAND_REBOOT, b'')

    def uploadSoftware83(self, filepath, slotListener=None, slotUserData=None):
        """上传已经选定的文件/bytes；自动选包和重启由 main 负责。返回 Result。"""
        ret = self.sendFile2(False, PTC_COMMAND_FOTA_REQUEST_UPGRADE, filepath, False, 1024,
                             {'send': (self.hookProcBigPara, None)}, slotUserData, 60)
        if slotListener is not None:
            slotListener(ret, slotUserData)
        return ret


# ---------------------------------- main ----------------------------------
def _portNumber(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('port must be an integer')
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('port must be in 1..65535')
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Standalone JT128 upgrade (no swap).',
        epilog='Return codes: 0=uploaded/reboot acknowledged, 1=package, 2=connection/arguments, '
               '3=inventory, 4=selection, 5=upload, 6=reboot, 7=native library, 130=interrupted.')
    parser.add_argument('--check', action='store_true', help='check the installation without connecting')
    parser.add_argument('package', nargs='?', help='path to the .patch upgrade package')
    parser.add_argument('ip', nargs='?', default=IP, help='lidar IP (default: %(default)s)')
    parser.add_argument('port', nargs='?', type=_portNumber, default=PORT,
                        help='PTC TCP port (default: %(default)s)')
    parser.add_argument('netcard', nargs='?', default=NETCARD,
                        help='Linux interface name or default (default: %(default)s)')
    args = parser.parse_args(argv)
    if not args.package and not args.check:
        parser.error('package is required unless --check is used')
    printInfo(VERSION)
    try:
        native = NativeUpgrade()
    except RuntimeError as error:
        printError(str(error))
        return 7
    if args.check:
        printInfo('Native API 1 OK: {} ({})'.format(native.path, native.tag))
        return 0
    try:
        with open(args.package, 'rb') as stream:
            data = stream.read()
        native.validate(data)
    except (OSError, ValueError) as error:
        printError(str(error))
        return 1

    ptc = Ptc(defaultTimeout=10, printPayload=False)
    try:
        if not ptc.connectLidar(args.ip, args.port, args.netcard, timeout=5):
            return 2
        inventory = ptc.getInventoryInfo()
        if not inventory.isSucceed():
            printError('Unable to read the lidar firmware information')
            return 3
        try:
            selected = native.prepare(data, inventory.payload)
        except ValueError as error:
            printError(str(error))
            return 4
        printInfo('selected upload size: {} bytes'.format(len(selected)))
        if not ptc.uploadSoftware83(selected).isSucceed():
            printError('Upgrade transfer failed; stopping')
            return 5
        printInfo('Package transmitted successfully; waiting 10 seconds before reboot')
        time.sleep(10)
        if not ptc.rebootLidar().isSucceed():
            printError('Reboot not acknowledged; verify the lidar and reboot manually if needed')
            return 6
        printInfo('Transfer complete and reboot command acknowledged')
        return 0
    except KeyboardInterrupt:
        printError('Upgrade interrupted')
        return 130
    finally:
        ptc.disconnectLidar()


if __name__ == '__main__':
    sys.exit(main())
