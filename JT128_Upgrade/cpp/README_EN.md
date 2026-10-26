# JT128 Upgrade Tool 2.0.0 (C++ Source + Dynamic Library)

Developed for upgrading JT128 LiDAR firmware. User can intergrate the code into their system.

This release package provides the C++ host program source code and prebuilt dynamic libraries for three platforms. User must compile the source into an executable before use (compile once per target platform).

## Included Files

| File / Directory | Description |
| --- | --- |
| `jt128_upgrade_cli.cpp` | Host program source code (C++11, single file) |
| `CMakeLists.txt` | Host program build configuration (CMake 3.15+) |
| `lib/windows-x86_64/jt128_upgrade.dll` | Windows x64 upgrade-package parsing library |
| `lib/linux-x86_64/libjt128_upgrade.so` | Linux x64 upgrade-package parsing library |
| `lib/linux-aarch64/libjt128_upgrade.so` | Linux ARM64 upgrade-package parsing library |
| `manifest.json` | SHA-256 checksums of the bundled files |

## Requirements

- C++11 compiler: g++ 4.8+ / MinGW-w64 / MSVC 2015 or later
- CMake 3.15+ (optional; you can also build straight from the compiler command line)
- Target platforms: Windows x64, Linux x64, Linux ARM64 (glibc 2.17+)
- 32-bit systems, native Windows ARM64, macOS, and musl Linux (e.g. Alpine) are not supported

## Building

Run the commands below in the package root directory (the directory containing `jt128_upgrade_cli.cpp`). `jt128_api.h` ships with the package in the same directory as the source file, so no additional include path is needed.

Linux x64:

```text
g++ -std=c++11 -O2 -o JT128_Upgrade jt128_upgrade_cli.cpp -ldl
```

Linux ARM64 (cross-compilation example; the toolchain is named `aarch64-linux-gnu-g++`):

```text
aarch64-linux-gnu-g++ -std=c++11 -O2 -o JT128_Upgrade jt128_upgrade_cli.cpp -ldl
```

Windows x64 (MinGW-w64):

```text
x86_64-w64-mingw32-g++ -std=c++11 -O2 -o JT128_Upgrade.exe jt128_upgrade_cli.cpp -lws2_32 -static
```

You can also use CMake (the output goes to the `build/<platform>/` directory):

```text
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
```

## Usage

After building, place the executable in the package root directory (next to the `lib` directory):

```text
JT128_Upgrade            <- build output (JT128_Upgrade.exe on Windows)
lib/<platform>/libjt128_upgrade.so or jt128_upgrade.dll
```

The program loads the dynamic library from `lib/<platform>/` next to its own executable. You can copy the whole package anywhere and run it by absolute path from any working directory.

```text
./JT128_Upgrade --check
./JT128_Upgrade firmware.patch 192.168.1.201 9347
```

`--check` verifies that the dynamic library can be loaded and that the interface version matches; it does not connect to the LiDAR. Use `--help` to view the command-line help.

Full command format:

```text
JT128_Upgrade <package> [ip] [port] [netcard]
```

| Argument | Description | Default |
| --- | --- | --- |
| `package` | Firmware file path, required | None |
| `ip` | LiDAR IP address | `192.168.1.201` |
| `port` | PTC TCP port, range 1-65535 | `9347` |
| `netcard` | Linux network interface name, or use the default route | `default` |

Quote the file path if it contains spaces. You normally do not need to specify a network interface; when you do specify a Linux interface name, the system may require additional network privileges. On Windows, use `default`.

The tool reads the device information and automatically selects the matching upgrade content. After the upload succeeds it waits 10 seconds and then sends the reboot command. It stops if the upgrade is interrupted, if the device rejects it, or if the firmware is incompatible; a failed upload never triggers an automatic reboot.

After the upgrade completes, wait for the device to come back online and check the firmware version. Exit code 0 means both the upload and the reboot command were acknowledged; it does not mean that the firmware version after the reboot has been verified.

## Exit Codes

| Exit Code | Description |
| --- | --- |
| 0 | Environment check succeeded, or the upload and reboot command succeeded |
| 1 | The firmware file could not be read or failed validation |
| 2 | Command-line argument or network connection failure |
| 3 | Failed to query the device information |
| 4 | The device information is not supported or the firmware does not match |
| 5 | Upload failed |
| 6 | The reboot command was not acknowledged |
| 7 | Unsupported system architecture, missing library, the library could not be loaded, or the interface does not match |
| 130 | Interrupted by the user (Ctrl+C) |

## Additional Notes

- At runtime the program calls the dynamic library through a C ABI (`jt128_api_version` / `jt128_validate` / `jt128_prepare`); it does not depend on the original project, Qt, or LidarUtilities.
- The environment variable `JT128_REBOOT_WAIT` overrides the wait time in seconds after a successful upload (default 10). It is intended for debugging and automated testing only; do not set it for production upgrades.
