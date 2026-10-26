# JT128 Upgrade Tool 2.0.0

Developed for upgrading JT128 LiDAR firmware. User can intergrate the code into their system. 

## Supported Environments

Requires 64-bit Python 3.6 or a higher Python 3 release, and the Python version in use must be supported by the host operating system. Only the Python standard library and the bundled dynamic libraries are used; no additional Python packages or compilers need to be installed.

| Platform & Architecture | Requirement | Bundled Library |
| --- | --- | --- |
| Windows x86_64 | Target compatibility: Windows 7 SP1 or later; use x64 Python | `lib/windows-x86_64/jt128_upgrade.dll` |
| Linux x86_64 | glibc 2.17 or later | `lib/linux-x86_64/libjt128_upgrade.so` |
| Linux ARM64 / aarch64 | glibc 2.17 or later | `lib/linux-aarch64/libjt128_upgrade.so` |

32-bit systems or Python, native Windows ARM64, macOS, and musl-based Linux (e.g. Alpine) are not supported. For WSL, choose the Linux library.

## Usage

Extract the full archive and enter the `JT128_Upgrade` directory before executing the commands below.

Windows:

```text
python JT128_Upgrade.py --check
python JT128_Upgrade.py firmware.patch 192.168.1.201 9347
```

Linux:

```text
python3 JT128_Upgrade.py --check
python3 JT128_Upgrade.py firmware.patch 192.168.1.201 9347
```

`--check` verifies that the local dynamic library loads correctly and that its interface version matches, without connecting to the LiDAR. Run with `--help` to view the command-line help.

Full command syntax:

```text
python JT128_Upgrade.py <package> [ip] [port] [netcard]
```

| Argument | Description | Default |
| --- | --- | --- |
| `package` | Path to the firmware file; required | None |
| `ip` | LiDAR IP address | `192.168.1.201` |
| `port` | PTC TCP port, range 1–65535 | `9347` |
| `netcard` | Linux network interface name, or use the default route | `default` |

Enclose the file path in quotes if it contains spaces. Specifying a network interface is generally not necessary; when a Linux interface is specified, the system may require additional network privileges. On Windows, use `default`.

The tool reads device information and automatically selects the appropriate upgrade payload. After the upload succeeds, the tool waits 10 seconds before sending the reboot command. The process stops if the upgrade is interrupted, if the device rejects it, or if the firmware is incompatible; the device is not rebooted automatically on upload failure.

After the upgrade completes, wait for the device to come back online and verify the firmware version. Exit code 0 means the upload and the reboot command have both been acknowledged; it does not mean the post-reboot firmware version has been confirmed.

## Directory Requirements

Always keep `JT128_Upgrade.py` adjacent to the `lib` directory and retain the dynamic library for the platform in use. The whole directory may be placed at any location, and the script may be launched from another working directory using its absolute path.

Execution does not depend on the original project, Qt, or LidarUtilities. `manifest.json` provides the SHA-256 checksums for the bundled files.

## Exit Codes

| Exit Code | Description |
| --- | --- |
| 0 | Environment check passed, or upload and reboot command succeeded |
| 1 | Firmware file cannot be read or fails validation |
| 2 | Command-line arguments or network connection failed |
| 3 | Device information query failed |
| 4 | Device information not supported or firmware mismatch |
| 5 | Upload failed |
| 6 | Reboot command not acknowledged |
| 7 | System architecture not supported, library missing, cannot load, or interface mismatch |
| 130 | Interrupted by the user |
