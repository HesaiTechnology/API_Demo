# JT128 升级工具 2.0.0

用于 JT128 雷达固件升级。 用户可以将代码集成到自己的系统里。

## 环境范围

需要 64 位 Python 3.6 或更高版本的 Python 3，且该 Python 版本须支持所在操作系统。仅使用 Python 标准库和随附动态库，无需安装额外 Python 包或编译器。

| 系统与架构 | 环境要求 | 随附库 |
| --- | --- | --- |
| Windows x86_64 | 兼容目标：Windows 7 SP1 及以上；使用 x64 Python | `lib/windows-x86_64/jt128_upgrade.dll` |
| Linux x86_64 | glibc 2.17 及以上 | `lib/linux-x86_64/libjt128_upgrade.so` |
| Linux ARM64 / aarch64 | glibc 2.17 及以上 | `lib/linux-aarch64/libjt128_upgrade.so` |

不支持 32 位系统或 Python、原生 Windows ARM64、macOS、musl Linux（例如 Alpine）。WSL 按 Linux 环境选择库。

## 使用方法

解压完整压缩包，进入 `JT128_Upgrade` 目录后执行。

Windows：

```text
python JT128_Upgrade.py --check
python JT128_Upgrade.py firmware.patch 192.168.1.201 9347
```

Linux：

```text
python3 JT128_Upgrade.py --check
python3 JT128_Upgrade.py firmware.patch 192.168.1.201 9347
```

`--check` 检查本机动态库能否加载及接口版本，不连接雷达。使用 `--help` 查看命令行帮助。

完整命令格式：

```text
python JT128_Upgrade.py <package> [ip] [port] [netcard]
```

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `package` | 固件文件路径，必填 | 无 |
| `ip` | 雷达 IP 地址 | `192.168.1.201` |
| `port` | PTC TCP 端口，范围 1～65535 | `9347` |
| `netcard` | Linux 网卡名，或使用默认路由 | `default` |

文件路径包含空格时请加引号。一般不需要指定网卡；指定 Linux 网卡名时，系统可能要求额外网络权限。Windows 请使用 `default`。

工具读取设备信息并自动选择适配的升级内容。上传成功后等待 10 秒，再发送重启命令。升级中断、设备拒绝或固件不兼容时停止；上传失败不会自动重启。

升级完成后，请等待设备重新上线并检查固件版本。退出码 0 表示上传及重启命令均已确认，不代表已经确认重启后的固件版本。

## 目录要求

始终保持 `JT128_Upgrade.py` 与 `lib` 目录相邻，并保留对应平台的动态库。可将整个目录复制到任意位置，也可从其他工作目录使用脚本的绝对路径启动。

运行不依赖原始工程、Qt 或 LidarUtilities。`manifest.json` 提供随附文件的 SHA-256 校验值。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 环境检查成功，或上传及重启命令成功 |
| 1 | 固件文件无法读取或校验失败 |
| 2 | 命令行参数或网络连接失败 |
| 3 | 设备信息查询失败 |
| 4 | 设备信息不受支持或固件不匹配 |
| 5 | 上传失败 |
| 6 | 重启命令未确认 |
| 7 | 系统架构不受支持、库缺失、无法加载或接口不匹配 |
| 130 | 用户中断 |
