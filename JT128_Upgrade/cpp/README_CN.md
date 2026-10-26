# JT128 升级工具 2.0.0（C++ 源码 + 动态库）

用于 JT128 雷达固件升级。 用户可以将代码集成到自己的系统里。。

本发布包提供 C++ 宿主程序源码与三平台预编译动态库。使用前需将源码编译为可执行文件（每个目标平台编译一次）。

## 随附内容

| 文件 / 目录 | 说明 |
| --- | --- |
| `jt128_upgrade_cli.cpp` | 宿主程序源码（C++11，单文件） |
| `CMakeLists.txt` | 宿主程序构建配置（CMake 3.15+） |
| `lib/windows-x86_64/jt128_upgrade.dll` | Windows x64 升级包解析库 |
| `lib/linux-x86_64/libjt128_upgrade.so` | Linux x64 升级包解析库 |
| `lib/linux-aarch64/libjt128_upgrade.so` | Linux ARM64 升级包解析库 |
| `manifest.json` | 随附文件 SHA-256 校验值 |

## 环境要求

- C++11 编译器：g++ 4.8+ / MinGW-w64 / MSVC 2015 及以上
- CMake 3.15+（可选，也可以直接用编译器命令行）
- 目标平台：Windows x64、Linux x64、Linux ARM64（glibc 2.17+）
- 不支持 32 位系统、原生 Windows ARM64、macOS、musl Linux（例如 Alpine）

## 编译方法

在本包根目录（`jt128_upgrade_cli.cpp` 所在目录）执行。`jt128_api.h` 已随包提供，与源文件同目录，无需额外头文件路径。

Linux x64：

```text
g++ -std=c++11 -O2 -o JT128_Upgrade jt128_upgrade_cli.cpp -ldl
```

Linux ARM64（交叉编译示例，工具链名为 `aarch64-linux-gnu-g++`）：

```text
aarch64-linux-gnu-g++ -std=c++11 -O2 -o JT128_Upgrade jt128_upgrade_cli.cpp -ldl
```

Windows x64（MinGW-w64）：

```text
x86_64-w64-mingw32-g++ -std=c++11 -O2 -o JT128_Upgrade.exe jt128_upgrade_cli.cpp -lws2_32 -static
```

也可以使用 CMake（产物在 `build/<平台>/` 目录下）：

```text
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

## 使用方法

编译完成后，将可执行文件放到包根目录（与 `lib` 目录相邻）：

```text
JT128_Upgrade            <- 编译产物（Windows 为 JT128_Upgrade.exe）
lib/<平台>/libjt128_upgrade.so 或 jt128_upgrade.dll
```

程序从自身所在目录的 `lib/<平台>/` 加载动态库。可整包复制到任意位置，从其他工作目录以绝对路径运行。

```text
./JT128_Upgrade --check
./JT128_Upgrade firmware.patch 192.168.1.201 9347
```

`--check` 检查动态库能否加载及接口版本，不连接雷达。使用 `--help` 查看命令行帮助。

完整命令格式：

```text
JT128_Upgrade <package> [ip] [port] [netcard]
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
| 130 | 用户中断（Ctrl+C） |

## 其他说明

- 程序运行时通过 C ABI（`jt128_api_version` / `jt128_validate` / `jt128_prepare`）调用动态库，不依赖原始工程、Qt 或 LidarUtilities。
- 环境变量 `JT128_REBOOT_WAIT` 可覆盖上传成功后的等待秒数（默认 10），仅供调试与自动化测试使用，正式升级请勿设置。
