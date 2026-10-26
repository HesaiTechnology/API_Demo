cmake_minimum_required(VERSION 3.15)
project(jt128_upgrade_cli LANGUAGES CXX)

# Standalone CLI host: compiles only jt128_upgrade_cli.cpp and talks to the
# shipped native library through its stable C ABI at runtime (dlopen/LoadLibrary).

set(CMAKE_CXX_STANDARD 11)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT CMAKE_SIZEOF_VOID_P EQUAL 8)
    message(FATAL_ERROR "JT128 CLI requires a 64-bit target")
endif()
string(TOLOWER "${CMAKE_SYSTEM_PROCESSOR}" JT128_PROCESSOR)
if(CMAKE_SYSTEM_NAME STREQUAL "Windows" AND JT128_PROCESSOR MATCHES "^(amd64|x86_64)$")
    set(JT128_PLATFORM "windows-x86_64")
elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND JT128_PROCESSOR MATCHES "^(amd64|x86_64)$")
    set(JT128_PLATFORM "linux-x86_64")
elseif(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND JT128_PROCESSOR MATCHES "^(aarch64|arm64)$")
    set(JT128_PLATFORM "linux-aarch64")
else()
    message(FATAL_ERROR "Unsupported JT128 target: ${CMAKE_SYSTEM_NAME}/${CMAKE_SYSTEM_PROCESSOR}")
endif()
if(DEFINED JT128_EXPECTED_PLATFORM AND NOT JT128_EXPECTED_PLATFORM STREQUAL JT128_PLATFORM)
    message(FATAL_ERROR "Requested ${JT128_EXPECTED_PLATFORM}, compiler targets ${JT128_PLATFORM}")
endif()

# 默认输出到当前 build 目录（独立编译友好）；仓库构建脚本会显式传入 Scripts/bin。
set(JT128_BIN_ROOT "${CMAKE_CURRENT_BINARY_DIR}" CACHE PATH "CLI executable output root")
option(JT128_STATIC_RUNTIME "Link the C++ and compiler runtimes statically" ON)
option(JT128_STRIP "Strip the resulting executable" ON)

add_executable(JT128_Upgrade jt128_upgrade_cli.cpp)
# 发布包将 jt128_api.h 与源文件同目录放置；仓库内则回退到 jt128_native。
target_include_directories(JT128_Upgrade PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/../jt128_native")
target_compile_definitions(JT128_Upgrade PRIVATE JT128_CLI_VERSION="jt128_upgrade_cpp_2.0.0")
set_target_properties(JT128_Upgrade PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${JT128_BIN_ROOT}/${JT128_PLATFORM}"
)
# Keep multi-configuration generators on the same distribution path.
foreach(JT128_CONFIG DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
    set_target_properties(JT128_Upgrade PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY_${JT128_CONFIG} "${JT128_BIN_ROOT}/${JT128_PLATFORM}")
endforeach()

if(MSVC)
    if(JT128_STATIC_RUNTIME)
        set_property(TARGET JT128_Upgrade PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
    endif()
    target_compile_options(JT128_Upgrade PRIVATE /W4)
    target_link_libraries(JT128_Upgrade PRIVATE ws2_32)
else()
    target_compile_options(JT128_Upgrade PRIVATE -Wall -Wextra)
    if(WIN32)
        target_link_libraries(JT128_Upgrade PRIVATE ws2_32)
        if(JT128_STATIC_RUNTIME)
            target_link_options(JT128_Upgrade PRIVATE -static)
        endif()
        # jt128_upgrade.dll is loaded at runtime; keep the import table minimal.
        target_link_options(JT128_Upgrade PRIVATE -Wl,--no-insert-timestamp)
    else()
        target_link_libraries(JT128_Upgrade PRIVATE ${CMAKE_DL_LIBS})
    endif()
    if(JT128_STATIC_RUNTIME AND NOT WIN32)
        target_link_options(JT128_Upgrade PRIVATE -static-libstdc++ -static-libgcc)
    endif()
    if(JT128_STRIP)
        target_link_options(JT128_Upgrade PRIVATE -s)
    endif()
endif()
message(STATUS "JT128 CLI distribution target: ${JT128_PLATFORM}")
