#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JT32 OTA 升级全流程脚本（适配完整升级包）
- 解析 OTA_Patch.py 生成的完整升级包
- 根据MCU当前分区只发送对应的APP组件
- 支持GUI可视化操作

组件升级方式:
- 下仓APP-A/B: MCU写入本地Flash（双分区），包含完整Header
- 下仓FPGA: MCU写入本地Flash，发送时跳过Header只发送纯固件
- 上仓FPGA: MCU通过寄存器发送给上仓，特殊流程：
  1. 第一包到达时MCU启动Flash擦除（需要12秒）
  2. 擦除期间MCU返回RETRANSMIT，上位机需等待重发
  3. 擦除完成后重置page计数，开始分page(256B)传输
  4. 每page分4次64B传输到上仓FPGA
- 上仓参数: MCU通过寄存器发送给上仓（流程与上仓FPGA相同，擦除地址不同）：
  1. 第一包到达时MCU启动Flash擦除（擦除地址0x80a05008）
  2. 后续流程与上仓FPGA相同
  3. 上仓参数无Header，直接传输原始数据
"""

import serial
import serial.tools.list_ports
import struct
import time
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext, messagebox

# 默认配置
DEFAULT_BAUDRATE_232 = 9600
DEFAULT_BAUDRATE_485 = 5000000

# OTA数据包配置（与MCU Ota.h保持一致）
PACKET_DATA_SIZE = 1024  # OTA_PACKET_SIGNAL_SIZE
PACKAGE_HEADER_SIZE = 56  # 整包Header大小（扩展为56字节，支持5个组件）
COMPONENT_HEADER_SIZE = 1024  # 组件Header大小（binHeader + bootInfo + padding）
PACKAGE_MAGIC = b'HSAI'  # 与OTA_Patch.py一致

# OTA组件类型映射（与MCU COMPONENT_TYPE_T一致）
COMPONENT_TYPE_MAP = {
    'TMB_FPGA': 0,    # COMPONENT_FPGA_UP
    'BMB_FPGA': 1,    # COMPONENT_FPGA_DOWN
    'BMB_APP': 2,     # COMPONENT_MCU_DOWN
    'TMB_PARAM': 3    # COMPONENT_PARAM_UP
}

ACK_ERROR_MAP = {
    0: "无错误", 1: "需要重传", 2: "无效包", 3: "Flash写入失败",
    4: "地址计算错误", 5: "无效参数", 6: "无效组件类型",
    7: "上仓FPGA擦除失败", 8: "上仓FPGA传输失败", 9: "上仓FPGA page计数不匹配"
}

# 上仓FPGA升级特殊配置
TMB_FPGA_ERASE_WAIT_SEC = 15      # 上仓FPGA Flash擦除等待时间（MCU端12秒，多留余量）
TMB_FPGA_FIRST_PKT_TIMEOUT = 20  # 上仓FPGA第一包超时时间（包含擦除等待）
TMB_FPGA_RETRANSMIT_DELAY = 2    # 收到RETRANSMIT后等待时间（秒）

# 上仓参数文件验证配置
TMB_PARAM_MAGIC = bytes([0x4B, 0x02])  # 参数文件魔数（前2字节）
TMB_PARAM_ADDR_OFFSET = 4              # 校验值地址在文件中的偏移（第5-8字节，索引4-7）
TMB_PARAM_QUERY_CMD = bytes.fromhex("24 4C 44 43 4D 44 2C 0F 02 01 08 00 A0 80 FF FF FF FF 00 00 00 00 00 78 56 34 12 52 19 63 26 EE FF")
TMB_PARAM_CHECKSUM_ACK_OFFSET = 18     # ACK中校验值的起始偏移（第18字节开始）

def calculate_crc32_mpeg2(data):
    """计算CRC-32/MPEG-2校验值（填充到4字节对齐）"""
    crc = 0xFFFFFFFF
    actual_len = len(data)
    padded_len = ((actual_len + 3) // 4) * 4  # 向上取整到4的倍数
    
    for i in range(padded_len):
        byte = data[i] if i < actual_len else 0  # 超出部分填充0
        crc ^= byte << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = (crc << 1) ^ 0x04C11DB7
            else:
                crc <<= 1
            crc &= 0xFFFFFFFF
    return crc

def bytes_to_hex_str(data, max_len=32):
    if len(data) <= max_len:
        return ' '.join(f'{b:02X}' for b in data)
    else:
        preview = ' '.join(f'{b:02X}' for b in data[:max_len//2])
        end = ' '.join(f'{b:02X}' for b in data[-max_len//2:])
        return f"{preview} ... {end} (总长度: {len(data)})"

class OtaUpgradeFlow:
    def __init__(self, package_path, uart_232_port, uart_485_port, baudrate_232, baudrate_485, log_callback=None):
        self.package_path = Path(package_path)
        self.uart_232_port = uart_232_port
        self.uart_485_port = uart_485_port
        self.baudrate_232 = baudrate_232
        self.baudrate_485 = baudrate_485
        self.log_callback = log_callback  # GUI日志回调
        self.uart_232 = None
        self.uart_485 = None
        self.current_partition = None  # 'A' or 'B'
        self.is_running = False  # 升级状态标志
        self.progress_callback = None  # 进度回调
        self.verbose_packet = False  # 是否显示每包详细内容
        
        # 解析升级包信息
        self.package_info = self.parse_package_header()
    
    def log(self, message):
        """输出日志，支持GUI回调"""
        print(message)
        if self.log_callback:
            self.log_callback(message)
        
    def parse_package_header(self):
        """解析完整升级包的Header，获取组件信息"""
        with open(self.package_path, 'rb') as f:
            data = f.read()
            
        if len(data) < PACKAGE_HEADER_SIZE:
            raise ValueError("升级包太小")
            
        magic = data[:4]
        if magic != PACKAGE_MAGIC:
            raise ValueError(f"无效升级包标识: {magic}, 期望: {PACKAGE_MAGIC}")
            
        total_size = struct.unpack('<I', data[4:8])[0]
        component_info = data[8]  # 位0-4表示组件存在
        
        # 组件地址和大小信息 (40字节，5组×8字节)
        # 地址是相对于数据区（Header后）的偏移
        components = {}
        
        # BMB_APP_A (bit 0) - 地址在offset 12-15, 大小在16-19
        if component_info & 0x01:
            addr = struct.unpack('<I', data[12:16])[0]
            size = struct.unpack('<I', data[16:20])[0]
            components['BMB_APP_A'] = {'offset': addr, 'size': size}
            
        # BMB_APP_B (bit 1) - 地址在offset 20-23, 大小在24-27
        if component_info & 0x02:
            addr = struct.unpack('<I', data[20:24])[0]
            size = struct.unpack('<I', data[24:28])[0]
            components['BMB_APP_B'] = {'offset': addr, 'size': size}
            
        # BMB_FPGA (bit 2) - 地址在offset 28-31, 大小在32-35
        if component_info & 0x04:
            addr = struct.unpack('<I', data[28:32])[0]
            size = struct.unpack('<I', data[32:36])[0]
            components['BMB_FPGA'] = {'offset': addr, 'size': size}
            
        # TMB_FPGA (bit 3) - 地址在offset 36-39, 大小在40-43
        if component_info & 0x08:
            addr = struct.unpack('<I', data[36:40])[0]
            size = struct.unpack('<I', data[40:44])[0]
            components['TMB_FPGA'] = {'offset': addr, 'size': size}
        
        # TMB_PARAM (bit 4) - 地址在offset 44-47, 大小在48-51
        if component_info & 0x10:
            addr = struct.unpack('<I', data[44:48])[0]
            size = struct.unpack('<I', data[48:52])[0]
            components['TMB_PARAM'] = {'offset': addr, 'size': size}
            
        return {
            'total_size': total_size,
            'component_info': component_info,
            'components': components,
            'header_size': PACKAGE_HEADER_SIZE
        }
        
    def open_serial_ports(self):
        try:
            self.uart_232 = serial.Serial(self.uart_232_port, self.baudrate_232, timeout=1)
            self.uart_485 = serial.Serial(self.uart_485_port, self.baudrate_485, timeout=1)
            self.log(f"串口打开成功: RS232={self.uart_232_port}@{self.baudrate_232}, RS485={self.uart_485_port}@{self.baudrate_485}")
            return True
        except Exception as e:
            self.log(f"串口打开失败: {e}")
            return False
            
    def close_serial_ports(self):
        if self.uart_232: self.uart_232.close()
        if self.uart_485: self.uart_485.close()
        
    def send_cmd_and_wait_ack(self, cmd_bytes, ack_header=b'$LDACK,', timeout=10, simple_ack=False, max_retries=3):
        """
        发送CMD指令并等待ACK（带重试机制）
        - CMD指令通过RS232发送
        - ACK通过RS485接收（因为485一直在发送数据，ACK会混在其中）
        
        Args:
            simple_ack: 简单ACK模式，只检查帧头和包尾（用于软重启指令）
            max_retries: 最大重试次数（默认3次）
        """
        for retry in range(max_retries):
            if retry > 0:
                self.log(f"\n  ⚠ CMD指令重试第 {retry}/{max_retries-1} 次...")
                time.sleep(1)  # 重试前等待1秒
            
            self.log(f"\n>>> 发送CMD指令 (RS232): {bytes_to_hex_str(cmd_bytes)}")
            
            # 发送前清空RS485接收缓冲区，避免旧数据干扰
            if self.uart_485.in_waiting > 0:
                discarded = self.uart_485.read(self.uart_485.in_waiting)
                self.log(f"  清空接收缓冲区: {len(discarded)} 字节")
            
            # 通过RS232发送CMD指令
            self.uart_232.write(cmd_bytes)
            self.uart_232.flush()  # 确保数据发送完成
            
            # 通过RS485接收ACK（累积接收缓冲区，持续搜索ACK）
            recv_buffer = b''
            start_time = time.time()
            last_log_time = start_time
            ack_found = False
            
            self.log(f"  等待ACK (RS485)...")
            
            while time.time() - start_time < timeout:
                if self.uart_485.in_waiting > 0:
                    new_data = self.uart_485.read(self.uart_485.in_waiting)
                    recv_buffer += new_data
                    
                    # 每秒输出一次接收进度
                    if time.time() - last_log_time >= 1.0:
                        self.log(f"  已接收 {len(recv_buffer)} 字节，继续等待ACK...")
                        last_log_time = time.time()
                    
                    # 在缓冲区中搜索ACK标识（从最新数据可能出现的位置开始搜索）
                    search_start = max(0, len(recv_buffer) - len(new_data) - len(ack_header))
                    ack_pos = recv_buffer.find(ack_header, search_start)
                    
                    if ack_pos >= 0:
                        # 找到ACK标识，检查后面是否有足够的数据
                        ack_data = recv_buffer[ack_pos:]
                        
                        # 打印ACK原始内容（取前20字节用于调试）
                        ack_preview_len = min(20, len(ack_data))
                        self.log(f"<<< 找到ACK，原始内容 (前{ack_preview_len}字节): {bytes_to_hex_str(ack_data[:ack_preview_len], 60)}")
                        
                        if simple_ack:
                            # 简单ACK模式：只检查帧头和包尾
                            # ACK格式: $LDACK,(7) + data_len(1) + data(data_len) + error(1) + check_id(4) + CRC(4) + end(2)
                            # ACK总长度 = 19 + data_len
                            if len(ack_data) >= 8:
                                data_len = ack_data[7]  # 获取data_len字段
                                expected_ack_len = 7 + 1 + data_len + 1 + 4 + 4 + 2  # 19 + data_len
                                
                                if len(ack_data) >= expected_ack_len:
                                    # 检查包尾是否为 EE FF
                                    end_pos = expected_ack_len - 2
                                    if ack_data[end_pos] == 0xEE and ack_data[end_pos + 1] == 0xFF:
                                        self.log(f"<<< ACK验证成功 (data_len={data_len}, 总长度={expected_ack_len})")
                                        self.log(f"  帧头=$LDACK,, 包尾=EE FF at [{end_pos}:{end_pos+2}]")
                                        self.log(f"  总接收: {len(recv_buffer)} 字节")
                                        return True
                                    else:
                                        self.log(f"  包尾不匹配: {ack_data[end_pos]:02X} {ack_data[end_pos+1]:02X}, 期望: EE FF (位置:{end_pos})")
                                        continue
                                else:
                                    # 数据不完整，继续等待
                                    continue
                            else:
                                # 数据不完整，继续等待
                                continue
                        else:
                            # 完整ACK模式：解析详细内容
                            if len(ack_data) >= 12:
                                # 逐字节打印解析
                                self.log(f"  字节解析:")
                                self.log(f"    [0-6] header: {bytes_to_hex_str(ack_data[0:7], 20)}")
                                self.log(f"    [7]   byte7:  0x{ack_data[7]:02X} ({ack_data[7]})")
                                self.log(f"    [8]   byte8:  0x{ack_data[8]:02X} ({ack_data[8]})")
                                self.log(f"    [9]   byte9:  0x{ack_data[9]:02X} ({ack_data[9]})")
                                self.log(f"    [10]  byte10: 0x{ack_data[10]:02X} ({ack_data[10]})")
                                self.log(f"    [11]  byte11: 0x{ack_data[11]:02X} ({ack_data[11]})")
                                if len(ack_data) > 12:
                                    self.log(f"    [12+] 后续:  {bytes_to_hex_str(ack_data[12:min(16,len(ack_data))], 20)}")
                                
                                error_code = ack_data[11]
                                partition_byte = ack_data[10] if len(ack_data) > 10 else 0
                                self.current_partition = 'A' if partition_byte == 0 else 'B'
                                self.log(f"  解析结果: 分区={self.current_partition}, 错误码={error_code}")
                                self.log(f"  总接收: {len(recv_buffer)} 字节")
                                if error_code == 0:
                                    return True
                                else:
                                    self.log(f"  ACK错误码非0: {error_code}")
                                    return False
                            else:
                                # ACK不完整，继续等待更多数据
                                continue
                                
                time.sleep(0.005)  # 短暂等待
            
            # 本次超时，显示调试信息
            self.log(f"<<< CMD ACK超时 (总接收 {len(recv_buffer)} 字节)")
            if recv_buffer:
                tail_size = min(200, len(recv_buffer))
                self.log(f"  缓冲区末尾 {tail_size} 字节: {bytes_to_hex_str(recv_buffer[-tail_size:], 100)}")
        
        # 所有重试都失败
        self.log(f"  ✗ CMD指令发送失败，已重试 {max_retries} 次")
        return False
        
    def read_component_data(self, component_name):
        """
        从完整升级包中读取指定组件的数据
        - BMB_APP_A/BMB_APP_B: 发送完整数据（包含1024字节组件Header）
        - BMB_FPGA/TMB_FPGA: 只发送纯固件数据，跳过1024字节组件Header
        - TMB_PARAM: 直接发送原始数据（无Header）
        """
        if component_name not in self.package_info['components']:
            return None
            
        comp_info = self.package_info['components'][component_name]
        with open(self.package_path, 'rb') as f:
            # offset是相对于数据区的偏移，数据区从Header后开始
            f.seek(PACKAGE_HEADER_SIZE + comp_info['offset'])
            full_data = f.read(comp_info['size'])
            
            # 上仓参数：无Header，直接返回原始数据
            if component_name == 'TMB_PARAM':
                self.log(f"  [{component_name}] 无Header，直接发送原始数据，大小={len(full_data)}")
                return full_data
            
            # 对于FPGA组件，跳过1024字节的组件Header，只返回纯固件数据
            if component_name in ['BMB_FPGA', 'TMB_FPGA']:
                if len(full_data) > COMPONENT_HEADER_SIZE:
                    self.log(f"  [{component_name}] 跳过组件Header，原始大小={len(full_data)}，固件大小={len(full_data) - COMPONENT_HEADER_SIZE}")
                    return full_data[COMPONENT_HEADER_SIZE:]
                else:
                    self.log(f"  [{component_name}] 警告: 数据大小({len(full_data)})小于组件Header大小({COMPONENT_HEADER_SIZE})")
                    return full_data
            
            # MCU APP组件返回完整数据（包含组件Header）
            return full_data
            
    def send_ota_packet(self, component_type, total_packets, packet_num, packet_data, verbose=False):
        """
        构建并发送OTA数据包
        包格式: header(7B) + component(1B) + total(2B) + current(2B) + size(2B) + data(1024B) + crc(4B) + footer(2B)
        总长度: 7 + 1 + 2 + 2 + 2 + 1024 + 4 + 2 = 1044 字节
        """
        actual_size = len(packet_data)
        
        # 数据填充到PACKET_DATA_SIZE字节
        if actual_size < PACKET_DATA_SIZE:
            padded_data = packet_data + b'\xFF' * (PACKET_DATA_SIZE - actual_size)
        else:
            padded_data = packet_data[:PACKET_DATA_SIZE]
            
        # 构建包体（与MCU upgrade_packet_t结构匹配）
        # component: 1字节 (B), total/current/size: 各2字节 (H)
        packet_body = b'$LDOTA,' + struct.pack('<BHHH', 
            COMPONENT_TYPE_MAP[component_type], total_packets, packet_num, actual_size
        ) + padded_data
        
        # 计算CRC
        crc = calculate_crc32_mpeg2(padded_data)  # CRC只对数据部分计算
        full_packet = packet_body + struct.pack('<I', crc) + b'\xEE\xFF'
        
        # 显示包内容（详细模式或首包/尾包）
        if verbose or packet_num == 1 or packet_num == total_packets:
            self.log(f"\n  ┌─ 发送OTA包 #{packet_num}/{total_packets} ─────────────────────")
            self.log(f"  │ 组件类型: {component_type} ({COMPONENT_TYPE_MAP[component_type]})")
            self.log(f"  │ 总包数: {total_packets}, 当前包: {packet_num}")
            self.log(f"  │ 实际数据大小: {actual_size} 字节")
            self.log(f"  │ 填充后大小: {len(padded_data)} 字节")
            self.log(f"  │ 数据CRC: 0x{crc:08X}")
            self.log(f"  │ 数据预览(前16字节): {bytes_to_hex_str(packet_data[:16], 48)}")
            if actual_size > 16:
                self.log(f"  │ 数据预览(后16字节): {bytes_to_hex_str(packet_data[-16:], 48)}")
            self.log(f"  │ 完整包大小: {len(full_packet)} 字节")
            self.log(f"  │ 包头: {bytes_to_hex_str(full_packet[:15], 48)}")
            self.log(f"  │ 包尾: {bytes_to_hex_str(full_packet[-6:], 48)}")
            self.log(f"  └─────────────────────────────────────────────")
        
        # 发送数据包
        self.uart_485.write(full_packet)
        
    def receive_ota_ack(self, timeout=2):
        """
        接收OTA ACK
        会在接收到的数据流中搜索ACK标识（处理ACK被其他数据包围的情况）
        """
        ack_header = b'$LDACK,'
        recv_buffer = b''
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if self.uart_485.in_waiting > 0:
                new_data = self.uart_485.read(self.uart_485.in_waiting)
                recv_buffer += new_data
                
                # 在缓冲区中搜索ACK标识（从最新数据可能出现的位置开始）
                search_start = max(0, len(recv_buffer) - len(new_data) - len(ack_header))
                ack_pos = recv_buffer.find(ack_header, search_start)
                
                if ack_pos >= 0:
                    # 找到ACK标识，检查后面是否有足够的数据（7字节header + 9字节数据 = 16字节）
                    ack_data = recv_buffer[ack_pos:]
                    if len(ack_data) >= 16:
                        # 解析ACK内容：header(7) + comp_type(2) + total(2) + current(2) + error(1) + footer(2)
                        remaining = ack_data[7:16]
                        comp_type, total, current, error_code = struct.unpack('<HHHB', remaining[:7])
                        return error_code
                        
                # 防止缓冲区无限增长，保留最后8KB（足够容纳大量其他数据）
                if len(recv_buffer) > 8192:
                    recv_buffer = recv_buffer[-4096:]
                    
            time.sleep(0.001)  # 短暂等待
            
        return -1  # 超时
        
    def send_component_packets(self, component_name, component_data, max_retries=5, base_timeout=2):
        """
        发送组件的所有数据包
        
        Args:
            component_name: 组件名称
            component_data: 组件数据
            max_retries: 最大重试次数（默认5次）
            base_timeout: 基础ACK超时时间（默认5秒）
        """
        total_packets = (len(component_data) + PACKET_DATA_SIZE - 1) // PACKET_DATA_SIZE
        self.log(f"\n开始发送 {component_name}，数据大小={len(component_data)}字节，共 {total_packets} 包")
        
        # 确定组件类型
        if 'APP' in component_name:
            comp_type = 'BMB_APP'
        elif 'BMB_FPGA' in component_name:
            comp_type = 'BMB_FPGA'
        elif 'TMB_PARAM' in component_name:
            comp_type = 'TMB_PARAM'
        else:
            comp_type = 'TMB_FPGA'
        
        # 上仓组件（FPGA和参数）使用相同的特殊处理流程
        is_tmb_component = (comp_type in ['TMB_FPGA', 'TMB_PARAM'])
        tmb_erase_started = False  # 标记是否已开始擦除
        
        if is_tmb_component:
            comp_display_name = "上仓FPGA" if comp_type == 'TMB_FPGA' else "上仓参数"
            self.log(f"  [{comp_display_name}] 特殊升级模式: 第一包将触发Flash擦除(约12秒)")
            # 计算page数用于显示
            page_count = (len(component_data) + 255) // 256
            self.log(f"  [{comp_display_name}] 数据大小={len(component_data)}B, 预计page数={page_count}")
        
        for i in range(total_packets):
            if not self.is_running:
                self.log("升级已取消")
                return False
                
            packet_num = i + 1
            start_idx = i * PACKET_DATA_SIZE
            end_idx = min(start_idx + PACKET_DATA_SIZE, len(component_data))
            packet_data = component_data[start_idx:end_idx]
            
            retry_count = 0
            success = False
            
            # 上仓组件（FPGA/参数）第一包特殊处理：等待擦除完成
            if is_tmb_component and packet_num == 1:
                max_retries_first_pkt = 10  # 第一包允许更多重试
                first_pkt_timeout = TMB_FPGA_FIRST_PKT_TIMEOUT
                log_prefix = "[上仓FPGA]" if comp_type == 'TMB_FPGA' else "[上仓参数]"
                
                self.log(f"  {log_prefix} 发送第一包，将等待Flash擦除...")
                
                while retry_count < max_retries_first_pkt and not success:
                    if not self.is_running:
                        return False
                    
                    self.send_ota_packet(comp_type, total_packets, packet_num, packet_data, verbose=self.verbose_packet)
                    error_code = self.receive_ota_ack(timeout=first_pkt_timeout)
                    
                    if error_code == 0:
                        self.log(f"  {log_prefix} 第一包发送成功，Flash擦除已完成")
                        if self.progress_callback:
                            self.progress_callback(component_name, packet_num, total_packets)
                        success = True
                        tmb_erase_started = True
                    elif error_code == 1:  # RETRANSMIT - MCU正在擦除Flash
                        if not tmb_erase_started:
                            self.log(f"  {log_prefix} MCU正在擦除Flash，等待 {TMB_FPGA_RETRANSMIT_DELAY} 秒后重发...")
                            tmb_erase_started = True
                        else:
                            self.log(f"  {log_prefix} 继续等待擦除完成...")
                        retry_count += 1
                        time.sleep(TMB_FPGA_RETRANSMIT_DELAY)
                    elif error_code == -1:  # 超时
                        retry_count += 1
                        self.log(f"  {log_prefix} ACK超时 (尝试 {retry_count}/{max_retries_first_pkt})")
                        time.sleep(1)
                    else:
                        self.log(f"  {log_prefix} 第一包发送失败: {ACK_ERROR_MAP.get(error_code, f'未知错误({error_code})')}")
                        return False
                
                if not success:
                    self.log(f"  {log_prefix} 第一包重试次数耗尽，Flash擦除可能失败")
                    return False
                continue  # 继续下一包
            
            # 普通包处理逻辑
            log_prefix = "[上仓FPGA]" if comp_type == 'TMB_FPGA' else ("[上仓参数]" if comp_type == 'TMB_PARAM' else "")
            
            while retry_count < max_retries and not success:
                if not self.is_running:
                    self.log("升级已取消")
                    return False
                    
                self.send_ota_packet(comp_type, total_packets, packet_num, packet_data, verbose=self.verbose_packet)
                
                # 上仓组件使用更长的超时（因为MCU需要分page发送给上仓）
                if is_tmb_component:
                    # 每包1024B = 4个page，每个page需要传输时间
                    current_timeout = base_timeout * 2
                else:
                    # 指数退避：超时时间随重试次数增加
                    current_timeout = base_timeout * (1.5 ** retry_count)
                
                error_code = self.receive_ota_ack(timeout=current_timeout)
                
                if error_code == 0:
                    # 每10包输出一次进度，减少日志量
                    if packet_num % 10 == 0 or packet_num == total_packets:
                        self.log(f"  进度: {packet_num}/{total_packets} ({100*packet_num//total_packets}%)")
                    # 更新进度条
                    if self.progress_callback:
                        self.progress_callback(component_name, packet_num, total_packets)
                    success = True
                elif error_code == -1:  # 超时
                    retry_count += 1
                    retry_delay = 0.5 if is_tmb_component else 0.2 * (2 ** (retry_count - 1))
                    self.log(f"  ⚠ 包 {packet_num} ACK超时 (尝试 {retry_count}/{max_retries})")
                    time.sleep(retry_delay)
                elif error_code == 1:  # 需要重传
                    retry_count += 1
                    # 上仓组件的RETRANSMIT可能是page传输问题，需要更长等待
                    if is_tmb_component:
                        self.log(f"  ⚠ {log_prefix} 包 {packet_num} 需要重传 (尝试 {retry_count}/{max_retries})")
                        time.sleep(1)
                    else:
                        retry_delay = 0.2 * (2 ** (retry_count - 1))
                        self.log(f"  ⚠ 包 {packet_num} 需要重传 (尝试 {retry_count}/{max_retries})")
                        time.sleep(retry_delay)
                elif error_code in [8, 9]:  # 上仓组件传输错误，可重试（MCU已同步page count）
                    retry_count += 1
                    self.log(f"  ⚠ {log_prefix} 包 {packet_num} 传输失败: {ACK_ERROR_MAP.get(error_code)} (重试 {retry_count}/{max_retries})")
                    if retry_count >= max_retries:
                        self.log(f"  ✗ {log_prefix} 包 {packet_num} 重试次数耗尽，升级失败")
                        return False
                    time.sleep(1)  # 等待1秒后重试当前包
                else:
                    self.log(f"  ✗ 包 {packet_num} 发送失败: {ACK_ERROR_MAP.get(error_code, f'未知错误({error_code})')}")
                    return False
                    
            if not success:
                self.log(f"  ✗ 包 {packet_num} 重试次数耗尽")
                return False
                
        return True
        
    def run_upgrade(self):
        self.is_running = True
        if not self.open_serial_ports():
            self.is_running = False
            return False
            
        try:
            # 步骤1: 发送升级CMD（增加超时和重试）
            self.log("="*50)
            self.log("步骤1: 发送升级指令...")
            upgrade_cmd = bytes.fromhex("24 4C 44 43 4D 44 2C 03 03 04 00 78 56 34 12 F9 5E 8C 52 EE FF")
            if not self.send_cmd_and_wait_ack(upgrade_cmd, timeout=15, max_retries=5):
                self.log("升级指令发送失败")
                return False
                
            if not self.is_running:
                return False
                
            # 步骤2: 确定要发送的APP组件
            app_component = None
            if self.current_partition == 'A':
                app_component = 'BMB_APP_B'  # 当前在A区，发送B区APP
            else:
                app_component = 'BMB_APP_A'  # 当前在B区，发送A区APP
                
            self.log(f"\nMCU当前在 {self.current_partition} 区，将发送 {app_component}")
            
            # 步骤3: 发送APP组件（如果存在）
            if app_component in self.package_info['components']:
                app_data = self.read_component_data(app_component)
                if not self.send_component_packets(app_component, app_data):
                    self.log(f"{app_component} 发送失败")
                    return False
            else:
                self.log(f"警告: 升级包中不包含 {app_component}")
                
            if not self.is_running:
                return False
                
            # 步骤4: 发送下仓FPGA组件（如果存在）
            if 'BMB_FPGA' in self.package_info['components']:
                self.log("\n" + "="*50)
                self.log("步骤4a: 发送下仓FPGA组件...")
                fpga_data = self.read_component_data('BMB_FPGA')
                if not self.send_component_packets('BMB_FPGA', fpga_data):
                    self.log("BMB_FPGA 发送失败")
                    return False
            
            if not self.is_running:
                return False
            
            # 步骤4b: 发送上仓FPGA组件（如果存在）
            # 上仓FPGA特殊流程：MCU收到后通过寄存器发送给上仓FPGA
            # - 第一包触发Flash擦除（约12秒）
            # - 擦除完成后分page(256B)传输
            if 'TMB_FPGA' in self.package_info['components']:
                self.log("\n" + "="*50)
                self.log("步骤4b: 发送上仓FPGA组件 (特殊模式)...")
                self.log("  注意: 上仓FPGA需要先擦除Flash(约12秒)，请耐心等待")
                fpga_data = self.read_component_data('TMB_FPGA')
                if not self.send_component_packets('TMB_FPGA', fpga_data):
                    self.log("TMB_FPGA 发送失败")
                    return False
                        
            if not self.is_running:
                return False
            
            # 步骤4c: 发送上仓参数组件（如果存在）
            # 上仓参数特殊流程：与上仓FPGA相同，但擦除地址不同
            # - 第一包触发Flash擦除（约12秒，擦除地址0x80a05008）
            # - 擦除完成后分page(256B)传输
            # - 无Header，直接传输原始数据
            if 'TMB_PARAM' in self.package_info['components']:
                self.log("\n" + "="*50)
                self.log("步骤4c: 发送上仓参数组件 (特殊模式)...")
                self.log("  注意: 上仓参数需要先擦除Flash(约12秒)，请耐心等待")
                param_data = self.read_component_data('TMB_PARAM')
                if not self.send_component_packets('TMB_PARAM', param_data):
                    self.log("TMB_PARAM 发送失败")
                    return False
                        
            if not self.is_running:
                return False
                        
            # 步骤5: 发送软重启
            self.log("\n" + "="*50)
            self.log("步骤5: 发送软重启指令...")
            reboot_cmd = bytes.fromhex("24 4C 44 43 4D 44 2C 01 07 78 56 34 12 2D AE C3 45 EE FF")
            if not self.send_cmd_and_wait_ack(reboot_cmd, simple_ack=True):
                self.log("软重启指令发送失败")
                return False
                
            # 步骤6: 等待MCU重启
            self.log("\n" + "="*50)
            self.log("步骤6: 等待MCU重启 (20秒)...")
            time.sleep(20)  # 增加等待时间，确保MCU完全启动
            
            if not self.is_running:
                return False
            
            # 重启后清空串口缓冲区（MCU启动时可能发送大量数据）
            if self.uart_485.in_waiting > 0:
                discarded = self.uart_485.read(self.uart_485.in_waiting)
                self.log(f"  清空重启后缓冲区: {len(discarded)} 字节")
            
            # 步骤7: 版本验证
            self.log("\n" + "="*50)
            self.log("步骤7: 版本验证...")
            
            # 7.1 发送切换一般指令模式（增加超时和重试）
            self.log("  发送切换一般指令模式...")
            switch_mode_cmd = bytes.fromhex("24 4C 44 43 4D 44 2C 03 03 01 00 78 56 34 12 61 66 04 25 EE FF")
            if not self.send_cmd_and_wait_ack(switch_mode_cmd, simple_ack=True, timeout=15, max_retries=5):
                self.log("  切换一般指令模式失败")
                return False
            
            time.sleep(0.5)
            
            # 7.2 验证各组件版本
            version_verify_result = self.verify_component_versions()
            
            if version_verify_result:
                self.log("\n升级流程完成，版本验证通过!")
            else:
                self.log("\n升级流程完成，但版本验证失败!")
            
            return version_verify_result
            
        finally:
            self.is_running = False
            self.close_serial_ports()
    
    def stop_upgrade(self):
        """停止升级"""
        self.is_running = False
    
    def get_component_version_from_package(self, component_name):
        """
        从升级包的组件binheader中提取版本信息（文件名）
        binheader前128字节包含文件名
        """
        if component_name not in self.package_info['components']:
            return None
        
        comp_info = self.package_info['components'][component_name]
        with open(self.package_path, 'rb') as f:
            f.seek(PACKAGE_HEADER_SIZE + comp_info['offset'])
            binheader = f.read(128)  # 读取128字节的binheader
        
        # 提取文件名（以0xFF或0x00结尾）
        filename_end = 128
        for i, b in enumerate(binheader):
            if b == 0xFF or b == 0x00:
                filename_end = i
                break
        
        try:
            filename = binheader[:filename_end].decode('utf-8')
            return filename
        except:
            return binheader[:filename_end].hex()
    
    def send_version_query(self, version_type, max_retries=3):
        """
        发送版本查询指令并返回ACK中的版本数据，支持重传机制
        version_type: 0=下仓FPGA, 1=上仓FPGA, 2=下仓MCU
        max_retries: 最大重试次数（默认3次）
        
        ACK格式:
        - $LDACK, (7字节)
        - data_len (1字节)
        - data[0-1]: 长度字段 (2字节)
        - data[2]: 版本类型 (1字节)
        - data[3-26]: 版本数据 (24字节)
        - error_code (1字节)
        - check_id (4字节)
        - CRC (4字节)
        - end_flag (2字节) = 0xEE 0xFF
        """
        # 版本查询指令（预定义）
        version_cmds = {
            0: bytes.fromhex("24 4C 44 43 4D 44 2C 1C 02 0F 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 78 56 34 12 CC F8 AD 21 EE FF"),  # 下仓FPGA
            1: bytes.fromhex("24 4C 44 43 4D 44 2C 1C 02 0F 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 78 56 34 12 D2 4F ED 8A EE FF"),  # 上仓FPGA
            2: bytes.fromhex("24 4C 44 43 4D 44 2C 1C 02 0F 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 78 56 34 12 47 8B ED 73 EE FF"),  # 下仓MCU
        }
        
        version_names = {0: "下仓FPGA", 1: "上仓FPGA", 2: "下仓MCU"}
        
        if version_type not in version_cmds:
            return None
        
        cmd = version_cmds[version_type]
        ack_header = b'$LDACK,'
        timeout = 3  # 单次超时时间
        
        for retry in range(max_retries):
            if retry > 0:
                self.log(f"  ⚠ 重试第 {retry}/{max_retries-1} 次...")
                time.sleep(1)  # 重试前等待1秒
            
            self.log(f"\n  查询{version_names[version_type]}版本...")
            self.log(f"  >>> 发送: {bytes_to_hex_str(cmd[:20], 60)}...")
            
            # 清空接收缓冲区
            if self.uart_485.in_waiting > 0:
                self.uart_485.read(self.uart_485.in_waiting)
            
            # 发送指令
            self.uart_232.write(cmd)
            
            # 接收ACK并解析版本数据
            recv_buffer = b''
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                if self.uart_485.in_waiting > 0:
                    new_data = self.uart_485.read(self.uart_485.in_waiting)
                    recv_buffer += new_data
                    
                    ack_pos = recv_buffer.find(ack_header)
                    if ack_pos >= 0:
                        ack_data = recv_buffer[ack_pos:]
                        # 版本数据偏移: 7(header) + 1(data_len) + 3(data前3字节) = 11
                        # 版本数据长度: 24字节
                        if len(ack_data) >= 35:  # 7+1+3+24=35字节
                            version_data = ack_data[11:35]  # 24字节版本数据
                            self.log(f"  <<< 收到ACK: {bytes_to_hex_str(ack_data[:min(40, len(ack_data))], 100)}")
                            self.log(f"  版本数据(24字节): {bytes_to_hex_str(version_data, 60)}")
                            return version_data
                
                time.sleep(0.01)
            
            self.log(f"  <<< 版本查询超时 (尝试 {retry+1}/{max_retries})")
        
        self.log(f"  ✗ 版本查询失败，已重试 {max_retries} 次")
        return None
    
    def validate_param_file_format(self, param_data):
        """
        验证参数文件格式
        检查前2字节是否为 4B 02
        返回: (is_valid, warning_message)
        """
        if len(param_data) < 8:
            return False, "参数文件太小，无法验证"
        
        magic = param_data[0:2]
        if magic != TMB_PARAM_MAGIC:
            return False, f"警告: 不是有效的参数文件! 前2字节为 {bytes_to_hex_str(magic, 10)}，期望 4B 02"
        
        return True, None
    
    def get_param_checksum_from_file(self, param_data):
        """
        从参数文件中提取校验值
        1. 读取第5-8字节（索引4-7）作为地址（小端格式）
        2. 在该地址前4字节处读取校验值
        返回: (checksum_bytes, error_message)
        """
        if len(param_data) < 8:
            return None, "参数文件太小，无法提取校验值地址"
        
        # 读取第5-8字节作为地址（小端格式）
        addr_bytes = param_data[TMB_PARAM_ADDR_OFFSET:TMB_PARAM_ADDR_OFFSET + 4]
        checksum_addr = struct.unpack('<I', addr_bytes)[0]
        
        self.log(f"  参数文件校验值地址字段: {bytes_to_hex_str(addr_bytes, 10)} -> 0x{checksum_addr:08X}")
        
        # 校验值在该地址前4字节
        checksum_offset = checksum_addr - 4
        
        if checksum_offset < 0 or checksum_offset + 4 > len(param_data):
            return None, f"校验值偏移 0x{checksum_offset:08X} 超出文件范围 (文件大小: {len(param_data)})"
        
        checksum_bytes = param_data[checksum_offset:checksum_offset + 4]
        self.log(f"  从文件偏移 0x{checksum_offset:08X} 提取校验值: {bytes_to_hex_str(checksum_bytes, 10)}")
        
        return checksum_bytes, None
    
    def send_param_checksum_query(self, timeout=10, max_retries=3):
        """
        发送参数校验值查询指令
        返回: 4字节校验值 或 None
        """
        ack_header = b'$LDACK,'
        
        for retry in range(max_retries):
            if retry > 0:
                self.log(f"  重试 {retry}/{max_retries}...")
                time.sleep(1)
            
            # 清空接收缓冲区
            if self.uart_485.in_waiting > 0:
                self.uart_485.read(self.uart_485.in_waiting)
            
            # 发送查询指令
            self.log(f">>> 发送参数校验值查询指令: {bytes_to_hex_str(TMB_PARAM_QUERY_CMD, 60)}")
            self.uart_232.write(TMB_PARAM_QUERY_CMD)
            self.uart_232.flush()
            
            # 接收ACK
            recv_buffer = b''
            start_time = time.time()
            
            while time.time() - start_time < timeout:
                if self.uart_485.in_waiting > 0:
                    new_data = self.uart_485.read(self.uart_485.in_waiting)
                    recv_buffer += new_data
                    
                    ack_pos = recv_buffer.find(ack_header)
                    if ack_pos >= 0:
                        ack_data = recv_buffer[ack_pos:]
                        # 需要足够的数据来提取校验值（至少18+4=22字节）
                        if len(ack_data) >= TMB_PARAM_CHECKSUM_ACK_OFFSET + 4:
                            self.log(f"<<< 收到ACK: {bytes_to_hex_str(ack_data[:min(40, len(ack_data))], 80)}")
                            # 提取第18字节开始的4字节校验值
                            checksum_bytes = ack_data[TMB_PARAM_CHECKSUM_ACK_OFFSET:TMB_PARAM_CHECKSUM_ACK_OFFSET + 4]
                            self.log(f"  ACK中校验值 (偏移{TMB_PARAM_CHECKSUM_ACK_OFFSET}): {bytes_to_hex_str(checksum_bytes, 10)}")
                            return checksum_bytes
                
                time.sleep(0.01)
            
            self.log(f"  <<< 参数校验值查询超时")
        
        self.log(f"  ✗ 参数校验值查询失败，已重试 {max_retries} 次")
        return None
    
    def verify_param_checksum(self):
        """
        验证上仓参数校验值
        返回: True=验证通过, False=验证失败
        """
        self.log("\n  ---- 上仓参数 校验值验证 ----")
        
        # 获取参数文件原始数据
        if 'TMB_PARAM' not in self.package_info['components']:
            self.log("  跳过: 升级包中不包含 TMB_PARAM")
            return True
        
        comp_info = self.package_info['components']['TMB_PARAM']
        with open(self.package_path, 'rb') as f:
            f.seek(PACKAGE_HEADER_SIZE + comp_info['offset'])
            param_data = f.read(comp_info['size'])
        
        # 1. 验证参数文件格式
        is_valid, warning = self.validate_param_file_format(param_data)
        if not is_valid:
            self.log(f"  {warning}")
            self.log("  跳过校验值验证")
            return True  # 不是参数文件，跳过验证但不算失败
        
        self.log(f"  ✓ 参数文件格式有效 (魔数: 4B 02)")
        
        # 2. 从文件提取校验值
        expected_checksum, error = self.get_param_checksum_from_file(param_data)
        if error:
            self.log(f"  ✗ {error}")
            return False
        
        # 3. 查询设备实际校验值
        actual_checksum = self.send_param_checksum_query()
        if actual_checksum is None:
            self.log(f"  ✗ 无法获取设备校验值")
            return False
        
        # 4. 比较校验值
        if expected_checksum == actual_checksum:
            self.log(f"  ✓ 校验值匹配成功!")
            self.log(f"    文件校验值: {bytes_to_hex_str(expected_checksum, 10)}")
            self.log(f"    设备校验值: {bytes_to_hex_str(actual_checksum, 10)}")
            return True
        else:
            self.log(f"  ✗ 校验值不匹配!")
            self.log(f"    文件校验值: {bytes_to_hex_str(expected_checksum, 10)}")
            self.log(f"    设备校验值: {bytes_to_hex_str(actual_checksum, 10)}")
            return False
    
    def verify_component_versions(self):
        """
        验证已升级组件的版本
        版本验证规则：
        - MCU：检查binheader文件名中是否包含ACK返回的版本字符串
        - FPGA：将8字节二进制版本转换为十六进制字符串，检查是否在文件名中
        - TMB_PARAM：验证校验值（通过专门的校验值查询指令）
        """
        all_passed = True
        
        # 确定需要验证的组件
        # 验证顺序：下仓FPGA -> 下仓MCU -> 上仓FPGA
        components_to_verify = []
        
        # 下仓FPGA
        if 'BMB_FPGA' in self.package_info['components']:
            components_to_verify.append(('下仓FPGA', 0, 'BMB_FPGA'))
        
        # 下仓MCU - 根据当前分区确定MCU APP组件
        if self.current_partition == 'A':
            mcu_comp = 'BMB_APP_B'
        else:
            mcu_comp = 'BMB_APP_A'
        
        if mcu_comp in self.package_info['components']:
            components_to_verify.append(('下仓MCU', 2, mcu_comp))
        
        # 上仓FPGA
        if 'TMB_FPGA' in self.package_info['components']:
            components_to_verify.append(('上仓FPGA', 1, 'TMB_FPGA'))
        
        for comp_name, query_type, pkg_comp_name in components_to_verify:
            self.log(f"\n  ---- {comp_name} 版本验证 ----")
            
            # 从升级包获取预期版本（文件名）
            expected_version = self.get_component_version_from_package(pkg_comp_name)
            self.log(f"  升级包binheader中文件名: {expected_version}")
            
            # 查询实际版本
            actual_version_data = self.send_version_query(query_type)
            
            if actual_version_data is None:
                self.log(f"  ✗ 版本查询失败")
                all_passed = False
                continue
            
            # 解析并比较版本数据
            if query_type == 2:  # MCU版本是字符串
                # 找到字符串结尾（以0x00结束）
                str_end = 24
                for i, b in enumerate(actual_version_data):
                    if b == 0x00:
                        str_end = i
                        break
                try:
                    actual_version = actual_version_data[:str_end].decode('utf-8')
                except:
                    actual_version = actual_version_data[:str_end].hex()
                
                self.log(f"  设备ACK返回版本: {actual_version}")
                
                # ACK版本必须完整出现在binheader文件名中（作为子串）
                if expected_version and actual_version and actual_version in expected_version:
                    self.log(f"  ✓ 版本匹配成功！(ACK版本在文件名中找到)")
                else:
                    self.log(f"  ✗ 版本不匹配！")
                    self.log(f"    ACK版本: {actual_version}")
                    self.log(f"    binheader文件名: {expected_version}")
                    all_passed = False
            else:  # FPGA版本是8字节二进制，需要解码为ASCII字符串
                # 提取8字节版本数据
                version_bytes = actual_version_data[:8]
                
                self.log(f"  设备ACK返回版本 (8字节原始): {bytes_to_hex_str(version_bytes, 24)}")
                
                # 将8字节十六进制解码为ASCII字符串
                # 找到字符串结尾（以0x00结束或遇到非打印字符）
                str_end = 8
                for i, b in enumerate(version_bytes):
                    if b == 0x00 or b < 0x20 or b > 0x7E:
                        str_end = i
                        break
                
                try:
                    actual_version = version_bytes[:str_end].decode('ascii')
                except:
                    # 如果解码失败，使用十六进制表示
                    actual_version = version_bytes[:str_end].hex()
                
                self.log(f"  设备ACK返回版本 (ASCII): {actual_version}")
                
                # ACK版本必须完整出现在binheader文件名中（作为子串）
                if expected_version and actual_version and actual_version in expected_version:
                    self.log(f"  ✓ 版本匹配成功！(ACK版本在文件名中找到)")
                else:
                    self.log(f"  ✗ 版本不匹配！")
                    self.log(f"    ACK版本: {actual_version}")
                    self.log(f"    binheader文件名: {expected_version}")
                    all_passed = False
        
        # 验证上仓参数校验值（如果存在）
        if 'TMB_PARAM' in self.package_info['components']:
            if not self.verify_param_checksum():
                all_passed = False
        
        return all_passed


class OtaUpgradeGUI:
    """OTA升级可视化界面"""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("JT32 OTA 升级工具")
        self.root.geometry("700x600")
        self.root.resizable(True, True)
        
        self.upgrade_flow = None
        self.upgrade_thread = None
        
        self.setup_ui()
        self.refresh_ports()
        
    def setup_ui(self):
        """创建UI界面"""
        # 主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # ===== 串口配置区域 =====
        port_frame = ttk.LabelFrame(main_frame, text="串口配置", padding="10")
        port_frame.pack(fill=tk.X, pady=(0, 10))
        
        # RS232配置
        ttk.Label(port_frame, text="RS232 串口:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.combo_232_port = ttk.Combobox(port_frame, width=15, state="readonly")
        self.combo_232_port.grid(row=0, column=1, padx=5)
        
        ttk.Label(port_frame, text="波特率:").grid(row=0, column=2, padx=5)
        self.entry_232_baud = ttk.Entry(port_frame, width=10)
        self.entry_232_baud.insert(0, str(DEFAULT_BAUDRATE_232))
        self.entry_232_baud.grid(row=0, column=3, padx=5)
        
        # RS485配置
        ttk.Label(port_frame, text="RS485 串口:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
        self.combo_485_port = ttk.Combobox(port_frame, width=15, state="readonly")
        self.combo_485_port.grid(row=1, column=1, padx=5, pady=5)
        
        ttk.Label(port_frame, text="波特率:").grid(row=1, column=2, padx=5, pady=5)
        self.entry_485_baud = ttk.Entry(port_frame, width=10)
        self.entry_485_baud.insert(0, str(DEFAULT_BAUDRATE_485))
        self.entry_485_baud.grid(row=1, column=3, padx=5, pady=5)
        
        # 刷新按钮
        self.btn_refresh = ttk.Button(port_frame, text="刷新串口", command=self.refresh_ports)
        self.btn_refresh.grid(row=0, column=4, rowspan=2, padx=10)
        
        # ===== 升级包选择区域 =====
        file_frame = ttk.LabelFrame(main_frame, text="升级包", padding="10")
        file_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.entry_file = ttk.Entry(file_frame, width=60)
        self.entry_file.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        self.btn_browse = ttk.Button(file_frame, text="浏览...", command=self.browse_file)
        self.btn_browse.pack(side=tk.RIGHT)
        
        # ===== 升级包信息显示 =====
        info_frame = ttk.LabelFrame(main_frame, text="升级包信息", padding="10")
        info_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.label_pkg_info = ttk.Label(info_frame, text="请选择升级包文件", foreground="gray")
        self.label_pkg_info.pack(anchor=tk.W)
        
        # ===== 进度条 =====
        progress_frame = ttk.LabelFrame(main_frame, text="升级进度", padding="10")
        progress_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.label_progress = ttk.Label(progress_frame, text="等待开始...")
        self.label_progress.pack(anchor=tk.W)
        
        self.progress_bar = ttk.Progressbar(progress_frame, mode='determinate', length=400)
        self.progress_bar.pack(fill=tk.X, pady=5)
        
        # ===== 控制按钮 =====
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.btn_start = ttk.Button(btn_frame, text="开始升级", command=self.start_upgrade)
        self.btn_start.pack(side=tk.LEFT, padx=5)
        
        self.btn_stop = ttk.Button(btn_frame, text="停止升级", command=self.stop_upgrade, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=5)
        
        # 详细显示复选框
        self.var_verbose = tk.BooleanVar(value=False)
        self.chk_verbose = ttk.Checkbutton(btn_frame, text="显示每包详细内容", variable=self.var_verbose)
        self.chk_verbose.pack(side=tk.LEFT, padx=20)
        
        self.btn_clear = ttk.Button(btn_frame, text="清空日志", command=self.clear_log)
        self.btn_clear.pack(side=tk.RIGHT, padx=5)
        
        # ===== 日志显示区域 =====
        log_frame = ttk.LabelFrame(main_frame, text="日志输出", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, state=tk.DISABLED, 
                                                   font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
    def refresh_ports(self):
        """刷新串口列表"""
        ports = [port.device for port in serial.tools.list_ports.comports()]
        self.combo_232_port['values'] = ports
        self.combo_485_port['values'] = ports
        
        if ports:
            if not self.combo_232_port.get():
                self.combo_232_port.set(ports[0])
            if not self.combo_485_port.get():
                self.combo_485_port.set(ports[-1] if len(ports) > 1 else ports[0])
        
        self.append_log(f"检测到串口: {', '.join(ports) if ports else '无'}")
        
    def browse_file(self):
        """选择升级包文件"""
        filename = filedialog.askopenfilename(
            title="选择升级包",
            filetypes=[("二进制文件", "*.bin"), ("所有文件", "*.*")]
        )
        if filename:
            self.entry_file.delete(0, tk.END)
            self.entry_file.insert(0, filename)
            self.parse_package_info(filename)
            
    def parse_package_info(self, filepath):
        """解析并显示升级包信息"""
        try:
            with open(filepath, 'rb') as f:
                data = f.read(PACKAGE_HEADER_SIZE)
            
            if len(data) < PACKAGE_HEADER_SIZE:
                self.label_pkg_info.config(text="文件太小，不是有效升级包", foreground="red")
                return
                
            magic = data[:4]
            if magic != PACKAGE_MAGIC:
                self.label_pkg_info.config(text=f"无效标识: {magic}", foreground="red")
                return
                
            total_size = struct.unpack('<I', data[4:8])[0]
            comp_flags = data[8]
            
            # 组件地址/大小信息从offset 12开始，每组8字节（4字节地址 + 4字节大小）
            components = []
            if comp_flags & 0x01:  # bit 0: BMB_APP_A
                size = struct.unpack('<I', data[16:20])[0]  # offset 12+4 = 16
                components.append(f"BMB_APP_A({size//1024}KB)")
            if comp_flags & 0x02:  # bit 1: BMB_APP_B
                size = struct.unpack('<I', data[24:28])[0]  # offset 20+4 = 24
                components.append(f"BMB_APP_B({size//1024}KB)")
            if comp_flags & 0x04:  # bit 2: BMB_FPGA
                size = struct.unpack('<I', data[32:36])[0]  # offset 28+4 = 32
                components.append(f"BMB_FPGA({size//1024}KB)")
            if comp_flags & 0x08:  # bit 3: TMB_FPGA
                size = struct.unpack('<I', data[40:44])[0]  # offset 36+4 = 40
                firmware_size = size - COMPONENT_HEADER_SIZE
                page_count = (firmware_size + 255) // 256
                components.append(f"TMB_FPGA({firmware_size//1024}KB,{page_count}pages)")
            if comp_flags & 0x10:  # bit 4: TMB_PARAM
                size = struct.unpack('<I', data[48:52])[0]  # offset 44+4 = 48
                # 上仓参数无Header，直接传输原始数据
                page_count = (size + 255) // 256
                components.append(f"TMB_PARAM({size//1024}KB,{page_count}pages)")
            
            info_text = f"总大小: {total_size//1024}KB | 组件: {', '.join(components)}"
            self.label_pkg_info.config(text=info_text, foreground="green")
            
        except Exception as e:
            self.label_pkg_info.config(text=f"解析失败: {e}", foreground="red")
            
    def append_log(self, message):
        """添加日志（线程安全）"""
        def _append():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _append)
        
    def clear_log(self):
        """清空日志"""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)
        
    def update_progress(self, component_name, current, total):
        """更新进度条（线程安全）"""
        def _update():
            percent = int(100 * current / total)
            self.progress_bar['value'] = percent
            self.label_progress.config(text=f"{component_name}: {current}/{total} ({percent}%)")
        self.root.after(0, _update)
        
    def start_upgrade(self):
        """开始升级"""
        # 验证输入
        package_path = self.entry_file.get().strip()
        if not package_path:
            messagebox.showerror("错误", "请选择升级包文件")
            return
            
        if not Path(package_path).exists():
            messagebox.showerror("错误", "升级包文件不存在")
            return
            
        uart_232_port = self.combo_232_port.get()
        uart_485_port = self.combo_485_port.get()
        
        if not uart_232_port or not uart_485_port:
            messagebox.showerror("错误", "请选择串口")
            return
            
        try:
            baudrate_232 = int(self.entry_232_baud.get())
            baudrate_485 = int(self.entry_485_baud.get())
        except ValueError:
            messagebox.showerror("错误", "波特率必须是数字")
            return
        
        # 禁用控件
        self.set_controls_state(False)
        self.progress_bar['value'] = 0
        self.label_progress.config(text="正在初始化...")
        
        # 创建升级实例
        try:
            self.upgrade_flow = OtaUpgradeFlow(
                package_path=package_path,
                uart_232_port=uart_232_port,
                uart_485_port=uart_485_port,
                baudrate_232=baudrate_232,
                baudrate_485=baudrate_485,
                log_callback=self.append_log
            )
            self.upgrade_flow.progress_callback = self.update_progress
            self.upgrade_flow.verbose_packet = self.var_verbose.get()  # 设置详细显示模式
        except Exception as e:
            messagebox.showerror("错误", f"初始化失败: {e}")
            self.set_controls_state(True)
            return
        
        # 在后台线程运行升级
        self.upgrade_thread = threading.Thread(target=self.run_upgrade_thread, daemon=True)
        self.upgrade_thread.start()
        
    def run_upgrade_thread(self):
        """升级线程"""
        try:
            success = self.upgrade_flow.run_upgrade()
            self.root.after(0, lambda: self.upgrade_complete(success))
        except Exception as e:
            self.root.after(0, lambda: self.upgrade_error(str(e)))
            
    def upgrade_complete(self, success):
        """升级完成回调"""
        self.set_controls_state(True)
        if success:
            self.label_progress.config(text="升级成功!")
            self.progress_bar['value'] = 100
            messagebox.showinfo("完成", "OTA升级成功!")
        else:
            self.label_progress.config(text="升级失败")
            messagebox.showerror("失败", "OTA升级失败，请查看日志")
            
    def upgrade_error(self, error_msg):
        """升级错误回调"""
        self.set_controls_state(True)
        self.label_progress.config(text="升级出错")
        self.append_log(f"错误: {error_msg}")
        messagebox.showerror("错误", f"升级过程出错: {error_msg}")
        
    def stop_upgrade(self):
        """停止升级"""
        if self.upgrade_flow:
            self.upgrade_flow.stop_upgrade()
            self.append_log("正在停止升级...")
            
    def set_controls_state(self, enabled):
        """设置控件状态"""
        state = tk.NORMAL if enabled else tk.DISABLED
        self.combo_232_port.config(state="readonly" if enabled else tk.DISABLED)
        self.combo_485_port.config(state="readonly" if enabled else tk.DISABLED)
        self.entry_232_baud.config(state=state)
        self.entry_485_baud.config(state=state)
        self.entry_file.config(state=state)
        self.btn_browse.config(state=state)
        self.btn_start.config(state=state)
        self.btn_refresh.config(state=state)
        self.chk_verbose.config(state=state)
        self.btn_stop.config(state=tk.DISABLED if enabled else tk.NORMAL)
        
    def run(self):
        """运行GUI"""
        self.root.mainloop()


if __name__ == "__main__":
    # 启动GUI
    app = OtaUpgradeGUI()
    app.run()