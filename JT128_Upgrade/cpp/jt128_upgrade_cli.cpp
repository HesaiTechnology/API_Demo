// Standalone JT128 upgrade CLI (C++11, no swap).
//
// Drop-in replacement for Scripts/JT128_Upgrade.py: same command line, same
// PTC protocol, same exit codes. The supplied native library is loaded at
// runtime from lib/<platform>/ next to this executable.
//
// Usage: JT128_Upgrade <package.patch> [ip] [port] [netcard]
// Check installation without connecting: JT128_Upgrade --check
//
// Exit 0 means the upload and reboot command were acknowledged; it does not
// verify which firmware is running after reboot.

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#else
#include <arpa/inet.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <net/if.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#endif

#include "jt128_api.h"

#ifndef JT128_CLI_VERSION
#define JT128_CLI_VERSION "jt128_upgrade_cpp_2.0.0"
#endif

namespace jt128cli {

// ------------------------------ 平台封装 ----------------------------------
#ifdef _WIN32
typedef SOCKET socket_t;
const socket_t kInvalidSocket = INVALID_SOCKET;
#else
typedef int socket_t;
const socket_t kInvalidSocket = -1;
#endif

void CloseSocket(socket_t fd) {
#ifdef _WIN32
  closesocket(fd);
#else
  ::close(fd);
#endif
}

void SetNonBlocking(socket_t fd) {
#ifdef _WIN32
  u_long mode = 1;
  ioctlsocket(fd, FIONBIO, &mode);
#else
  int flags = ::fcntl(fd, F_GETFL, 0);
  if (flags >= 0) ::fcntl(fd, F_SETFL, flags | O_NONBLOCK);
#endif
}

void SetBlocking(socket_t fd) {
#ifdef _WIN32
  u_long mode = 0;
  ioctlsocket(fd, FIONBIO, &mode);
#else
  int flags = ::fcntl(fd, F_GETFL, 0);
  if (flags >= 0) ::fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);
#endif
}

void SetSocketTimeouts(socket_t fd, double seconds) {
#ifdef _WIN32
  DWORD ms = static_cast<DWORD>(seconds * 1000.0);
  ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char *>(&ms), sizeof(ms));
  ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char *>(&ms), sizeof(ms));
#else
  struct timeval tv;
  tv.tv_sec = static_cast<long>(seconds);
  tv.tv_usec = static_cast<long>((seconds - static_cast<double>(tv.tv_sec)) * 1e6);
  ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char *>(&tv), sizeof(tv));
  ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char *>(&tv), sizeof(tv));
#endif
}

// 等待 socket 变为可写（非阻塞 connect 用）；返回 1=可写 0=超时 -1=错误
int PollWritable(socket_t fd, int timeoutMs) {
#ifdef _WIN32
  fd_set writer;
  FD_ZERO(&writer);
  FD_SET(fd, &writer);
  struct timeval tv;
  tv.tv_sec = timeoutMs / 1000;
  tv.tv_usec = (timeoutMs % 1000) * 1000;
  return ::select(0, NULL, &writer, NULL, &tv);
#else
  struct pollfd pfd;
  pfd.fd = fd;
  pfd.events = POLLOUT;
  pfd.revents = 0;
  const int result = ::poll(&pfd, 1, timeoutMs);
  if (result <= 0) return result;  // 0=超时 -1=错误
  return (pfd.revents & POLLOUT) ? 1 : -1;
#endif
}

std::string SocketErrorText(int code) {
#ifdef _WIN32
  char buffer[256] = {0};
  FormatMessageA(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, NULL,
                 static_cast<DWORD>(code), 0, buffer, sizeof(buffer), NULL);
  std::string text(buffer);
  while (!text.empty() && (text.back() == '\n' || text.back() == '\r' || text.back() == ' ')) {
    text.pop_back();
  }
  return text.empty() ? ("socket error " + std::to_string(code)) : text;
#else
  return std::strerror(code);
#endif
}

int LastSocketError() {
#ifdef _WIN32
  return WSAGetLastError();
#else
  return errno;
#endif
}

const int kWouldBlock = 
#ifdef _WIN32
    WSAEWOULDBLOCK;
#else
    EWOULDBLOCK;
#endif

const int kInProgress =
#ifdef _WIN32
    WSAEWOULDBLOCK;
#else
    EINPROGRESS;
#endif

const int kTimedOut =
#ifdef _WIN32
    WSAETIMEDOUT;
#else
    EAGAIN;
#endif

const int kInterrupted =
#ifdef _WIN32
    WSAEINTR;
#else
    EINTR;
#endif

// 中断时统一上报的文案；结果行可能已经打印过，主流程据此去重。
const char kInterruptedMessage[] = "Upgrade interrupted";

double NowMonotonic() {
  return std::chrono::duration<double>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

// ------------------------------ 中断处理 ----------------------------------
volatile std::sig_atomic_t g_interrupt = 0;

void HandleSigint(int /*signal*/) { g_interrupt = 1; }

bool Interrupted() { return g_interrupt != 0; }

// 分段睡眠；返回 false 表示被 Ctrl+C 打断
bool InterruptibleSleep(double seconds) {
  const double kSliceSeconds = 0.05;
  const double deadline = NowMonotonic() + seconds;
  while (!Interrupted()) {
    double remaining = deadline - NowMonotonic();
    if (remaining <= 0.0) return true;
    double slice = std::min(remaining, kSliceSeconds);
#ifdef _WIN32
    ::Sleep(static_cast<DWORD>(slice * 1000.0));
#else
    struct timespec req;
    req.tv_sec = static_cast<time_t>(slice);
    req.tv_nsec = static_cast<long>((slice - static_cast<double>(req.tv_sec)) * 1e9);
    ::nanosleep(&req, NULL);
#endif
  }
  return false;
}

// ------------------------------ 日志输出 ----------------------------------
void PrintTimestamped(const char *color, const char *tag, const std::string &text,
                      bool overLine) {
  char stamp[32] = {0};
  std::time_t now = std::time(NULL);
  std::strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", std::localtime(&now));
  std::printf("%s%s[%s] \033[0m%s%c", color, stamp, tag, text.c_str(),
              overLine ? '\r' : '\n');
  std::fflush(stdout);
}

void PrintInfo(const std::string &text, bool overLine = false) {
  PrintTimestamped("\033[32m", "INFO", text, overLine);
}

void PrintError(const std::string &text, bool overLine = false) {
  PrintTimestamped("\033[31m", "ERROR", text, overLine);
}

// ------------------------------ 二进制工具 --------------------------------
void AppendU32BE(std::vector<uint8_t> *out, uint32_t value) {
  out->push_back(static_cast<uint8_t>(value >> 24));
  out->push_back(static_cast<uint8_t>(value >> 16));
  out->push_back(static_cast<uint8_t>(value >> 8));
  out->push_back(static_cast<uint8_t>(value));
}

uint32_t ReadU32BE(const uint8_t *data) {
  return (static_cast<uint32_t>(data[0]) << 24) |
         (static_cast<uint32_t>(data[1]) << 16) |
         (static_cast<uint32_t>(data[2]) << 8) |
         static_cast<uint32_t>(data[3]);
}

// PTC 块 CRC：MSB-first / poly 0x04C11DB7 / init=FFFFFFFF / 无 xorout。
// 与 SDK Crc32::CRCCalc 及 Python 版 calCrc32 位级一致，不能用 zlib 代替。
uint32_t CalCrc32(const uint8_t *data, size_t length) {
  uint32_t crc = 0xFFFFFFFFu;
  for (size_t i = 0; i < length; ++i) {
    crc ^= static_cast<uint32_t>(data[i]) << 24;
    for (int bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x80000000u) ? ((crc << 1) ^ 0x04C11DB7u) : (crc << 1);
    }
  }
  return crc;
}

std::string CastBytes2HexStrs(const uint8_t *data, size_t length) {
  static const char *kDigits = "0123456789abcdef";
  std::string text;
  text.reserve(length * 3);
  for (size_t i = 0; i < length; ++i) {
    if (i) text.push_back(' ');
    text.push_back(kDigits[data[i] >> 4]);
    text.push_back(kDigits[data[i] & 0x0F]);
  }
  return text;
}

// ------------------------------ PTC 帧 ------------------------------------
const uint8_t kPtcId0 = 0x47;
const uint8_t kPtcId1 = 0x74;
const size_t kFrameHeaderSize = 8;
const uint32_t kMaxResponseBytes = 1024u * 1024u;
const size_t kUploadBlockSize = 1024;
const size_t kBlockHeaderSize = 16;

const uint8_t PTC_COMMAND_GET_INVENTORY_INFO = 0x07;
const uint8_t PTC_COMMAND_REBOOT = 0x10;
const uint8_t PTC_COMMAND_FOTA_REQUEST_UPGRADE = 0x83;

std::vector<uint8_t> MakeFrame(uint8_t wireCmd, const uint8_t *payload, size_t length) {
  std::vector<uint8_t> frame;
  frame.reserve(kFrameHeaderSize + length);
  frame.push_back(kPtcId0);
  frame.push_back(kPtcId1);
  frame.push_back(wireCmd);
  frame.push_back(0);
  AppendU32BE(&frame, static_cast<uint32_t>(length));
  frame.insert(frame.end(), payload, payload + length);
  return frame;
}

// 升级块 payload：BlockHeader（crc_valid=1, index 从 1 起, 总块数, 块 CRC32）+ 数据
std::vector<uint8_t> MakeBlockPayload(uint32_t index, uint32_t total,
                                      const uint8_t *chunk, size_t chunkLength) {
  std::vector<uint8_t> payload;
  payload.reserve(kBlockHeaderSize + chunkLength);
  AppendU32BE(&payload, 1);
  AppendU32BE(&payload, index);
  AppendU32BE(&payload, total);
  AppendU32BE(&payload, CalCrc32(chunk, chunkLength));
  payload.insert(payload.end(), chunk, chunk + chunkLength);
  return payload;
}

// ------------------------------ PTC 结果 ----------------------------------
class Result {
 public:
  explicit Result(uint8_t command)
      : command_(command),
        beginTime_(NowMonotonic()),
        endTime_(beginTime_),
        reqBlock_(kUploadBlockSize) {}

  uint8_t command() const { return command_; }
  int innerCode() const { return innerCode_; }
  uint32_t errCode() const { return errCode_; }
  size_t reqTotal() const { return reqTotal_; }
  size_t repTotal() const { return repTotal_; }
  size_t curSend() const { return curSend_; }
  size_t curRecv() const { return curRecv_; }
  const std::vector<uint8_t> &payload() const { return payload_; }
  const std::string &errorMessage() const { return errorMessage_; }

  void set_innerCode(int value) { innerCode_ = value; }
  void set_errCode(uint32_t value) { errCode_ = value; }
  void set_reqTotal(size_t value) { reqTotal_ = value; }
  void set_reqBlock(size_t value) { reqBlock_ = value; }
  void set_errorMessage(const std::string &value) { errorMessage_ = value; }
  void set_payload(std::vector<uint8_t> value) { payload_ = std::move(value); }
  void set_repTotal(size_t value) { repTotal_ = value; }
  void set_curRecv(size_t value) { curRecv_ = value; }

  void MarkSent() { curSend_ = reqTotal_; }
  void AddCurSend(size_t value) { curSend_ += value; }

  // 采纳入库单次 exchange 的响应状态（对应 Python sendFile2 中逐帧字段拷贝）。
  void AdoptFrameState(const Result &reply) {
    innerCode_ = reply.innerCode_;
    errCode_ = reply.errCode_;
    errorMessage_ = reply.errorMessage_;
    payload_ = reply.payload_;
    repTotal_ = reply.repTotal_;
    curRecv_ = reply.curRecv_;
  }

  void Finish() {
    endTime_ = NowMonotonic();
    finished_ = true;
  }

  double WasteTimeMs() const { return (endTime_ - beginTime_) * 1000.0; }

  double ElapsedSeconds() const { return NowMonotonic() - beginTime_; }

  std::string ErrInfo() const {
    static const char *kMapError[] = {
        "success",
        "invalid input parameter",
        "failure to connect to server",
        "no valid data returned",
        "server does not have enough memory",
        "server does not support this command yet",
        "server fails to communicate with the inner FPGA",
        "some inner errors occur",
        "cannot activate lidar because of current fault code",
    };
    if (errCode_ < sizeof(kMapError) / sizeof(kMapError[0])) return kMapError[errCode_];
    char buffer[64];
    std::snprintf(buffer, sizeof(buffer), "unknown device error 0x%02X",
                  static_cast<unsigned>(errCode_));
    return std::string(buffer);
  }

  bool IsSucceed() const {
    return finished_ && innerCode_ == 0 && errCode_ == 0 &&
           curSend_ == reqTotal_ && curRecv_ == repTotal_;
  }

  bool IsFailed() const { return finished_ && !IsSucceed(); }

 private:
  uint8_t command_;
  int innerCode_ = 0;
  uint32_t errCode_ = 0;
  double beginTime_;
  double endTime_;
  size_t reqBlock_;
  size_t reqTotal_ = 0;
  size_t repTotal_ = 0;
  size_t curSend_ = 0;
  size_t curRecv_ = 0;
  std::vector<uint8_t> payload_;
  std::string errorMessage_;
  bool finished_ = false;
};

std::string FormatResultLine(const Result &result) {
  char buffer[256];
  std::snprintf(buffer, sizeof(buffer),
                "ret: {cmd:%08X, innerCode:%d, errCode:%u, request:%lu/%lu, "
                "response:%lu/%lu, totalTime:%.3fms}",
                static_cast<unsigned>(result.command()), result.innerCode(),
                static_cast<unsigned>(result.errCode()),
                static_cast<unsigned long>(result.curSend()),
                static_cast<unsigned long>(result.reqTotal()),
                static_cast<unsigned long>(result.curRecv()),
                static_cast<unsigned long>(result.repTotal()),
                result.WasteTimeMs());
  return std::string(buffer);
}

// ------------------------------ 原生库加载 --------------------------------
const char *PlatformTag() {
#if defined(_WIN32) && defined(_WIN64)
  return "windows-x86_64";
#elif !defined(_WIN32) && (defined(__x86_64__) || defined(__amd64__))
  return "linux-x86_64";
#elif !defined(_WIN32) && (defined(__aarch64__) || defined(__arm64__))
  return "linux-aarch64";
#else
  return NULL;
#endif
}

std::string NativeErrorText(int32_t status) {
  switch (status) {
    case 1: return "Invalid native API input";
    case 2: return "Upgrade package invalid or corrupted";
    case 3: return "Unable to recognize the device firmware information";
    case 4: return "Upgrade package is not compatible with this device";
    case 5: return "Native output buffer is too small";
    case 6: return "Native upgrade preparation failed";
    default: {
      char buffer[64];
      std::snprintf(buffer, sizeof(buffer), "Unknown native error %d",
                    static_cast<int>(status));
      return std::string(buffer);
    }
  }
}

bool FileExists(const std::string &path) {
#ifdef _WIN32
  DWORD attributes = ::GetFileAttributesA(path.c_str());
  return attributes != INVALID_FILE_ATTRIBUTES &&
         !(attributes & FILE_ATTRIBUTE_DIRECTORY);
#else
  struct stat info;
  return ::stat(path.c_str(), &info) == 0 && S_ISREG(info.st_mode);
#endif
}

std::string DirectoryName(const std::string &path) {
  size_t position = path.find_last_of("/\\");
  if (position == std::string::npos) return ".";
  if (position == 0) return path.substr(0, 1);
  return path.substr(0, position);
}

std::string ExecutableDirectory() {
#ifdef _WIN32
  char buffer[MAX_PATH] = {0};
  DWORD length = ::GetModuleFileNameA(NULL, buffer, MAX_PATH);
  if (length == 0 || length >= MAX_PATH) return ".";
  return DirectoryName(std::string(buffer, length));
#else
  char buffer[4096];
  ssize_t length = ::readlink("/proc/self/exe", buffer, sizeof(buffer) - 1);
  if (length <= 0) return ".";
  buffer[length] = '\0';
  return DirectoryName(std::string(buffer, static_cast<size_t>(length)));
#endif
}

class NativeLibrary {
 public:
  NativeLibrary() : handle_(NULL), api_version_(NULL), validate_(NULL), prepare_(NULL) {}
  ~NativeLibrary() {
#ifdef _WIN32
    if (handle_) ::FreeLibrary(handle_);
#else
    if (handle_) ::dlclose(handle_);
#endif
  }

  const std::string &path() const { return path_; }
  const char *tag() const { return tag_; }

  // 返回 false 时 *error 为用户可读原因（对应 Python 版退出码 7 的各分支）。
  bool Load(std::string *error) {
    tag_ = PlatformTag();
    if (!tag_) {
      *error = "Unsupported system/architecture; this distribution ships 64-bit "
               "Windows x64, Linux x64 and Linux ARM64 libraries only";
      return false;
    }
    if (sizeof(void *) != 8) {
      *error = "This distribution requires a 64-bit build";
      return false;
    }
    const char *filename = std::string(tag_).compare(0, 7, "windows") == 0
                               ? "jt128_upgrade.dll"
                               : "libjt128_upgrade.so";
    path_ = ExecutableDirectory() + "/lib/" + tag_ + "/" + filename;
    if (!FileExists(path_)) {
      *error = "Native library is missing: " + path_ +
               ". Extract the complete distribution.";
      return false;
    }
#ifdef _WIN32
    handle_ = ::LoadLibraryA(path_.c_str());
    if (!handle_) {
      *error = "Cannot load native library " + path_ + ": " +
               SocketErrorText(static_cast<int>(::GetLastError()));
      return false;
    }
    api_version_ = reinterpret_cast<uint32_t(*)(void)>(::GetProcAddress(handle_, "jt128_api_version"));
    validate_ = reinterpret_cast<int32_t(*)(const uint8_t *, uint64_t)>(::GetProcAddress(handle_, "jt128_validate"));
    prepare_ = reinterpret_cast<int32_t(*)(const uint8_t *, uint64_t, const uint8_t *, uint64_t,
                                           uint8_t *, uint64_t, uint64_t *)>(
        ::GetProcAddress(handle_, "jt128_prepare"));
#else
    handle_ = ::dlopen(path_.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (!handle_) {
      const char *reason = ::dlerror();
      *error = "Cannot load native library " + path_ + ": " +
               (reason ? reason : "unknown error");
      return false;
    }
    api_version_ = reinterpret_cast<uint32_t(*)(void)>(::dlsym(handle_, "jt128_api_version"));
    validate_ = reinterpret_cast<int32_t(*)(const uint8_t *, uint64_t)>(::dlsym(handle_, "jt128_validate"));
    prepare_ = reinterpret_cast<int32_t(*)(const uint8_t *, uint64_t, const uint8_t *, uint64_t,
                                           uint8_t *, uint64_t, uint64_t *)>(
        ::dlsym(handle_, "jt128_prepare"));
#endif
    if (!api_version_ || !validate_ || !prepare_) {
      *error = "Cannot load native library " + path_ + ": missing JT128 API symbols";
      return false;
    }
    if (api_version_() != 1) {
      *error = "Native library API version mismatch; use files from the same distribution";
      return false;
    }
    return true;
  }

  int32_t Validate(const uint8_t *data, uint64_t length) const {
    return validate_(data, length);
  }

  int32_t Prepare(const uint8_t *data, uint64_t length,
                  const uint8_t *inventory, uint64_t inventoryLength,
                  uint8_t *output, uint64_t capacity, uint64_t *outputLength) const {
    return prepare_(data, length, inventory, inventoryLength, output, capacity, outputLength);
  }

 private:
#ifdef _WIN32
  HMODULE handle_;
#else
  void *handle_;
#endif
  uint32_t (*api_version_)(void);
  int32_t (*validate_)(const uint8_t *, uint64_t);
  int32_t (*prepare_)(const uint8_t *, uint64_t, const uint8_t *, uint64_t,
                      uint8_t *, uint64_t, uint64_t *);
  std::string path_;
  const char *tag_ = "";
};

// 与 Python 版 prepare 相同的两步调用：先查长度（期望 BUFFER_TOO_SMALL），再填充。
bool PrepareSelected(const NativeLibrary &native, const std::vector<uint8_t> &data,
                     const std::vector<uint8_t> &inventory, std::vector<uint8_t> *selected,
                     std::string *error) {
  uint64_t required = 0;
  int32_t status = native.Prepare(data.data(), data.size(), inventory.data(),
                                  inventory.size(), NULL, 0, &required);
  if (status != 5) {
    *error = status == 0 ? "Native library returned an invalid size response"
                         : NativeErrorText(status);
    return false;
  }
  if (required == 0 || required > data.size()) {
    *error = "Native library returned an invalid output size";
    return false;
  }
  selected->assign(static_cast<size_t>(required), 0);
  status = native.Prepare(data.data(), data.size(), inventory.data(), inventory.size(),
                          selected->data(), required, &required);
  if (status != 0) {
    *error = NativeErrorText(status);
    return false;
  }
  if (required == 0 || required > selected->size()) {
    *error = "Native library returned an invalid output length";
    return false;
  }
  selected->resize(static_cast<size_t>(required));
  return true;
}

// ------------------------------ PTC 客户端 --------------------------------
class Ptc {
 public:
  explicit Ptc(double defaultTimeout, bool printPayload)
      : defaultTimeout_(defaultTimeout), printPayload_(printPayload) {}

  ~Ptc() { DisconnectLidar(false); }

  bool IsConnected() const { return socket_ != kInvalidSocket; }

  bool ConnectLidar(const std::string &ip, uint16_t port, const std::string &netcard,
                    double timeout) {
    DisconnectLidar(false);
    PrintInfo("connect lidar " + netcard + ":" + ip + ":" + std::to_string(port) + " ...");
    if (netcard != "default") {
#ifndef SO_BINDTODEVICE
      PrintError("connect failed: Binding a named network interface requires Linux");
      return false;
#endif
    }
    socket_t fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd == kInvalidSocket) {
      PrintError("connect failed: " + SocketErrorText(LastSocketError()));
      return false;
    }
    std::string error;
    if (netcard != "default") {
#ifdef SO_BINDTODEVICE
      if (::setsockopt(fd, SOL_SOCKET, SO_BINDTODEVICE, netcard.c_str(),
                       static_cast<socklen_t>(netcard.size() + 1)) != 0) {
        error = SocketErrorText(errno);
      }
#endif
    }
    if (error.empty()) {
      struct sockaddr_in address;
      std::memset(&address, 0, sizeof(address));
      address.sin_family = AF_INET;
      address.sin_port = htons(port);
      if (::inet_pton(AF_INET, ip.c_str(), &address.sin_addr) != 1) {
        error = "invalid IP address: " + ip;
      } else {
        error = ConnectWithTimeout(fd, address, timeout);
      }
    }
    if (!error.empty()) {
      CloseSocket(fd);
      PrintError("connect failed: " + error);
      return false;
    }
    SetBlocking(fd);
    SetSocketTimeouts(fd, defaultTimeout_);
    socket_ = fd;
    PrintInfo("connect successfully");
    return true;
  }

  void DisconnectLidar(bool normal = true) {
    if (socket_ == kInvalidSocket) return;
    socket_t fd = socket_;
    socket_ = kInvalidSocket;
#ifdef _WIN32
    ::shutdown(fd, SD_BOTH);
#else
    ::shutdown(fd, SHUT_RDWR);
#endif
    CloseSocket(fd);
    if (normal) PrintInfo("disconnect lidar");
  }

  Result SendData1(bool isExt, uint8_t cmd, const std::vector<uint8_t> &data,
                   double timeout = -1.0) {
    Result result = Exchange(isExt, cmd, data, timeout);
    PrintResult(result);
    return result;
  }

  Result SendFile2(bool isExt, uint8_t cmd, const std::vector<uint8_t> &data,
                   size_t block, double timeout) {
    Result result(cmd);
    do {
      if (block < 1 || block > 1024) {
        result.set_errorMessage("Block size must be between 1 and 1024");
        break;
      }
      if (data.empty()) {
        result.set_errorMessage("Cannot upload an empty file");
        break;
      }
      result.set_reqTotal(data.size());
      result.set_reqBlock(block);
      const size_t total = (data.size() + block - 1) / block;
      for (size_t offset = 0, index = 1; offset < data.size(); offset += block, ++index) {
        if (Interrupted()) {
          result.set_errorMessage(kInterruptedMessage);
          break;
        }
        const size_t chunkLength = std::min(block, data.size() - offset);
        std::vector<uint8_t> payload = MakeBlockPayload(
            static_cast<uint32_t>(index), static_cast<uint32_t>(total),
            data.data() + offset, chunkLength);
        // 每帧的响应独立处理，不能将前一帧 payload 混入后续响应。
        Result reply = Exchange(isExt, cmd, payload, timeout);
        result.AdoptFrameState(reply);
        if (!reply.IsSucceed()) break;
        result.AddCurSend(chunkLength);  // 只统计已确认的文件字节
        const double elapsed = std::max(result.ElapsedSeconds(), 0.000001);
        char progress[160];
        std::snprintf(progress, sizeof(progress),
                      "ptc transfer: Send=%lu/%lu --- %.2f%%(%.2fK/s)",
                      static_cast<unsigned long>(result.curSend()),
                      static_cast<unsigned long>(result.reqTotal()),
                      100.0 * static_cast<double>(result.curSend()) /
                          static_cast<double>(result.reqTotal()),
                      static_cast<double>(result.curSend()) / elapsed / 1024.0);
        PrintInfo(progress, result.curSend() < result.reqTotal());
      }
    } while (false);
    result.Finish();
    PrintResult(result);
    return result;
  }

  Result GetInventoryInfo() {
    return SendData1(false, PTC_COMMAND_GET_INVENTORY_INFO, std::vector<uint8_t>());
  }

  Result RebootLidar() {
    return SendData1(false, PTC_COMMAND_REBOOT, std::vector<uint8_t>());
  }

  Result UploadSoftware83(const std::vector<uint8_t> &data) {
    return SendFile2(false, PTC_COMMAND_FOTA_REQUEST_UPGRADE, data, kUploadBlockSize, 60.0);
  }

 private:
  Result Exchange(bool isExt, uint8_t cmd, const std::vector<uint8_t> &data,
                  double timeout) {
    Result result(cmd);
    result.set_reqTotal(data.size());
    std::vector<uint8_t> payload = data;
    uint8_t wireCmd = cmd;
    if (isExt) {
      wireCmd = 0xFF;
      std::vector<uint8_t> ext;
      AppendU32BE(&ext, cmd);
      ext.insert(ext.end(), payload.begin(), payload.end());
      payload = ext;
    }
    std::string error;
    do {
      if (!IsConnected()) {
        error = "Lidar is not connected";
        break;
      }
      if (Interrupted()) {
        error = kInterruptedMessage;
        break;
      }
      if (timeout <= 0.0) timeout = defaultTimeout_;
      SetSocketTimeouts(socket_, timeout);
      const double deadline = NowMonotonic() + timeout;
      std::vector<uint8_t> frame = MakeFrame(wireCmd, payload.data(), payload.size());
      if (printPayload_) {
        PrintInfo("send: " + CastBytes2HexStrs(frame.data(), frame.size()));
      }
      if (!SendAll(frame.data(), frame.size(), &error)) break;
      result.MarkSent();  // 对齐 Python：sendall 完成后 curSend=reqTotal
      uint8_t header[kFrameHeaderSize];
      if (!RecvExact(header, kFrameHeaderSize, deadline, &error)) break;
      if (header[0] != kPtcId0 || header[1] != kPtcId1 || header[2] != wireCmd) {
        error = "Invalid PTC response magic or command";
        break;
      }
      result.set_errCode(header[3]);
      const uint32_t repTotal = ReadU32BE(header + 4);
      result.set_repTotal(repTotal);
      if (repTotal > kMaxResponseBytes) {
        error = "PTC response exceeds 1 MiB";
        break;
      }
      std::vector<uint8_t> response(repTotal);
      if (!RecvExact(response.data(), repTotal, deadline, &error)) {
        break;
      }
      if (isExt && result.errCode() == 0) {
        if (response.size() < 4 || ReadU32BE(response.data()) != cmd) {
          error = "Invalid PTC response subcommand";
          break;
        }
        response.erase(response.begin(), response.begin() + 4);
      }
      result.set_curRecv(response.size());
      result.set_payload(std::move(response));
    } while (false);
    if (!error.empty()) {
      result.set_innerCode(1);
      result.set_errorMessage(error);
      DisconnectLidar(false);
    }
    result.Finish();
    return result;
  }

  void PrintResult(const Result &result) const {
    if (result.IsFailed()) {
      PrintError(FormatResultLine(result));
      PrintError(result.errorMessage().empty() ? result.ErrInfo() : result.errorMessage());
    } else {
      PrintInfo(FormatResultLine(result));
    }
  }

  std::string ConnectWithTimeout(socket_t fd, const struct sockaddr_in &address,
                                 double timeout) {
    SetNonBlocking(fd);
    if (::connect(fd, reinterpret_cast<const struct sockaddr *>(&address),
                  sizeof(address)) != 0) {
      int code = LastSocketError();
      if (code != kInProgress) return SocketErrorText(code);
      const int pollResult = PollWritable(fd, static_cast<int>(timeout * 1000.0));
      if (pollResult <= 0) {
        return pollResult == 0 ? "timed out" : SocketErrorText(LastSocketError());
      }
      int soError = 0;
      socklen_t optionLength = sizeof(soError);
      if (::getsockopt(fd, SOL_SOCKET, SO_ERROR, reinterpret_cast<char *>(&soError),
                       &optionLength) != 0 || soError != 0) {
        return SocketErrorText(soError != 0 ? soError : LastSocketError());
      }
    }
    SetBlocking(fd);
    return "";
  }

  bool SendAll(const uint8_t *data, size_t length, std::string *error) {
    size_t sent = 0;
    while (sent < length) {
      if (Interrupted()) {
        *error = kInterruptedMessage;
        return false;
      }
      const int chunk = static_cast<int>(std::min<size_t>(length - sent, 0x40000000u));
#ifdef _WIN32
      const int written = ::send(socket_, reinterpret_cast<const char *>(data) + sent, chunk, 0);
#else
      const ssize_t written = ::send(socket_, data + sent, static_cast<size_t>(chunk), 0);
#endif
      if (written > 0) {
        sent += static_cast<size_t>(written);
        continue;
      }
      if (written < 0 && LastSocketError() == kInterrupted) continue;
      const int code = LastSocketError();
      *error = code == kWouldBlock || code == kTimedOut ? "send timed out"
                                                        : SocketErrorText(code);
      return false;
    }
    return true;
  }

  // 在共享 deadline 内收满 length 字节；超时/中断/断连分别报错。
  bool RecvExact(uint8_t *data, size_t length, double deadline, std::string *error) {
    size_t received = 0;
    while (received < length) {
      if (Interrupted()) {
        *error = kInterruptedMessage;
        return false;
      }
      const double remaining = deadline - NowMonotonic();
      if (remaining <= 0.0) {
        *error = "PTC response timeout";
        return false;
      }
      SetSocketTimeouts(socket_, remaining);
#ifdef _WIN32
      const int chunk = ::recv(socket_, reinterpret_cast<char *>(data) + received,
                               static_cast<int>(length - received), 0);
#else
      const ssize_t chunk = ::recv(socket_, data + received, length - received, 0);
#endif
      if (chunk > 0) {
        received += static_cast<size_t>(chunk);
        continue;
      }
      if (chunk < 0 && LastSocketError() == kInterrupted) continue;
      const int code = LastSocketError();
      if (chunk == 0) {
        *error = "Lidar closed the connection before a complete response";
      } else if (code == kWouldBlock || code == kTimedOut) {
        *error = "PTC response timeout";
      } else {
        *error = SocketErrorText(code);
      }
      return false;
    }
    return true;
  }

  socket_t socket_ = kInvalidSocket;
  double defaultTimeout_;
  bool printPayload_;
};

// ------------------------------ 命令行 ------------------------------------
struct Options {
  bool check = false;
  std::string package;
  std::string ip = "192.168.1.201";
  uint16_t port = 9347;
  std::string netcard = "default";
};

const int kParseOk = 0;
const int kParseHelp = 1;
const int kParseError = 2;

std::string UsageText() {
  return
      "usage: JT128_Upgrade [-h] [--check] [package] [ip] [port] [netcard]\n"
      "\n"
      "Standalone JT128 upgrade (no swap).\n"
      "\n"
      "positional arguments:\n"
      "  package               path to the .patch upgrade package\n"
      "  ip                    lidar IP (default: 192.168.1.201)\n"
      "  port                  PTC TCP port (default: 9347)\n"
      "  netcard               Linux interface name or default (default: default)\n"
      "\n"
      "options:\n"
      "  -h, --help            show this help message and exit\n"
      "  --check               check the installation without connecting\n"
      "\n"
      "Return codes: 0=uploaded/reboot acknowledged, 1=package, "
      "2=connection/arguments, 3=inventory, 4=selection, 5=upload, 6=reboot, "
      "7=native library, 130=interrupted.\n";
}

void PrintParseError(const std::string &message) {
  std::fprintf(stderr, "usage: JT128_Upgrade [-h] [--check] [package] [ip] [port] [netcard]\n"
                       "JT128_Upgrade: error: %s\n", message.c_str());
}

// 严格解析 1..65535 的十进制端口（允许前后空白与正负号，与 Python int() 一致）
bool ParsePort(const std::string &text, uint16_t *port) {
  if (text.empty()) return false;
  size_t begin = text.find_first_not_of(" \t\r\n\f\v");
  size_t end = text.find_last_not_of(" \t\r\n\f\v");
  if (begin == std::string::npos) return false;
  const std::string trimmed = text.substr(begin, end - begin + 1);
  errno = 0;
  char *stop = NULL;
  const long value = std::strtol(trimmed.c_str(), &stop, 10);
  if (errno != 0 || stop == trimmed.c_str() || *stop != '\0') return false;
  if (value < 1 || value > 65535) return false;
  *port = static_cast<uint16_t>(value);
  return true;
}

// 与 argparse 一致：解析器无数字选项时，负数（含小数）视为位置参数而非选项
bool LooksLikeNegativeNumber(const std::string &text) {
  return text.size() >= 2 && text[0] == '-' &&
         text.find_first_not_of("0123456789.", 1) == std::string::npos;
}

int ParseArgs(int argc, char **argv, Options *options) {
  size_t positional = 0;
  for (int i = 1; i < argc; ++i) {
    const std::string argument = argv[i];
    if (argument == "-h" || argument == "--help") return kParseHelp;
    if (argument == "--check") {
      options->check = true;
      continue;
    }
    if (argument.size() >= 2 && argument[0] == '-' && argument != "-" &&
        !LooksLikeNegativeNumber(argument)) {
      PrintParseError("unrecognized arguments: " + argument);
      return kParseError;
    }
    switch (positional++) {
      case 0: options->package = argument; break;
      case 1: options->ip = argument; break;
      case 2:
        if (!ParsePort(argument, &options->port)) {
          PrintParseError(argument.find_first_not_of("+-0123456789 \t\r\n\f\v") == std::string::npos
                              ? "port must be in 1..65535"
                              : "port must be an integer");
          return kParseError;
        }
        break;
      case 3: options->netcard = argument; break;
      default:
        PrintParseError("unrecognized arguments: " + argument);
        return kParseError;
    }
  }
  if (!options->check && options->package.empty()) {
    PrintParseError("the following arguments are required: package");
    return kParseError;
  }
  return kParseOk;
}

bool ReadFileBytes(const std::string &path, std::vector<uint8_t> *data,
                   std::string *error) {
  std::FILE *stream = std::fopen(path.c_str(), "rb");
  if (!stream) {
    std::string reason = std::strerror(errno);
    *error = "[Errno " + std::to_string(errno) + "] " + reason + ": '" + path + "'";
    return false;
  }
  data->clear();
  char buffer[65536];
  size_t chunk;
  while ((chunk = std::fread(buffer, 1, sizeof(buffer), stream)) > 0) {
    data->insert(data->end(), buffer, buffer + chunk);
  }
  bool ok = std::ferror(stream) == 0;
  if (!ok) *error = "failed to read file: " + path;
  std::fclose(stream);
  return ok;
}

// 等待秒数：默认 10，可用环境变量 JT128_REBOOT_WAIT 覆盖（仅供调试/测试）。
double RebootWaitSeconds() {
  const char *text = std::getenv("JT128_REBOOT_WAIT");
  if (!text) return 10.0;
  char *stop = NULL;
  const double value = std::strtod(text, &stop);
  if (stop == text || *stop != '\0' || value < 0.0) return 10.0;
  return value;
}

int RunMain(int argc, char **argv) {
  std::signal(SIGINT, HandleSigint);
#ifdef _WIN32
  WSADATA wsaData;
  if (::WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
    PrintError("Cannot initialize Winsock");
    return 7;
  }
#endif
  int exitCode = 0;
  Ptc *ptc = NULL;
  Options options;
  const int parseStatus = ParseArgs(argc, argv, &options);
  if (parseStatus == kParseHelp) {
    std::fputs(UsageText().c_str(), stdout);
    exitCode = 0;
  } else if (parseStatus == kParseError) {
    exitCode = 2;
  } else {
    PrintInfo(JT128_CLI_VERSION);
    NativeLibrary native;
    std::string error;
    if (!native.Load(&error)) {
      PrintError(error);
      exitCode = 7;
    } else if (options.check) {
      PrintInfo(std::string("Native API 1 OK: ") + native.path() + " (" + native.tag() + ")");
      exitCode = 0;
    } else {
      std::vector<uint8_t> data;
      if (!ReadFileBytes(options.package, &data, &error)) {
        PrintError(error);
        exitCode = 1;
      } else {
        const int32_t status = native.Validate(data.data(), data.size());
        if (status != 0) {
          PrintError(NativeErrorText(status));
          exitCode = 1;
        }
      }
      if (exitCode == 0) {
        ptc = new Ptc(10.0, false);
        if (Interrupted()) {
          PrintError(kInterruptedMessage);  // 尚未建立连接，结果行不会携带原因。
          exitCode = 130;
        } else if (!ptc->ConnectLidar(options.ip, options.port, options.netcard, 5.0)) {
          exitCode = Interrupted() ? 130 : 2;
        } else {
          Result inventory = ptc->GetInventoryInfo();
          if (Interrupted()) {
            // 结果行已打印过中断原因时不再重复。
            if (inventory.errorMessage() != kInterruptedMessage) {
              PrintError(kInterruptedMessage);
            }
            exitCode = 130;
          } else if (!inventory.IsSucceed()) {
            PrintError("Unable to read the lidar firmware information");
            exitCode = 3;
          } else {
            std::vector<uint8_t> selected;
            if (!PrepareSelected(native, data, inventory.payload(), &selected, &error)) {
              PrintError(error);
              exitCode = 4;
            } else {
              PrintInfo("selected upload size: " + std::to_string(selected.size()) + " bytes");
              const Result upload = ptc->UploadSoftware83(selected);
              if (Interrupted()) {
                if (upload.errorMessage() != kInterruptedMessage) {
                  PrintError(kInterruptedMessage);
                }
                exitCode = 130;
              } else if (!upload.IsSucceed()) {
                PrintError("Upgrade transfer failed; stopping");
                exitCode = 5;
              } else {
                PrintInfo("Package transmitted successfully; waiting " +
                          std::to_string(static_cast<int>(RebootWaitSeconds())) +
                          " seconds before reboot");
                if (!InterruptibleSleep(RebootWaitSeconds())) {
                  PrintError(kInterruptedMessage);
                  exitCode = 130;
                } else if (!ptc->RebootLidar().IsSucceed()) {
                  PrintError("Reboot not acknowledged; verify the lidar and reboot manually if needed");
                  exitCode = 6;
                } else {
                  PrintInfo("Transfer complete and reboot command acknowledged");
                  exitCode = 0;
                }
              }
            }
          }
        }
      }
    }
  }
  if (ptc) {
    ptc->DisconnectLidar();
    delete ptc;
  }
#ifdef _WIN32
  ::WSACleanup();
#endif
  return exitCode;
}

}  // namespace jt128cli

#ifndef JT128_CLI_NO_MAIN
int main(int argc, char **argv) { return jt128cli::RunMain(argc, argv); }
#endif
