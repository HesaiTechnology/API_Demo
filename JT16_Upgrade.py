#!/usr/bin/env python3
# JT16 CLI 整包升级（FPGA+MCU，固定 UPGRADE_TYPE=0x04）

import argparse
import struct
import sys
import time
from pathlib import Path

import crc
import serial
import serial.tools.list_ports

START_FRAME_STR = "$LDOTA,"
END_FRAME_HEX = [0xEE, 0xFF]

# 与 JT16_OTA_Upgrade.py 对齐：仅 FPGA+MCU APP
UPGRADE_TYPE = 0x04 # FPGA+MCU 联合升级
UPGRADE_ALL = bytes([0x0, 0x0, 0x0, 0x04])

CMD_START_FRAME_STR = "$LDCMD,"
CMD_START_ACK_FRAME_STR = "$LDACK,"
ackStartFrame = [ord(char) for char in CMD_START_ACK_FRAME_STR]

UART_SEND_DATA_LEN = 1024 # 每包数据大小

mcu_crc32_cfg = crc.Configuration(
    width=32,
    polynomial=0x04C11DB7,
    init_value=0xFFFFFFFF,
    final_xor_value=0x00000000,
    reverse_input=False,
    reverse_output=False,
)
mcu_crc32_fun = crc.Calculator(mcu_crc32_cfg, optimized=True)


#-------------------------------------------辅助功能函数--------------------------------------
def find_sublist_indices(a, b):
    res = []
    idx = 0
    cnt = 0
    for data in b:
        if idx >= len(a):
            res = b[cnt - len(a) : len(b) + 1]
            print("find ! ")
            print(" 0x".join(f"{byte:02X}" for byte in res))
            break
        if data == a[idx]:
            idx = idx + 1
        else:
            idx = 0
        cnt = cnt + 1

    return res


#-------------------------------------------CMD串口通信---------------------------------------
class Jt16_cmd_serial:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.startFrame = [ord(char) for char in CMD_START_FRAME_STR]
        self.endFrame = END_FRAME_HEX
        self.crc32 = 0
        self.checkId = [0x78, 0x56, 0x34, 0x12]
        self.sendData = []

    def serialInit(self, cmd_port, ota_port, cmd_baud, ota_baud_cmd):
        self.serialName = cmd_port
        self.serialFd = serial.Serial(self.serialName, cmd_baud, timeout=0.1)
        self.serialName2 = ota_port
        self.serialFd2 = serial.Serial(self.serialName2, ota_baud_cmd, timeout=0.1)
        print("check which port was really used >", self.serialFd.name)

    def packData(self, u8_data_list):
        # 添加 checkId
        u8_list = u8_data_list + self.checkId
        u8_bytes_list = bytes(u8_list)
        u32_bytes_data = []
        for i in range(0, len(u8_bytes_list), 4):
            if i + 4 < len(u8_bytes_list):
                pass
            else:
                cnt = len(u8_bytes_list) - i
                u8_bytes_list = u8_bytes_list + bytes([0] * (4 - cnt))
                if self.verbose:
                    print("add new list:", u8_bytes_list)
            tmp = 0
            tmp |= (
                (u8_bytes_list[i] << 24)
                | (u8_bytes_list[i + 1] << 16)
                | (u8_bytes_list[i + 2] << 8)
                | u8_bytes_list[i + 3]
            )
            u32_bytes_data.append(tmp)
        if self.verbose:
            print(" 0x".join(f"{byte:08X}" for byte in u32_bytes_data))
        # 计算 CRC
        crc_byte = b"".join(struct.pack(">I", value) for value in u32_bytes_data)
        cur_crc32 = mcu_crc32_fun.checksum(crc_byte)
        cur_crc32_u8_list = [
            (cur_crc32 >> 24) & 0xFF,
            (cur_crc32 >> 16) & 0xFF,
            (cur_crc32 >> 8) & 0xFF,
            cur_crc32 & 0xFF,
        ]
        cur_crc32_u8_list_turn = [
            cur_crc32_u8_list[3],
            cur_crc32_u8_list[2],
            cur_crc32_u8_list[1],
            cur_crc32_u8_list[0],
        ]
        if self.verbose:
            print("cal cur crc:", hex(cur_crc32))
        # 组装发送数据
        self.sendData = (
            self.startFrame
            + u8_data_list
            + self.checkId
            + cur_crc32_u8_list_turn
            + self.endFrame
        )

    def uartRcv(self, timeout=30.0):
        # 接收 ACK
        rx_list = []
        if self.verbose:
            print("********** rx data: **********")
        start_time = time.monotonic()
        while True:
            if time.monotonic() - start_time > timeout:
                print(f"错误: CMD ACK 接收超时 (>{timeout}s)", file=sys.stderr)
                return False
            rcv = self.serialFd2.read(1)
            if rcv:
                int_data = int.from_bytes(rcv, byteorder="big")
                rx_list.append(int_data)
            # 检查帧尾
            if len(rx_list) > 4:
                if rx_list[-2] == END_FRAME_HEX[0] and rx_list[-1] == END_FRAME_HEX[1]:
                    print("get ack :")
                    if self.verbose:
                        print(" 0x".join(f"{byte:02X}" for byte in rx_list))
                    break
        find_sublist_indices(ackStartFrame, rx_list)
        if self.verbose:
            print("--------------------------- rx end -------------------------------")
        return True

    def send(self):
        # 发送 CMD 数据
        print("send data len:", len(self.sendData))
        if self.verbose:
            print(" 0x".join(f"{byte:02X}" for byte in self.sendData))
        self.serialFd.write(self.sendData)

    def closeSerial(self):
        self.serialFd.close()
        self.serialFd2.close()


#-------------------------------------------OTA数据发送--------------------------------------
class Jt16_ota:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.startFrame = [ord(char) for char in START_FRAME_STR]
        self.endFrame = [0xEE, 0xFF]
        self.upgrade_data = UPGRADE_ALL
        self.bin_data = []
        self.uart_data = []
        self.pack_versionId = 0

    def serialInit(self, ota_port, ota_baud_data):
        self.serialName = ota_port
        self.serialFd = serial.Serial(self.serialName, ota_baud_data, timeout=0.1)
        print("check which port was really used >", self.serialFd.name)

    def read_bin(self, input_path):
        # 读取固件文件
        with open(input_path, "rb") as f:
            original_data = f.read()
        byte_list = list(original_data)
        u8_list = bytes(byte_list)
        self.bin_data = u8_list
        print("bin file len: ", hex(len(self.bin_data)))

    def set_upgrade_obj(self, upgrade_id):
        self.upgrade_data = upgrade_id
        self.pack_versionId = 0

    def uartRcv(self, timeout=30.0):
        # 接收 OTA 数据包应答
        rx_list = []
        if self.verbose:
            print("********** rx data: **********")
        start_time = time.monotonic()
        while True:
            if time.monotonic() - start_time > timeout:
                print(f"错误: OTA 数据包应答超时 (>{timeout}s)", file=sys.stderr)
                return False
            rcv = self.serialFd.read(1)
            if rcv:
                int_data = int.from_bytes(rcv, byteorder="big")
                rx_list.append(int_data)
            # 检查帧尾
            if len(rx_list) > 4:
                if rx_list[-2] == END_FRAME_HEX[0] and rx_list[-1] == END_FRAME_HEX[1]:
                    print("get ack :", rx_list if self.verbose else f"pack received ({len(rx_list)} bytes)")
                    if self.verbose:
                        print(" 0x".join(f"{byte:02X}" for byte in rx_list))
                    break
        if self.verbose:
            print("----------------------------------------------------------")
        time.sleep(0.01)
        return True

    def pack_payload(self):
        """
        分包发送 OTA 数据
        Returns:
            bool: True=发送成功, False=发送失败
        """
        self.cur_pack_id = 0
        self.cur_pack_len = 0
        bin_len = len(self.bin_data)
        # 计算分包数
        self.all_pack_number = bin_len // UART_SEND_DATA_LEN
        if bin_len % UART_SEND_DATA_LEN > 0:
            self.all_pack_number = self.all_pack_number + 1
        load_len = 0
        print(
            "******************** all len: %d,all pack num:%d"
            % (bin_len, self.all_pack_number)
        )
        for pack_id in range(0, self.all_pack_number):
            # 计算当前包长度
            if (load_len + UART_SEND_DATA_LEN) <= bin_len:
                self.cur_pack_len = UART_SEND_DATA_LEN
            else:
                if (bin_len % UART_SEND_DATA_LEN) != 0:
                    self.cur_pack_len = bin_len % UART_SEND_DATA_LEN
                else:
                    self.cur_pack_len = UART_SEND_DATA_LEN
                if self.verbose:
                    print("end of :", self.cur_pack_len, " ", hex(self.cur_pack_len))
            print(
                "*********** id:",
                pack_id,
                " ",
                self.cur_pack_len,
                " ",
                hex(self.cur_pack_len),
            )

            bin_list = self.bin_data[load_len : load_len + self.cur_pack_len]
            load_len = load_len + self.cur_pack_len
            if self.verbose:
                print("load_len", load_len)

            upgrade_list = self.upgrade_data
            if self.verbose:
                print("self.cur_pack_len [%d] :" % (pack_id), self.cur_pack_len)
                print(
                    "--------------------------------------------------------------------------------------"
                )

            # 构建包头
            cmd_list = [self.all_pack_number, pack_id, self.cur_pack_len]
            cmd_bytes_list = []
            for data in cmd_list:
                _list = [
                    (data >> 24) & 0xFF,
                    (data >> 16) & 0xFF,
                    (data >> 8) & 0xFF,
                    data & 0xFF,
                ]
                cmd_bytes_list = cmd_bytes_list + _list

            # 计算 CRC
            cmd_bytes_list = bytes(cmd_bytes_list)
            crc_data = upgrade_list + cmd_bytes_list + bin_list
            crc_data_u32 = []
            for i in range(0, len(crc_data), 4):
                tmp = 0
                tmp |= (
                    (crc_data[i] << 24)
                    | (crc_data[i + 1] << 16)
                    | (crc_data[i + 2] << 8)
                    | crc_data[i + 3]
                )
                crc_data_u32.append(tmp)
            crc_byte = b"".join(struct.pack(">I", value) for value in crc_data_u32)

            cur_crc32 = mcu_crc32_fun.checksum(crc_byte)
            cur_crc32_u8_list = [
                (cur_crc32 >> 24) & 0xFF,
                (cur_crc32 >> 16) & 0xFF,
                (cur_crc32 >> 8) & 0xFF,
                cur_crc32 & 0xFF,
            ]

            # 组装并发送数据包
            uart_data = []
            for data in crc_data:
                uart_data.append(int(data))
            crc_list = cur_crc32_u8_list
            end_list = END_FRAME_HEX
            temp_list = self.startFrame + uart_data + crc_list + end_list
            self.uart_data.append(temp_list)
            if self.verbose:
                print("uart [send][%d]:\n" % (pack_id))
                print(" 0x".join(f"{byte:02X}" for byte in temp_list))
                print("cal cur[%d] crc:" % (pack_id), hex(cur_crc32))
                print(" 0x".join(f"{byte:02X}" for byte in cur_crc32_u8_list))
            # 发送并等待应答
            self.serialFd.write(temp_list)

            if not self.uartRcv():
                print(
                    f"错误: 第 {pack_id}/{self.all_pack_number - 1} 包应答超时，升级中止",
                    file=sys.stderr,
                )
                return False

        return True

    def closeSerial(self):
        self.serialFd.close()


#-------------------------------------------CLI入口----------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="JT16 OTA FPGA+MCU full-package upgrade (CLI)")
    p.add_argument("--package", help="固件路径")
    p.add_argument("--cmd-port", help="CMD 串口")
    p.add_argument("--ota-port", help="OTA 串口")
    p.add_argument("--cmd-baud", type=int, default=9600, help="CMD 波特率 (default: 9600)")
    p.add_argument(
        "--ota-baud-cmd",
        type=int,
        default=3000000,
        help="OTA 口收 CMD ACK 波特率 (default: 3000000)",
    )
    p.add_argument(
        "--ota-baud-data",
        type=int,
        default=115200,
        help="OTA 口发固件数据波特率 (default: 115200)",
    )
    p.add_argument("--list-ports", action="store_true", help="列出串口后退出")
    p.add_argument("-v", "--verbose", action="store_true", help="打印详细包内容")
    return p


def list_serial_ports():
    for port in serial.tools.list_ports.comports():
        print(port)
    return 0


def run_upgrade(
    package,
    cmd_port,
    ota_port,
    cmd_baud,
    ota_baud_cmd,
    ota_baud_data,
    verbose=False,
) -> bool:
    """
    执行 JT16 整包升级
    Returns:
        bool: True=升级成功, False=升级失败
    """
    cmd = None
    ota = None
    try:
        # CMD 阶段：启动升级 + 设置升级类型
        print("******************* start send cmd ***************************")
        cmd = Jt16_cmd_serial(verbose=verbose)
        cmd.serialInit(cmd_port, ota_port, cmd_baud, ota_baud_cmd)
        cmd.packData([0x03, 0x03, 0x04, 0x00])
        cmd.send()
        time.sleep(0.5)
        cmd.packData([0x03, 0x02, 0x05, UPGRADE_TYPE])
        cmd.send()
        if not cmd.uartRcv():
            print("错误: CMD ACK 超时或失败，升级中止", file=sys.stderr)
            return False
        cmd.closeSerial()
        print("******************* end of send cmd ***************************")

        time.sleep(3)

        # 数据阶段：发送固件
        print("---------------------- start send ota ----------------------")
        ota = Jt16_ota(verbose=verbose)
        ota.serialInit(ota_port, ota_baud_data)
        ota.read_bin(package)
        if len(ota.bin_data) == 0:
            print(f"错误: 升级包为空: {package}", file=sys.stderr)
            return False
        ota.set_upgrade_obj(UPGRADE_ALL)
        if not ota.pack_payload():
            print("错误: OTA 数据发送失败，升级中止", file=sys.stderr)
            return False
        ota.closeSerial()
        print("---------------------- end of send ota ----------------------")
        return True
    except Exception as exc:
        print(f"升级失败: {exc}")
        return False
    finally:
        if cmd is not None:
            try:
                cmd.closeSerial()
            except Exception:
                pass
        if ota is not None:
            try:
                ota.closeSerial()
            except Exception:
                pass


#-----------------------------------main--------------------------------------------
def main() -> int:
    args = build_parser().parse_args()
    if args.list_ports:
        return list_serial_ports()
    if not args.package or not args.cmd_port or not args.ota_port:
        print("错误: 升级需要 --package --cmd-port --ota-port", file=sys.stderr)
        return 1
    if not Path(args.package).is_file():
        print(f"错误: 升级包不存在: {args.package}", file=sys.stderr)
        return 1
    try:
        ok = run_upgrade(
            args.package,
            args.cmd_port,
            args.ota_port,
            args.cmd_baud,
            args.ota_baud_cmd,
            args.ota_baud_data,
            args.verbose,
        )
        return 0 if ok else 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
