import serial 
import serial.tools.list_ports
import time

import datetime
import struct
import numpy
import crc 
import binascii


# ======================== 配置区 ========================
# 串口设备名
CMD_UsedSerialName = '/dev/ttyUSB0'   # 命令串口（RS232 / TTL）
OTA_UsedSerialName = '/dev/ttyUSB1'   # OTA串口（RS485）

# 波特率配置（可根据实际硬件调整）
CMD_BAUDRATE = 9600        # CMD 命令串口波特率（发送 CMD 帧）
OTA_BAUDRATE_CMD = 3000000 # OTA 串口波特率（用于接收 CMD 的 ACK）
OTA_BAUDRATE_DATA = 115200 # OTA 串口波特率（用于发送 OTA 固件数据）

# 固件路径
INPUT_FILE_PATH = ""

# 升级类型：0x04 = FPGA+MCU APP，0x03 = MCU APP，0x02 = PBL
UPGRADE_TYPE = 0x04
# ======================== 配置区结束 ========================

pack_str = '<I' # > 大端  <小端

mcu_crc32_cfg = crc.Configuration(width=32,
                                polynomial=0x04c11db7,
                                init_value=0xffffffff,
                                final_xor_value=0x00000000,
                                reverse_input=False,
                                reverse_output=False)

mcu_crc32_fun = crc.Calculator(mcu_crc32_cfg,optimized=True)

print("************** SHOW ALL COM **************")
print("--------------------------------------------------------------------------------------------")
port_list = list(serial.tools.list_ports.comports())
for l in port_list:
    print(l)
print("--------------------------------------------------------------------------------------------")
print("************** SHOW END **************")

START_FRAME_STR = "$LDOTA,"
END_FRAME_HEX = [0xee,0xff]

UPGRADE_ALL = bytes([0x0,0x0,0x0,0x04])

GET_VERSION_ID = bytes([0x0,0x0,0x0,0x05])

UART_SEND_DATA_LEN = 1024*1

### cmd class

CMD_START_FRAME_STR = "$LDCMD,"
CMD_START_ACK_FRAME_STR = "$LDACK,"
UPGRADE_APP = 0x03
UPGRADE_PBL = 0x02
ackStartFrame = [ord(char) for char in CMD_START_ACK_FRAME_STR]

def find_sublist_indices(a, b):
    res = []
    idx = 0
    cnt = 0
    for data in b:
        if idx >= len(a):
            res=b[cnt-len(a):len(b)+1]
            print("find ! ")
            print(' 0x'.join(f'{byte:02X}' for byte in res))
            break
        if data ==a[idx]:
            idx = idx + 1
        else:
            idx = 0
        cnt = cnt + 1
    
    return res

class Jt16_cmd_serial():
    def __init__(self):
        super().__init__()
        self.startFrame = [ord(char) for char in CMD_START_FRAME_STR]
        self.endFrame = END_FRAME_HEX
        self.crc32=0
        self.checkId = [0x78,0x56,0x34,0x12]
        self.sendData = []
    def serialInit(self,serialName):
        self.serialName = serialName
        self.serialFd = serial.Serial(self.serialName, CMD_BAUDRATE, timeout = 0.1)
        self.serialName2 = OTA_UsedSerialName
        self.serialFd2 = serial.Serial(self.serialName2, OTA_BAUDRATE_CMD, timeout = 0.1)
        print ("check which port was really used >",self.serialFd.name)
    def packData(self,u8_data_list):
        u8_list = u8_data_list + self.checkId
        u8_bytes_list = bytes(u8_list)
        u32_bytes_data = []
        for i in range(0, len(u8_bytes_list), 4):
            if(i+4<(len(u8_bytes_list))):
                pass
            else:
                cnt = len(u8_bytes_list)-i
                u8_bytes_list = u8_bytes_list+bytes([0]*(4-cnt))
                print("add new list:",u8_bytes_list)
            tmp = 0
            tmp |= ((u8_bytes_list[i]<<24) | (u8_bytes_list[i+1]<<16) | (u8_bytes_list[i+2]<<8) | u8_bytes_list[i+3])
            u32_bytes_data.append(tmp)
        print(' 0x'.join(f'{byte:08X}' for byte in u32_bytes_data))
        # print(u32_bytes_data)
        crc_byte =  b''.join(struct.pack('>I', value) for value in u32_bytes_data)
        cur_crc32 = mcu_crc32_fun.checksum(crc_byte)
        cur_crc32_u8_list = [ (cur_crc32>>24)&0xff, (cur_crc32>>16)&0xff, (cur_crc32>>8)&0xff, cur_crc32&0xff]
        cur_crc32_u8_list_turn = [cur_crc32_u8_list[3],cur_crc32_u8_list[2],cur_crc32_u8_list[1],cur_crc32_u8_list[0]]
        print("cal cur crc:",hex(cur_crc32))
        self.sendData = self.startFrame + u8_data_list + self.checkId + cur_crc32_u8_list_turn +self.endFrame
        # print(self.sendData)
    def uartRcv (self):
        # hex_data = binascii.b2a_hex(rcv,bytes_per_sep=10).decode('utf-8')
        # print(int_data)
        # return int(hex_data,16)
        rx_list = []
        print("********** rx data: **********")
        while 1:
            rcv=self.serialFd2.read(1)
            int_data = int.from_bytes(rcv, byteorder='big')
            if(int_data>=0):
                rx_list.append(int_data)
            if len(rx_list)>4:
                if( rx_list[-2] == END_FRAME_HEX[0] and rx_list[-1] == END_FRAME_HEX[1] ):
                    print("get ack :")
                    print(' 0x'.join(f'{byte:02X}' for byte in rx_list))
                    break
        res = find_sublist_indices(ackStartFrame,rx_list)
        print("--------------------------- rx end -------------------------------")    
        
    def send(self):
        print("send data len:",len(self.sendData))
        print(' 0x'.join(f'{byte:02X}' for byte in self.sendData))
        self.serialFd.write(self.sendData)
        # self.uartRcv()
    def closeSerial(self):
        self.serialFd.close()
        self.serialFd2.close()





### end of cmd 

class Jt16_ota():
    def __init__(self):
        super().__init__()
        self.startFrame = [ord(char) for char in START_FRAME_STR]
        self.endFrame = [0xee,0xff]
        self.upgrade_data = UPGRADE_ALL
        self.bin_data = []
        self.uart_data = []
    def serialInit(self,serialName):
        self.serialName = serialName
        self.serialFd = serial.Serial(self.serialName, OTA_BAUDRATE_DATA, timeout = 0.1)
        print ("check which port was really used >",self.serialFd.name)
    def read_bin(self,input_path):
        with open(input_path, 'rb') as f:
            original_data = f.read()
        byte_list = list(original_data)
        u8_list = bytes(byte_list)
        # print("read Byte list in hexadecimal:")
        # print(' 0x'.join(f'{byte:02X}' for byte in u8_list))
        self.bin_data = u8_list
        # print("self.bin_data",self.bin_data)
        print("bin file len: ",hex(len(self.bin_data)))
    def set_upgrade_obj(self,id):
        self.upgrade_data = id
        if(id == GET_VERSION_ID):
            self.pack_versionId = 1
        else:
            self.pack_versionId = 0
    def uartRcv (self):
        
        # hex_data = binascii.b2a_hex(rcv,bytes_per_sep=10).decode('utf-8')
        # print(int_data)
        # return int(hex_data,16)
        
        rx_list = []
        print("********** rx data: **********")
        while 1:
            rcv=self.serialFd.read(1)
            int_data = int.from_bytes(rcv, byteorder='big')
            if(int_data>=0):
                rx_list.append(int_data)
            if len(rx_list)>4:
                if( rx_list[-2] == END_FRAME_HEX[0] and rx_list[-1] == END_FRAME_HEX[1] ):
                    print("get ack :",rx_list)
                    print(' 0x'.join(f'{byte:02X}' for byte in rx_list))
                    break
        print("----------------------------------------------------------")
        time.sleep(0.01)
    def pack_payload(self):
        self.cur_pack_id = 0
        self.cur_pack_len = 0
        bin_len = len(self.bin_data)
        self.all_pack_number = (bin_len // UART_SEND_DATA_LEN)
        if(bin_len % UART_SEND_DATA_LEN)>0:
            self.all_pack_number = self.all_pack_number + 1
        load_len = 0
        flag = 0
        print("******************** all len: %d,all pack num:%d"%(bin_len,self.all_pack_number))
        if self.pack_versionId == 1:
                self.all_pack_number = 1
        for id in range (0,self.all_pack_number):
            if( (load_len + UART_SEND_DATA_LEN) <= bin_len ):
                self.cur_pack_len = UART_SEND_DATA_LEN
            else:
                if(bin_len % UART_SEND_DATA_LEN)!= 0:
                    self.cur_pack_len = bin_len % UART_SEND_DATA_LEN
                else:
                    self.cur_pack_len = UART_SEND_DATA_LEN
                print("end of :",self.cur_pack_len," " ,hex(self.cur_pack_len))
                flag =1
            print("*********** id:",id," ",self.cur_pack_len," " ,hex(self.cur_pack_len))
            
            bin_list = self.bin_data[load_len:load_len+self.cur_pack_len]
            load_len  = load_len + self.cur_pack_len
            print("load_len",load_len)
            # if flag !=1 :
            #     continue
            
            upgrade_list = self.upgrade_data
            ### error code happened
            # if(id == 0x1f):
            #     # upgrade_list = bytes([0xa6,0xc7,0x49,0x97])
            #     self.cur_pack_len = 1024*3
            print("self.cur_pack_len [%d] :"%(id),self.cur_pack_len)
            

            
            
            print("--------------------------------------------------------------------------------------")
            # print("upgrade_list",upgrade_list)
            cmd_list = [self.all_pack_number,id,self.cur_pack_len]
            cmd_bytes_list= []
            for data  in cmd_list:
                _list = [ (data>>24)&0xff, (data>>16)&0xff,(data>>8)&0xff,data&0xff]
                cmd_bytes_list = cmd_bytes_list +_list

            hex_list = [hex(val) for val in cmd_bytes_list]
            # print("cmd_bytes_list:",hex_list)
            cmd_bytes_list = bytes(cmd_bytes_list)
            crc_data = upgrade_list + cmd_bytes_list + bin_list
            # print("crc_data:",crc_data)
            # print(' 0x'.join(f'{byte:02X}' for byte in crc_data))
            crc_data_u32 = []
            for i in range(0, len(crc_data), 4):
                tmp = 0
                tmp |= ((crc_data[i]<<24) | (crc_data[i+1]<<16) | (crc_data[i+2]<<8) | crc_data[i+3])
                crc_data_u32.append(tmp)
            crc_byte =  b''.join(struct.pack('>I', value) for value in crc_data_u32)

            # print(' 0x'.join(f'{byte:08X}' for byte in crc_byte))
            # print(crc_byte)
            cur_crc32 = mcu_crc32_fun.checksum(crc_byte)
            cur_crc32_u8_list = [ (cur_crc32>>24)&0xff, (cur_crc32>>16)&0xff, (cur_crc32>>8)&0xff, cur_crc32&0xff]
            
            
            uart_data = []
            for data in crc_data:
                uart_data.append(int(data))
            crc_list = cur_crc32_u8_list
            end_list = END_FRAME_HEX
            temp_list =  self.startFrame + uart_data + crc_list + end_list
            self.uart_data.append(temp_list)#todo maybe can save as pack bin
            print("uart [send][%d]:\n"%(id)) 
            print(' 0x'.join(f'{byte:02X}' for byte in temp_list))

            print("cal cur[%d] crc:"%(id),hex(cur_crc32))
            print(' 0x'.join(f'{byte:02X}' for byte in cur_crc32_u8_list))
            self.serialFd.write(temp_list)

            self.uartRcv()
            # break
    def closeSerial(self):
        self.serialFd.close()

print("******************* start send cmd ***************************")
print("ackStartFrame:",ackStartFrame," ",len(ackStartFrame))
print(' 0x'.join(f'{byte:02X}' for byte in ackStartFrame))
test = Jt16_cmd_serial()
test.serialInit(CMD_UsedSerialName)
# test.packData([0x01,0x00])
test.packData([0x03,0x03,0x04,0x00])
test.send()
time.sleep(0.5)
test.packData([0x03,0x02,0x05,UPGRADE_TYPE])
test.send()
test.uartRcv()

test.closeSerial()
time.sleep(3)
print("******************* end of send cmd ***************************")

upgrade_id = UPGRADE_ALL
print("---------------------- start send ota ----------------------")
if upgrade_id == UPGRADE_ALL:
    # upgradeAll_pack_fpga_version_1.00b399
    all_path = INPUT_FILE_PATH
    # all_path = "../input/upgradeAll_pack_fpga_version_1.00b399.bin"
    ota_all = Jt16_ota()
    ota_all.serialInit(OTA_UsedSerialName)
    ota_all.read_bin(all_path)
    ota_all.set_upgrade_obj(UPGRADE_ALL)
    ota_all.pack_payload()
    # print("ota_app.startFrame:")
    # print(' 0x'.join(f'{byte:02X}' for byte in ota_app.startFrame))
    ota_all.closeSerial()
    pass
print("---------------------- end of send ota ----------------------")
