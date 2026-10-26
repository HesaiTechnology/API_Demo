#ifndef JT128_API_H_
#define JT128_API_H_

#include <stdint.h>

#if defined(_WIN32)
#  if defined(JT128_BUILDING_LIBRARY)
#    define JT128_API __declspec(dllexport)
#  else
#    define JT128_API __declspec(dllimport)
#  endif
#  define JT128_CALL __cdecl
#else
#  define JT128_API __attribute__((visibility("default")))
#  define JT128_CALL
#endif

#if defined(__cplusplus)
#  define JT128_NOEXCEPT noexcept
extern "C" {
#else
#  define JT128_NOEXCEPT
#endif

/* Stable ABI status values. All buffers remain owned by the caller. */
enum jt128_status {
  JT128_OK = 0,
  JT128_INVALID_ARGUMENT = 1,
  JT128_INVALID_PACKAGE = 2,
  JT128_UNKNOWN_DEVICE = 3,
  JT128_PACKAGE_MISMATCH = 4,
  JT128_BUFFER_TOO_SMALL = 5,
  JT128_INTERNAL_ERROR = 6
};

JT128_API uint32_t JT128_CALL jt128_api_version(void) JT128_NOEXCEPT;

/* Validate an input package without exposing its internal representation. */
JT128_API int32_t JT128_CALL jt128_validate(
    const uint8_t *data, uint64_t length) JT128_NOEXCEPT;

/* Prepare the upload using the complete device inventory response.
 * data, inventory and output_length must be non-null. output may be null only
 * when capacity is zero. A length query returns JT128_BUFFER_TOO_SMALL.
 * output_length receives the required size on OK or BUFFER_TOO_SMALL and zero
 * on all other errors. No output bytes are written on failure.
 */
JT128_API int32_t JT128_CALL jt128_prepare(
    const uint8_t *data, uint64_t length,
    const uint8_t *inventory, uint64_t inventory_length,
    uint8_t *output, uint64_t capacity, uint64_t *output_length) JT128_NOEXCEPT;

#if defined(__cplusplus)
}  /* extern "C" */
#endif

#endif  /* JT128_API_H_ */
