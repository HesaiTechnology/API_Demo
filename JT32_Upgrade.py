#!/usr/bin/env python3
# JT32 OTA CLI 整包升级脚本

import argparse
import struct
import sys
import time
from pathlib import Path

import serial
import serial.tools.list_ports

DEFAULT_BAUDRATE_232 = 9600 # RS232 波特率
DEFAULT_BAUDRATE_485 = 6000000 # RS485 波特率

PACKET_DATA_SIZE = 1024 # 每包数据大小
PACKAGE_HEADER_SIZE = 56 # 整包Header大小
COMPONENT_HEADER_SIZE = 1024 # 组件Header大小
PACKAGE_MAGIC = b'HSAI' # 整包标识

COMPONENT_TYPE_MAP = {
    'TMB_FPGA': 0, # 上仓FPGA
    'BMB_FPGA': 1, # 下仓FPGA
    'BMB_APP': 2,  # 下仓MCU
    'TMB_PARAM': 3 # 上仓参数
}

ACK_ERROR_MAP = {
    0: "无错误", 1: "需要重传", 2: "无效包", 3: "Flash写入失败",
    4: "地址计算错误", 5: "无效参数", 6: "无效组件类型",
    7: "上仓FPGA擦除失败", 8: "上仓FPGA传输失败", 9: "上仓FPGA page计数不匹配"
}

TMB_FPGA_ERASE_WAIT_SEC = 15 # Flash擦除等待时间
TMB_FPGA_FIRST_PKT_TIMEOUT = 20 # 第一包超时时间
TMB_FPGA_RETRANSMIT_DELAY = 2 # RETRANSMIT后等待时间

TMB_PARAM_MAGIC = bytes([0x4B, 0x02]) # 参数文件魔数
TMB_PARAM_ADDR_OFFSET = 4 # 校验值地址偏移
TMB_PARAM_QUERY_CMD = bytes.fromhex("24 4C 44 43 4D 44 2C 0F 02 01 08 00 A0 80 FF FF FF FF 00 00 00 00 00 78 56 34 12 52 19 63 26 EE FF")
TMB_PARAM_CHECKSUM_ACK_OFFSET = 18 # ACK中校验值偏移


#-------------------------------------------辅助功能函数--------------------------------------
def calculate_crc32_mpeg2(data):
    """计算CRC-32/MPEG-2校验值"""
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


#-------------------------------------------OTA升级流程--------------------------------------
class OtaUpgradeFlow:
    def __init__(self, package_path, uart_232_port, uart_485_port, baudrate_232, baudrate_485, log_callback=None):
        self.package_path = Path(package_path)
        self.uart_232_port = uart_232_port
        self.uart_485_port = uart_485_port
        self.baudrate_232 = baudrate_232
        self.baudrate_485 = baudrate_485
        self.log_callback = log_callback
        self.uart_232 = None
        self.uart_485 = None
        self.current_partition = None  # 'A' or 'B'
        self.is_running = False
        self.progress_callback = None
        self.verbose_packet = False

        # 解析升级包信息
        self.package_info = self.parse_package_header()

    def log(self, message):
        print(message)
        if self.log_callback:
            self.log_callback(message)

    def parse_package_header(self):
        with open(self.package_path, 'rb') as f:
            data = f.read()

        if len(data) < PACKAGE_HEADER_SIZE:
            raise ValueError("升级包太小")

        magic = data[:4]
        if magic != PACKAGE_MAGIC:
            raise ValueError(f"无效升级包标识: {magic}, 期望: {PACKAGE_MAGIC}")

        total_size = struct.unpack('<I', data[4:8])[0]
        component_info = data[8]  # 位0-4表示组件存在

        components = {}

        # BMB_APP_A
        if component_info & 0x01:
            addr = struct.unpack('<I', data[12:16])[0]
            size = struct.unpack('<I', data[16:20])[0]
            components['BMB_APP_A'] = {'offset': addr, 'size': size}

        # BMB_APP_B
        if component_info & 0x02:
            addr = struct.unpack('<I', data[20:24])[0]
            size = struct.unpack('<I', data[24:28])[0]
            components['BMB_APP_B'] = {'offset': addr, 'size': size}

        # BMB_FPGA
        if component_info & 0x04:
            addr = struct.unpack('<I', data[28:32])[0]
            size = struct.unpack('<I', data[32:36])[0]
            components['BMB_FPGA'] = {'offset': addr, 'size': size}

        # TMB_FPGA
        if component_info & 0x08:
            addr = struct.unpack('<I', data[36:40])[0]
            size = struct.unpack('<I', data[40:44])[0]
            components['TMB_FPGA'] = {'offset': addr, 'size': size}

        # TMB_PARAM
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
        发送CMD指令并等待ACK（带重试）
        Args:
            cmd_bytes: CMD指令
            timeout: 超时时间
            simple_ack: 简单ACK模式（只检查帧头和包尾）
            max_retries: 最大重试次数
        Returns:
            bool: True=成功, False=失败
        """
        for retry in range(max_retries):
            if retry > 0:
                self.log(f"\n  ⚠ CMD指令重试第 {retry}/{max_retries-1} 次...")
                time.sleep(1)  # 重试前等待1秒

            self.log(f"\n>>> 发送CMD指令 (RS232): {bytes_to_hex_str(cmd_bytes)}")

            # 清空接收缓冲区
            if self.uart_485.in_waiting > 0:
                discarded = self.uart_485.read(self.uart_485.in_waiting)
                self.log(f"  清空接收缓冲区: {len(discarded)} 字节")

            # 发送CMD指令
            self.uart_232.write(cmd_bytes)
            self.uart_232.flush()

            # 接收ACK
            recv_buffer = b''
            start_time = time.time()
            ack_found = False


            while time.time() - start_time < timeout:
                if self.uart_485.in_waiting > 0:
                    new_data = self.uart_485.read(self.uart_485.in_waiting)
                    recv_buffer += new_data

                    # 搜索ACK
                    search_start = max(0, len(recv_buffer) - len(new_data) - len(ack_header))
                    ack_pos = recv_buffer.find(ack_header, search_start)

                    if ack_pos >= 0:
                        # 找到ACK标识，检查后面是否有足够的数据
                        ack_data = recv_buffer[ack_pos:]

                        ack_preview_len = min(20, len(ack_data))
                        self.log(f"<<< 找到ACK: {bytes_to_hex_str(ack_data[:ack_preview_len], 60)}")

                        if simple_ack:
                            # 简单ACK模式
                            if len(ack_data) >= 8:
                                data_len = ack_data[7]  # 获取data_len字段
                                expected_ack_len = 7 + 1 + data_len + 1 + 4 + 4 + 2  # 19 + data_len

                                if len(ack_data) >= expected_ack_len:
                                    # 检查包尾是否为 EE FF
                                    end_pos = expected_ack_len - 2
                                    if ack_data[end_pos] == 0xEE and ack_data[end_pos + 1] == 0xFF:
                                        self.log(f"<<< ACK验证成功")
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
                                continue
                        else:
                            # 完整ACK模式：解析详细内容
                            if len(ack_data) >= 12:
                                error_code = ack_data[11]
                                partition_byte = ack_data[10] if len(ack_data) > 10 else 0
                                self.current_partition = 'A' if partition_byte == 0 else 'B'
                                self.log(f"  分区={self.current_partition}, 错误码={error_code}")
                                if error_code == 0:
                                    return True
                                else:
                                    self.log(f"  错误码: {error_code}")
                                    return False
                            else:
                                continue

                time.sleep(0.005)

            self.log(f"<<< CMD ACK超时")


        self.log(f"  ✗ CMD指令发送失败，已重试 {max_retries} 次")
        return False

    def read_component_data(self, component_name):
        """
        读取指定组件的数据
        Args:
            component_name: 组件名称
        Returns:
            bytes: 组件数据
        """
        if component_name not in self.package_info['components']:
            return None

        comp_info = self.package_info['components'][component_name]
        with open(self.package_path, 'rb') as f:
            # offset是相对于数据区的偏移，数据区从Header后开始
            f.seek(PACKAGE_HEADER_SIZE + comp_info['offset'])
            full_data = f.read(comp_info['size'])

            # 上仓参数：无Header
            if component_name == 'TMB_PARAM':
                self.log(f"  [{component_name}] 无Header，直接发送原始数据，大小={len(full_data)}")
                return full_data

            # FPGA组件：跳过组件Header
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
        Args:
            component_type: 组件类型
            total_packets: 总包数
            packet_num: 当前包号
            packet_data: 包数据
        """
        actual_size = len(packet_data)

        # 填充数据
        if actual_size < PACKET_DATA_SIZE:
            padded_data = packet_data + b'\xFF' * (PACKET_DATA_SIZE - actual_size)
        else:
            padded_data = packet_data[:PACKET_DATA_SIZE]

        # 构建包体
        packet_body = b'$LDOTA,' + struct.pack('<BHHH',
            COMPONENT_TYPE_MAP[component_type], total_packets, packet_num, actual_size
        ) + padded_data

        # 计算CRC
        crc = calculate_crc32_mpeg2(padded_data)
        full_packet = packet_body + struct.pack('<I', crc) + b'\xEE\xFF'

        # 显示包内容
        if verbose or packet_num == 1 or packet_num == total_packets:
            self.log(f"\n  发送OTA包 #{packet_num}/{total_packets}")
            self.log(f"  组件类型: {component_type}, CRC: 0x{crc:08X}, 包大小: {len(full_packet)}")

        # 发送数据包
        self.uart_485.write(full_packet)

    def receive_ota_ack(self, timeout=2):
        """
        接收OTA ACK
        Args:
            timeout: 超时时间
        Returns:
            int: 错误码, -1=超时
        """
        ack_header = b'$LDACK,'
        ack_total_len = 20
        recv_buffer = b''
        min_search_pos = 0
        start_time = time.time()

        while time.time() - start_time < timeout:
            if self.uart_485.in_waiting > 0:
                new_data = self.uart_485.read(self.uart_485.in_waiting)
                recv_buffer += new_data

                while True:
                    ack_pos = recv_buffer.find(ack_header, min_search_pos)
                    if ack_pos < 0 or ack_pos + ack_total_len > len(recv_buffer):
                        break

                    ack_data = recv_buffer[ack_pos:ack_pos + ack_total_len]

                    # 校验包尾
                    if ack_data[18] != 0xEE or ack_data[19] != 0xFF:
                        min_search_pos = ack_pos + 1
                        continue

                    # 校验CRC
                    comp_type, total, current, error_code = struct.unpack('<HHHB', ack_data[7:14])
                    recv_crc = struct.unpack('<I', ack_data[14:18])[0]
                    calc_crc = calculate_crc32_mpeg2(ack_data[:14])
                    if recv_crc != calc_crc:
                        self.log(f"  ACK CRC校验失败")
                        min_search_pos = ack_pos + 1
                        continue

                    return error_code

                # 防止缓冲区增长
                if len(recv_buffer) > 8192:
                    trim_size = len(recv_buffer) - 4096
                    recv_buffer = recv_buffer[trim_size:]
                    min_search_pos = max(0, min_search_pos - trim_size)

            time.sleep(0.001)

        return -1  # 超时

    def send_component_packets(self, component_name, component_data, max_retries=5, base_timeout=2):
        """
        发送组件的所有数据包
        Args:
            component_name: 组件名称
            component_data: 组件数据
            max_retries: 最大重试次数
            base_timeout: 基础ACK超时时间
        Returns:
            bool: True=成功, False=失败
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

        # 上仓组件特殊处理
        is_tmb_component = (comp_type in ['TMB_FPGA', 'TMB_PARAM'])
        tmb_erase_started = False  # 标记是否已开始擦除

        if is_tmb_component:
            comp_display_name = "上仓FPGA" if comp_type == 'TMB_FPGA' else "上仓参数"
            self.log(f"  [{comp_display_name}] 第一包将触发Flash擦除")

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

            # 上仓第一包：等待擦除完成
            if is_tmb_component and packet_num == 1:
                max_retries_first_pkt = 10  # 第一包允许更多重试
                first_pkt_timeout = TMB_FPGA_FIRST_PKT_TIMEOUT
                log_prefix = "[上仓FPGA]" if comp_type == 'TMB_FPGA' else "[上仓参数]"


                while retry_count < max_retries_first_pkt and not success:
                    if not self.is_running:
                        return False

                    self.send_ota_packet(comp_type, total_packets, packet_num, packet_data, verbose=self.verbose_packet)
                    error_code = self.receive_ota_ack(timeout=first_pkt_timeout)

                    if error_code == 0:
                        self.log(f"  {log_prefix} 第一包发送成功")
                        if self.progress_callback:
                            self.progress_callback(component_name, packet_num, total_packets)
                        else:
                            self.log(f"{component_name}: {packet_num}/{total_packets}")
                        success = True
                        tmb_erase_started = True
                    elif error_code == 1:  # RETRANSMIT - MCU正在擦除Flash
                        if not tmb_erase_started:
                            self.log(f"  {log_prefix} MCU正在擦除Flash，等待重发...")
                            tmb_erase_started = True
                        else:
                            self.log(f"  {log_prefix} 等待擦除完成...")
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
                    self.log(f"  {log_prefix} 第一包重试耗尽")
                    return False
                continue  # 继续下一包

            # 普通包处理
            log_prefix = "[上仓FPGA]" if comp_type == 'TMB_FPGA' else ("[上仓参数]" if comp_type == 'TMB_PARAM' else "")

            while retry_count < max_retries and not success:
                if not self.is_running:
                    self.log("升级已取消")
                    return False

                self.send_ota_packet(comp_type, total_packets, packet_num, packet_data, verbose=self.verbose_packet)

                # 上仓组件使用更长超时
                if is_tmb_component:
                    # 每包1024B = 4个page，每个page需要传输时间
                    current_timeout = base_timeout * 2
                else:
                    # 指数退避
                    current_timeout = base_timeout * (1.5 ** retry_count)

                error_code = self.receive_ota_ack(timeout=current_timeout)

                if error_code == 0:
                    # 进度输出
                    if packet_num % 10 == 0 or packet_num == total_packets:
                        self.log(f"  进度: {packet_num}/{total_packets} ({100*packet_num//total_packets}%)")
                    # 更新进度条
                    if self.progress_callback:
                        self.progress_callback(component_name, packet_num, total_packets)
                    else:
                        self.log(f"{component_name}: {packet_num}/{total_packets}")
                    success = True
                elif error_code == -1:  # 超时
                    retry_count += 1
                    retry_delay = 0.5 if is_tmb_component else 0.2 * (2 ** (retry_count - 1))
                    self.log(f"  ⚠ 包 {packet_num} ACK超时 (尝试 {retry_count}/{max_retries})")
                    time.sleep(retry_delay)
                elif error_code == 1:  # 需要重传
                    retry_count += 1
                    # 上仓RETRANSMIT需更长等待
                    if is_tmb_component:
                        self.log(f"  ⚠ {log_prefix} 包 {packet_num} 需要重传 (尝试 {retry_count}/{max_retries})")
                        time.sleep(1)
                    else:
                        retry_delay = 0.2 * (2 ** (retry_count - 1))
                        self.log(f"  ⚠ 包 {packet_num} 需要重传 (尝试 {retry_count}/{max_retries})")
                        time.sleep(retry_delay)
                elif error_code in [8, 9]:
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

    def run_upgrade(self) -> bool:
        self.is_running = True
        if not self.open_serial_ports():
            self.is_running = False
            return False

        try:
            # 发送升级CMD
            self.log("="*50)
            self.log("步骤1: 发送升级指令...")
            upgrade_cmd = bytes.fromhex("24 4C 44 43 4D 44 2C 03 03 04 00 78 56 34 12 F9 5E 8C 52 EE FF")
            if not self.send_cmd_and_wait_ack(upgrade_cmd, timeout=15, max_retries=5):
                self.log("升级指令发送失败")
                return False

            if not self.is_running:
                return False

            # 确定APP组件
            app_component = None
            if self.current_partition == 'A':
                app_component = 'BMB_APP_B'  # 当前在A区，发送B区APP
            else:
                app_component = 'BMB_APP_A'  # 当前在B区，发送A区APP

            self.log(f"\nMCU当前在 {self.current_partition} 区，将发送 {app_component}")

            # 发送APP组件
            if app_component in self.package_info['components']:
                app_data = self.read_component_data(app_component)
                if not self.send_component_packets(app_component, app_data):
                    self.log(f"{app_component} 发送失败")
                    return False
            else:
                self.log(f"警告: 升级包中不包含 {app_component}")

            if not self.is_running:
                return False

            # 发送下仓FPGA
            if 'BMB_FPGA' in self.package_info['components']:
                self.log("\n" + "="*50)
                self.log("步骤4a: 发送下仓FPGA...")
                fpga_data = self.read_component_data('BMB_FPGA')
                if not self.send_component_packets('BMB_FPGA', fpga_data):
                    self.log("BMB_FPGA 发送失败")
                    return False

            if not self.is_running:
                return False

            # 发送上仓FPGA
            if 'TMB_FPGA' in self.package_info['components']:
                self.log("\n" + "="*50)
                self.log("步骤4b: 发送上仓FPGA...")
                fpga_data = self.read_component_data('TMB_FPGA')
                if not self.send_component_packets('TMB_FPGA', fpga_data):
                    self.log("TMB_FPGA 发送失败")
                    return False

            if not self.is_running:
                return False

            # 发送上仓参数
            if 'TMB_PARAM' in self.package_info['components']:
                self.log("\n" + "="*50)
                self.log("步骤4c: 发送上仓参数...")
                param_data = self.read_component_data('TMB_PARAM')
                if not self.send_component_packets('TMB_PARAM', param_data):
                    self.log("TMB_PARAM 发送失败")
                    return False

            if not self.is_running:
                return False

            self.log("\n" + "="*50)
            self.log("数据发送完成。请手动重启设备以完成升级。")
            self.log("="*50)
            return True

        finally:
            self.is_running = False
            self.close_serial_ports()

    def stop_upgrade(self):
        self.is_running = False

    def get_component_version_from_package(self, component_name):
        """
        从升级包提取组件版本信息
        Returns:
            str: 版本文件名
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
        发送版本查询指令并返回版本数据
        Args:
            version_type: 0=下仓FPGA, 1=上仓FPGA, 2=下仓MCU
            max_retries: 最大重试次数
        Returns:
            bytes: 24字节版本数据, None=失败
        """
        # 版本查询指令
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
            self.log(f"  >>> 发送版本查询指令")

            # 清空接收缓冲区
            if self.uart_485.in_waiting > 0:
                self.uart_485.read(self.uart_485.in_waiting)

            # 发送指令
            self.uart_232.write(cmd)

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
                        if len(ack_data) >= 35:
                            version_data = ack_data[11:35]  # 24字节版本数据
                            self.log(f"  版本数据: {bytes_to_hex_str(version_data, 60)}")
                            return version_data

                time.sleep(0.01)

            self.log(f"  <<< 版本查询超时 (尝试 {retry+1}/{max_retries})")

        self.log(f"  ✗ 版本查询失败，已重试 {max_retries} 次")
        return None

    def validate_param_file_format(self, param_data):
        """
        验证参数文件格式
        Returns:
            (bool, str): (是否有效, 错误信息)
        """
        if len(param_data) < 8:
            return False, "参数文件太小，无法验证"

        magic = param_data[0:2]
        if magic != TMB_PARAM_MAGIC:
            return False, f"警告: 不是有效的参数文件! 前2字节为 {bytes_to_hex_str(magic, 10)}，期望 4B 02"

        return True, None

    def get_param_checksum_from_file(self, param_data):
        """
        从参数文件提取校验值
        Returns:
            (bytes, str): (校验值, 错误信息)
        """
        if len(param_data) < 8:
            return None, "参数文件太小，无法提取校验值地址"

        addr_bytes = param_data[TMB_PARAM_ADDR_OFFSET:TMB_PARAM_ADDR_OFFSET + 4]
        checksum_addr = struct.unpack('<I', addr_bytes)[0]

        self.log(f"  校验值地址: 0x{checksum_addr:08X}")

        checksum_offset = checksum_addr - 4

        if checksum_offset < 0 or checksum_offset + 4 > len(param_data):
            return None, f"校验值偏移 0x{checksum_offset:08X} 超出文件范围 (文件大小: {len(param_data)})"

        checksum_bytes = param_data[checksum_offset:checksum_offset + 4]
        self.log(f"  校验值: {bytes_to_hex_str(checksum_bytes, 10)}")

        return checksum_bytes, None

    def send_param_checksum_query(self, timeout=10, max_retries=3):
        """
        发送参数校验值查询指令
        Returns:
            bytes: 4字节校验值, None=失败
        """
        ack_header = b'$LDACK,'

        for retry in range(max_retries):
            if retry > 0:
                self.log(f"  重试 {retry}/{max_retries}...")
                time.sleep(1)

            # 清空接收缓冲区
            if self.uart_485.in_waiting > 0:
                self.uart_485.read(self.uart_485.in_waiting)

            # 发送指令
            self.log(">>> 发送参数校验值查询指令")
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
                        if len(ack_data) >= TMB_PARAM_CHECKSUM_ACK_OFFSET + 4:
                            checksum_bytes = ack_data[TMB_PARAM_CHECKSUM_ACK_OFFSET:TMB_PARAM_CHECKSUM_ACK_OFFSET + 4]
                            self.log(f"  ACK校验值: {bytes_to_hex_str(checksum_bytes, 10)}")
                            return checksum_bytes

                time.sleep(0.01)

            self.log(f"  <<< 参数校验值查询超时")

        self.log(f"  ✗ 参数校验值查询失败，已重试 {max_retries} 次")
        return None

    def verify_param_checksum(self):
        """
        验证上仓参数校验值
        Returns:
            bool: True=通过, False=失败
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

        # 验证文件格式
        is_valid, warning = self.validate_param_file_format(param_data)
        if not is_valid:
            self.log(f"  {warning}")
            self.log("  跳过校验值验证")
            return True  # 不是参数文件，跳过验证但不算失败

        self.log(f"  参数文件格式有效")

        # 提取校验值
        expected_checksum, error = self.get_param_checksum_from_file(param_data)
        if error:
            self.log(f"  ✗ {error}")
            return False

        # 查询设备校验值
        actual_checksum = self.send_param_checksum_query()
        if actual_checksum is None:
            self.log(f"  ✗ 无法获取设备校验值")
            return False

        # 比较校验值
        if expected_checksum == actual_checksum:
            self.log(f"  校验值匹配成功")
            return True
        else:
            self.log(f"  校验值不匹配")
            return False

    def verify_component_versions(self):
        """
        验证已升级组件的版本
        Returns:
            bool: True=全部通过, False=有失败
        """
        all_passed = True

        # 确定需要验证的组件
        components_to_verify = []

        # 下仓FPGA
        if 'BMB_FPGA' in self.package_info['components']:
            components_to_verify.append(('下仓FPGA', 0, 'BMB_FPGA'))

        # 下仓MCU
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
            self.log(f"  预期版本: {expected_version}")

            # 查询实际版本
            actual_version_data = self.send_version_query(query_type)

            if actual_version_data is None:
                self.log(f"  ✗ 版本查询失败")
                all_passed = False
                continue

            # 解析并比较版本数据
            if query_type == 2:  # MCU版本是字符串
                # 找到字符串结尾
                str_end = 24
                for i, b in enumerate(actual_version_data):
                    if b == 0x00:
                        str_end = i
                        break
                try:
                    actual_version = actual_version_data[:str_end].decode('utf-8')
                except:
                    actual_version = actual_version_data[:str_end].hex()

                self.log(f"  设备版本: {actual_version}")

                # 检查版本是否匹配
                if expected_version and actual_version and actual_version in expected_version:
                    self.log(f"  版本匹配成功")
                else:
                    self.log(f"  版本不匹配: {actual_version}")
                    all_passed = False
            else:  # FPGA版本是8字节二进制，需要解码为ASCII字符串
                # 提取8字节版本数据
                version_bytes = actual_version_data[:8]

                # 解码为ASCII字符串
                str_end = 8
                for i, b in enumerate(version_bytes):
                    if b == 0x00 or b < 0x20 or b > 0x7E:
                        str_end = i
                        break

                try:
                    actual_version = version_bytes[:str_end].decode('ascii')
                except:
                    # 解码失败，使用十六进制
                    actual_version = version_bytes[:str_end].hex()

                self.log(f"  设备版本: {actual_version}")

                # 检查版本是否匹配
                if expected_version and actual_version and actual_version in expected_version:
                    self.log(f"  版本匹配成功")
                else:
                    self.log(f"  版本不匹配: {actual_version}")
                    all_passed = False

        # 验证上仓参数校验值（如果存在）
        if 'TMB_PARAM' in self.package_info['components']:
            if not self.verify_param_checksum():
                all_passed = False

        return all_passed


#-------------------------------------------CLI入口----------------------------------------
def build_parser():
    p = argparse.ArgumentParser(description="JT32 OTA full-package upgrade (CLI)")
    p.add_argument("--package", help="整包固件路径")
    p.add_argument("--cmd-port", help="RS232 CMD 串口")
    p.add_argument("--ota-port", help="RS485 OTA 串口")
    p.add_argument("--cmd-baud", type=int, default=DEFAULT_BAUDRATE_232)
    p.add_argument("--ota-baud", type=int, default=DEFAULT_BAUDRATE_485)
    p.add_argument("--list-ports", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def list_serial_ports():
    for port in serial.tools.list_ports.comports():
        print(port)
    return 0


#-----------------------------------main--------------------------------------------
def main() -> int:
    args = build_parser().parse_args()
    if args.list_ports:
        return list_serial_ports()
    if not args.package or not args.cmd_port or not args.ota_port:
        print("错误: 升级需要 --package --cmd-port --ota-port", file=sys.stderr)
        return 1
    package = Path(args.package)
    if not package.is_file():
        print(f"错误: 升级包不存在: {package}", file=sys.stderr)
        return 1
    try:
        flow = OtaUpgradeFlow(
            package_path=str(package),
            uart_232_port=args.cmd_port,
            uart_485_port=args.ota_port,
            baudrate_232=args.cmd_baud,
            baudrate_485=args.ota_baud,
            log_callback=None,
        )
        flow.verbose_packet = args.verbose

        def _progress(name, current, total):
            flow.log(f"进度 {name}: {current}/{total}")

        flow.progress_callback = _progress
        ok = flow.run_upgrade()
        return 0 if ok else 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
