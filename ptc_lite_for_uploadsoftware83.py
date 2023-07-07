
# 剥离自LidarUtilities的ptc模块

VERSION = 'ptc_lite_3.0.10'

import datetime, socket, copy, time, sys, os, traceback


PTC_COMMAND_REBOOT                                      = 0x10 # 16
PTC_COMMAND_FOTA_REQUEST_UPGRADE                        = 0x83 # 131


#-------------------------------------------辅助功能函数--------------------------------------
def castBytes2HexStrs(data:bytes, sep:str = ''):
    arr = ['{:02x}'.format(int(i)) for i in data]
    return sep.join(arr)

def printInfo(text, overLine = False, newLine = True, isPure = False):
    dtstr = str(datetime.datetime.now())[:-7]
    prefix = '\033[32m' + dtstr + '[INFO] \033[0m'
    if isPure: prefix = ''
    print(prefix + text, end=('\r' if overLine else '\r\n'))
        
def printError(text, overLine = False, newLine = True, isPure = False):
    dtstr = str(datetime.datetime.now())[:-7]
    prefix = '\033[31m' + dtstr + '[ERROR] \033[0m'
    if isPure: prefix = ''
    print(prefix + text, end=('\r' if overLine else '\r\n'))

def calCrc32(data):
    crc_table = [
        0x00000000, 0x4c11db7, 0x9823b6e, 0xd4326d9, 0x130476dc, 0x17c56b6b, 0x1a864db2, 0x1e475005, 0x2608edb8, 0x22c9f00f, 0x2f8ad6d6, 
        0x2b4bcb61, 0x350c9b64, 0x31cd86d3, 0x3c8ea00a, 0x384fbdbd, 0x4c11db70, 0x48d0c6c7, 0x4593e01e, 0x4152fda9, 0x5f15adac, 0x5bd4b01b, 
        0x569796c2, 0x52568b75, 0x6a1936c8, 0x6ed82b7f, 0x639b0da6, 0x675a1011, 0x791d4014, 0x7ddc5da3, 0x709f7b7a, 0x745e66cd, 0x9823b6e0, 
        0x9ce2ab57, 0x91a18d8e, 0x95609039, 0x8b27c03c, 0x8fe6dd8b, 0x82a5fb52, 0x8664e6e5, 0xbe2b5b58, 0xbaea46ef, 0xb7a96036, 0xb3687d81, 
        0xad2f2d84, 0xa9ee3033, 0xa4ad16ea, 0xa06c0b5d, 0xd4326d90, 0xd0f37027, 0xddb056fe, 0xd9714b49, 0xc7361b4c, 0xc3f706fb, 0xceb42022, 
        0xca753d95, 0xf23a8028, 0xf6fb9d9f, 0xfbb8bb46, 0xff79a6f1, 0xe13ef6f4, 0xe5ffeb43, 0xe8bccd9a, 0xec7dd02d, 0x34867077, 0x30476dc0, 
        0x3d044b19, 0x39c556ae, 0x278206ab, 0x23431b1c, 0x2e003dc5, 0x2ac12072, 0x128e9dcf, 0x164f8078, 0x1b0ca6a1, 0x1fcdbb16, 0x18aeb13, 
        0x54bf6a4, 0x808d07d, 0xcc9cdca, 0x7897ab07, 0x7c56b6b0, 0x71159069, 0x75d48dde, 0x6b93dddb, 0x6f52c06c, 0x6211e6b5, 0x66d0fb02, 
        0x5e9f46bf, 0x5a5e5b08, 0x571d7dd1, 0x53dc6066, 0x4d9b3063, 0x495a2dd4, 0x44190b0d, 0x40d816ba, 0xaca5c697, 0xa864db20, 0xa527fdf9, 
        0xa1e6e04e, 0xbfa1b04b, 0xbb60adfc, 0xb6238b25, 0xb2e29692, 0x8aad2b2f, 0x8e6c3698, 0x832f1041, 0x87ee0df6, 0x99a95df3, 0x9d684044, 
        0x902b669d, 0x94ea7b2a, 0xe0b41de7, 0xe4750050, 0xe9362689, 0xedf73b3e, 0xf3b06b3b, 0xf771768c, 0xfa325055, 0xfef34de2, 0xc6bcf05f, 
        0xc27dede8, 0xcf3ecb31, 0xcbffd686, 0xd5b88683, 0xd1799b34, 0xdc3abded, 0xd8fba05a, 0x690ce0ee, 0x6dcdfd59, 0x608edb80, 0x644fc637, 
        0x7a089632, 0x7ec98b85, 0x738aad5c, 0x774bb0eb, 0x4f040d56, 0x4bc510e1, 0x46863638, 0x42472b8f, 0x5c007b8a, 0x58c1663d, 0x558240e4, 
        0x51435d53, 0x251d3b9e, 0x21dc2629, 0x2c9f00f0, 0x285e1d47, 0x36194d42, 0x32d850f5, 0x3f9b762c, 0x3b5a6b9b, 0x315d626, 0x7d4cb91, 
        0xa97ed48, 0xe56f0ff, 0x1011a0fa, 0x14d0bd4d, 0x19939b94, 0x1d528623, 0xf12f560e, 0xf5ee4bb9, 0xf8ad6d60, 0xfc6c70d7, 0xe22b20d2, 
        0xe6ea3d65, 0xeba91bbc, 0xef68060b, 0xd727bbb6, 0xd3e6a601, 0xdea580d8, 0xda649d6f, 0xc423cd6a, 0xc0e2d0dd, 0xcda1f604, 0xc960ebb3, 
        0xbd3e8d7e, 0xb9ff90c9, 0xb4bcb610, 0xb07daba7, 0xae3afba2, 0xaafbe615, 0xa7b8c0cc, 0xa379dd7b, 0x9b3660c6, 0x9ff77d71, 0x92b45ba8, 
        0x9675461f, 0x8832161a, 0x8cf30bad, 0x81b02d74, 0x857130c3, 0x5d8a9099, 0x594b8d2e, 0x5408abf7, 0x50c9b640, 0x4e8ee645, 0x4a4ffbf2, 
        0x470cdd2b, 0x43cdc09c, 0x7b827d21, 0x7f436096, 0x7200464f, 0x76c15bf8, 0x68860bfd, 0x6c47164a, 0x61043093, 0x65c52d24, 0x119b4be9, 
        0x155a565e, 0x18197087, 0x1cd86d30, 0x29f3d35, 0x65e2082, 0xb1d065b, 0xfdc1bec, 0x3793a651, 0x3352bbe6, 0x3e119d3f, 0x3ad08088, 
        0x2497d08d, 0x2056cd3a, 0x2d15ebe3, 0x29d4f654, 0xc5a92679, 0xc1683bce, 0xcc2b1d17, 0xc8ea00a0, 0xd6ad50a5, 0xd26c4d12, 0xdf2f6bcb, 
        0xdbee767c, 0xe3a1cbc1, 0xe760d676, 0xea23f0af, 0xeee2ed18, 0xf0a5bd1d, 0xf464a0aa, 0xf9278673, 0xfde69bc4, 0x89b8fd09, 0x8d79e0be, 
        0x803ac667, 0x84fbdbd0, 0x9abc8bd5, 0x9e7d9662, 0x933eb0bb, 0x97ffad0c, 0xafb010b1, 0xab710d06, 0xa6322bdf, 0xa2f33668, 0xbcb4666d, 
        0xb8757bda, 0xb5365d03, 0xb1f740b4]
    
    crc = 0xffffffff
    for i in range(len(data)):
        crc = ((crc<<8)&0xffffffff) ^ crc_table[((crc>>24)^data[i])&0xff]
        
    return crc

class Result():
    MAP_ERROR = [
        'success',                                                      # 0
        'invalid input parameter',                                      # 1
        'failure to connect to server',                                 # 2
        'no valid data returned',                                       # 3
        'server does not have enough memory',                           # 4
        'server does not support this command yet',                     # 5
        'server fails to communicate with the inner FPGA',              # 6
        'some inner errors occur',                                      # 7
        'cannot activate lidar because of current fault code',          # 8
    ]
    HEAD_REQLEN = 8
    HEAD_REPLEN = 8
    def __init__(self, cmd, userData) -> None:
        self.command = cmd
        self.direction = False # False-send, True-recv
        self.finished = False # whole transfer finish
        self.innerCode = 0
        self.beginTime = 0
        self.endTime = 0
        self.errCode = 0
        self.reqBlock = 1024 # block size of request, max is 1024
        self.reqTotal = 0 # total length of will send, exclude ptc header
        self.repTotal = 0 # total length of will recv, exclude ptc header
        self.__curSend = 0 # cursor of reqTotal, exclude ptc header
        self.__curRecv = 0 # cursor of repTotal, exclude ptc header
        self.__extraReqLen = 0 # length of exclude ptc header and read data
        self.__extraRepLen = 0 # length of exclude ptc header and read data
        self.payload = b''
        self.commCrc32 = 0x00000000
        self.userData = userData
        
    @property
    def errInfo(self): return Result.MAP_ERROR[self.errCode]
    
    @property
    def curSend(self): 
        ret = (self.__curSend - self.__extraReqLen)
        return (ret if ret >= 0 else 0)
    
    @property
    def curRecv(self): 
        ret = (self.__curRecv - self.__extraRepLen)
        return (ret if ret >= 0 else 0)
    
    @property  # ms
    def wasteTime(self): return int((self.endTime - self.beginTime) * 1000 * 1000) / 1000
    
    @property # range [0, totalOfBlock-1]
    def indexOfBlock(self):
        return ((self.curSend + self.reqBlock - 1) // self.reqBlock)
        
    @property  # 
    def totalOfBlock(self):
        return ((self.reqTotal + self.reqBlock - 1) // self.reqBlock)
    
    def isFailed(self):
        return (self.finished and (self.innerCode != 0 or self.errCode != 0 or self.curSend != self.reqTotal or self.curRecv != self.repTotal))
    
    def isSucceed(self):
        return (self.finished and (self.curSend == self.reqTotal and self.curRecv == self.repTotal) and (self.innerCode == 0 and self.errCode == 0))
    
    # correct curSend
    def subCurSend(self, extraReqLen):
        self.__curSend -= extraReqLen
        return self
    
    # correct curRecv
    def subCurRecv(self, extraRepLen):
        self.__curRecv -= extraRepLen
        return self
    
    def setInnerCode(self, code):
        self.innerCode = code
        return self
    
    def setFinished(self, finished):
        self.finished = finished
        return self
    
    def moveCurSend(self, offset):
        self.__curSend += offset
        return self
    
    def moveCurRecv(self, offset):
        self.__curRecv += offset
        return self
    
    def clone(self):
        # return copy.deepcopy(self)
        return copy.copy(self)
        

class PtcCore():
    def __init__(self, defaultTimeout = 5, printPayload = True):
        self.__clientSocket = None
        self.__Defaulttimeout = defaultTimeout
        self.__printPayload = printPayload
        self.blockSize = 1024 * 8 # send/recv 8k every unit
        self.lastRet = None
        
    def connectLidar(self, ip:str, port:int, netcard:str = 'default', timeout:int = None):
        
        if self.__clientSocket is not None:
            self.disconnectLidar(True)
                
        def __connectlidar(self, ip, port, netcard, timeout, emitSiganl):
            self.__clientSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                printInfo('connect lidar {}:{}:{} ...'.format(netcard, ip, port))
                if netcard != 'default':
                    self.__clientSocket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, netcard.encode('utf-8'))
                if timeout == None: timeout = self.__Defaulttimeout
                self.__clientSocket.settimeout(timeout)
                self.__clientSocket.connect((ip, port))
            except:
                self.__clientSocket = None
                printError('connect failed !!!')
                return False
            
            self.__clientSocket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 64)
            printInfo('connect successfully')
            return True
            
        return __connectlidar(self, ip, port, netcard, timeout, False)
                
    def disconnectLidar(self, normal:bool = True):
        if self.__clientSocket == None:
            return
        sock = self.__clientSocket
        self.__clientSocket = None
        try:sock.shutdown(socket.SHUT_RDWR)
        except:pass
        sock.close()
        if normal == True:
            printInfo('disconnect lidar')
        else:
            printError('lidar connect break !')
        
    def isConnected(self):
        return (self.__clientSocket != None)
        
    def hookProcBigPara(self, subData:bytes, result:Result, addr:int):
        crcValid = 1
        crc = calCrc32(subData)
        head = crcValid.to_bytes(4, 'big') + (result.indexOfBlock + 1).to_bytes(4, 'big') + result.totalOfBlock.to_bytes(4, 'big') + crc.to_bytes(4, 'big')
        if addr == None:
            return (head + subData)
        return (head + addr.to_bytes(4, 'big') + subData)
    
    def __build_pack(self, isExt:bool, cmd:int, data:bytes, setcrc:bool = False):
        if not isExt:
            return b'\x47\x74' + cmd.to_bytes(1, 'big') + b'\x00' + len(data).to_bytes(4, 'big') + data
        return b'\x47\x74' + b'\xFF' + b'\x00' + (len(data) + 4).to_bytes(4, 'big') + cmd.to_bytes(4, 'big') + data
    
    def __sendonce_and_receive(self, isExt:bool, cmd:int, data:bytes, enCrc:bool = False, hook:dict = None, userData:object = None, timeout = None, ret:Result = None) -> Result:
        try:
            if timeout == None: timeout = self.__Defaulttimeout
            self.__clientSocket.settimeout(timeout)
            
            # send
            ret.direction = False
            len_before = len(data)
            if hook != None and hook.get('send') != None: 
                data = hook['send'][0](data, ret, hook['send'][1])
            data = self.__build_pack(isExt, cmd, data, enCrc)
            send_total = len(data)
            ret.subCurSend(send_total - len_before)
            
            if self.__printPayload:
                printInfo('----------send----------')
                printInfo(castBytes2HexStrs(data, ' '))
                
            index = 0
            
            while index in range(0, send_total):
                end = send_total if (index + self.blockSize > send_total) else (index + self.blockSize)
                sended = self.__clientSocket.send(data[index: end])
                if sended < 0: 
                    ret.setFinished(True).setInnerCode(1)
                    raise ValueError(True)
                index += sended
                self.__slotProcess(ret.setFinished(False).moveCurSend(sended).setInnerCode(0).clone())
            
            # recv header
            recv_head = self.__clientSocket.recv(Result.HEAD_REPLEN)
            if hook and hook.get('peek') != None: hook['peek'](recv_head)
            if len(recv_head) != Result.HEAD_REPLEN:
                ret.setFinished(True).setInnerCode(2)
                raise ValueError(True)
                
            if recv_head[0:2] != b'\x47\x74':
                ret.setFinished(True).setInnerCode(3)
                raise ValueError(False)

            # recv data
            ret.direction = True
            ret.errCode = recv_head[3]
            ret.repTotal = int.from_bytes(recv_head[4:8], 'big')
            ret.subCurRecv(ret.curRecv)
            if ret.errCode != 0: 
                ret.setFinished(True).setInnerCode(4)
                raise ValueError(False)
            
            index = 0
            while index in range(0, ret.repTotal):
                recv_data = self.__clientSocket.recv(self.blockSize)
                if hook and hook.get('peek') != None: hook['peek'](recv_data)
                if recv_data == None or len(recv_data) < 1:
                    ret.setFinished(True).setInnerCode(5)
                    raise ValueError(True)
                index += len(recv_data)
                ret.payload += recv_data
                self.__slotProcess(ret.setFinished(False).moveCurRecv(len(recv_data)).setInnerCode(0).clone())
                
            if self.__printPayload:
                printInfo('----------recv----------')
                printInfo(castBytes2HexStrs(recv_head + ret.payload, ' '))
            if isExt: ret.payload = ret.payload[4:]
            if hook and hook.get('recv') != None: hook['recv'][0](ret.payload, ret, hook['recv'][1])
        except Exception as e: # timeout except
            # printError(traceback.format_exc())
            if ret.innerCode == 0: ret.setFinished(True).setInnerCode(6)
            self.__flush_socket_buffer_tcp(e.args)
            pass
        
        ret.setFinished(True)
        return ret
    
    def __senddata_and_receive(self, isExt:bool, cmd:int, data:bytes, enCrc:bool = False, hook:dict = None, userData:object = None, timeout = None) -> Result:
        ret = Result(cmd, userData)
        ret.reqTotal = len(data)
        ret.beginTime = time.time()
        self.__slotProcess(ret.subCurSend(ret.curSend).subCurRecv(ret.curRecv).clone())
        self.__sendonce_and_receive(isExt, cmd, data, enCrc, hook, userData, timeout, ret)
        ret.endTime = time.time()
        self.__slotProcess(ret.clone())
        return ret
            
    def __sendfile_and_receive_multi(self, isExt:bool, cmd:int, file:str, enCrc:bool = False, block:int = 1024, hook:dict = None, userData:object = None, timeout = None) -> Result:
        ret = Result(cmd, userData)
        file = open(file, 'rb')
        if file == None: 
            self.__slotProcess(ret.setFinished(True).setInnerCode(8).clone())
            return ret;
        ret.beginTime = time.time()
        
        ret = Result(cmd, userData)
        data = file.read() # best way is reading and sending part by part
        ret.reqTotal = len(data)
        ret.reqBlock = block
        ret.beginTime = time.time()
        self.__slotProcess(ret.subCurSend(ret.curSend).subCurRecv(ret.curRecv).clone())
        
        index = 0
        while index in range(0, ret.reqTotal):
            end = ret.reqTotal if (index + ret.reqBlock > ret.reqTotal) else (index + ret.reqBlock)
            ret = self.__sendonce_and_receive(isExt, cmd, data[index: end], enCrc, hook, userData, timeout, ret)
            if ret.innerCode != 0:
                break
            index = end
        
        ret.endTime = time.time()
        file.close()

        self.__slotProcess(ret.clone())
        return ret;
        
    def __slotProcess(self, result:Result):
        if not result.isFailed() and ((result.direction and result.repTotal > 256) or (not result.direction and result.reqTotal > 256)):
            total = result.reqTotal
            dealt = result.curSend
            if result.direction:
                total = result.repTotal
                dealt = result.curRecv
            percent = 100 * (0 if dealt == 0 else dealt/total)
            speed = (dealt/(time.time() - result.beginTime + 0.000001)) / 1024
            text = 'ptc transfer: {}={}/{} --- {:.2f}%({:.2f}K/s)'.format('Recv' if result.direction else 'Send', dealt, total, percent, speed)
            printInfo(text, int(percent) != 100)
    
    def __flush_socket_buffer_tcp(self, args):
        if self.__clientSocket == None:
            return
        if args and len(args) ==  1 and isinstance(args[0], bool) and args[0] == False:
            self.__clientSocket.settimeout(0.2)
            while True:
                try: self.__clientSocket.recv(1024)
                except: break
            return
        self.disconnectLidar(False)
        
    def __printResult(self, ret:Result):
        text = 'ret: {{cmd:{:08X}, innerCode:{}, errCode:{}, direct:{}, request:{}/{}, response:{}/{}, totalTime:{:.3f}ms}}'.format(ret.command, ret.innerCode, ret.errCode, 'Recv' if ret.direction else 'Send', ret.curSend, ret.reqTotal, ret.curRecv, ret.repTotal, ret.wasteTime)
        if ret.isFailed():
            printError(text)
            printError('-----------error detail------------')
            if ret.errCode != 0: printError(Result.MAP_ERROR[ret.errCode])
            elif ret.innerCode != 0: printError('network error !')
        else:
            printInfo(text)
        
    def sendData1(self, isExt, cmd, data, enCrc:bool = False, hook:dict = None, userData = None, timeout = None) -> Result:    
        """
        单包发送并接收响应
        Args:
            isExt (bool): 是否为扩展命令（subCmd）
            cmd (int): 命令号
            data (bytes): 数据
            enCrc (bool, optional): 是否启用CRC校验.
            hook (dict, optional): 钩子函数.
            userData ([type], optional): 用户数据.
            timeout ([type], optional): 超时时间.
        Returns:
            Result: 响应结构体
        """
        ret = self.__senddata_and_receive(isExt, cmd, data, enCrc, hook, userData, timeout)
        self.__printResult(ret)
        return ret
        
    def sendFile2(self, isExt, cmd, file, enCrc:bool = False, block:int = 1024, hook:dict = None, userData = None, timeout = None) -> Result:
        """
        分包发送并接收响应
        Args:
            block (int, optional): 每个包的大小.
            其他字段同sendFile1参数
        Returns:
            Result: 响应结构体
        """
        ret = self.__sendfile_and_receive_multi(isExt, cmd, file, enCrc, block, hook, userData, timeout)
        self.__printResult(ret)
        return ret
        
        
#-------------------------------User Class for extends------------------------------
class Ptc(PtcCore):
            
    def __init__(self, defaultTimeout = 5, printPayload = True):
        super().__init__(defaultTimeout, printPayload)
        
    def rebootLidar(self):
        cmd = PTC_COMMAND_REBOOT
        data = b''
        return self.sendData1(False, cmd, data)
    
    def uploadSoftware83(self, filepath, slotListener = None, slotUserData = None):
        cmd = PTC_COMMAND_FOTA_REQUEST_UPGRADE
        timeout = 30
        self.sendFile2(False, cmd, filepath, False, 1024, {'send':(self.hookProcBigPara, None)}, slotListener, timeout)
        
        


#-----------------------------------main--------------------------------------------
IP = "192.168.1.201"
PORT = 9347
NETCARD = "default" # vlan.id，如：en2sp.34
if __name__ == '__main__':
    
    printInfo(VERSION)
    
    file = sys.argv[1]
    if file == None or not os.path.exists(file) or os.path.isdir(file):
        printError('invalid patch file path !!!')
        exit(1)
    
    lidarPtc = Ptc(defaultTimeout = 10, printPayload = False) # 第一个参数PTC默认超时时长，第二个参数是否打印payload
    status = lidarPtc.connectLidar(ip = IP, port = PORT, netcard = NETCARD, timeout = 5)
    if not status: exit(2)
    
    lidarPtc.uploadSoftware83(file)
    lidarPtc.rebootLidar()
    time.sleep(10)
    status = lidarPtc.connectLidar(IP, PORT, NETCARD, 5)
    if not status: exit(3)
    lidarPtc.disconnectLidar()
    