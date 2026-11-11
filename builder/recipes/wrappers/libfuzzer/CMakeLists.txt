cmake_minimum_required(VERSION 3.20)
project(LibFuzzer VERSION 20.1.8 LANGUAGES CXX)

include(GNUInstallDirs)
include(CMakePackageConfigHelpers)
include(CheckCXXSourceCompiles)
find_package(Threads REQUIRED)

# This wrapper builds unmodified compiler-rt sources with the prefix's C++ ABI.
get_filename_component(compiler_rt "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)
set(fuzzer "${compiler_rt}/lib/fuzzer")
set(sources
    FuzzerCrossOver.cpp FuzzerDataFlowTrace.cpp FuzzerDriver.cpp
    FuzzerExtFunctionsDlsym.cpp FuzzerExtFunctionsWeak.cpp FuzzerExtFunctionsWindows.cpp
    FuzzerExtraCounters.cpp FuzzerExtraCountersDarwin.cpp FuzzerExtraCountersWindows.cpp
    FuzzerFork.cpp FuzzerIO.cpp FuzzerIOPosix.cpp FuzzerIOWindows.cpp FuzzerLoop.cpp
    FuzzerMerge.cpp FuzzerMutate.cpp FuzzerSHA1.cpp FuzzerTracePC.cpp FuzzerUtil.cpp
    FuzzerUtilDarwin.cpp FuzzerUtilFuchsia.cpp FuzzerUtilLinux.cpp FuzzerUtilPosix.cpp
    FuzzerUtilWindows.cpp)
list(TRANSFORM sources PREPEND "${fuzzer}/")

check_cxx_source_compiles("#include <cstddef>
#ifndef _LIBCPP_VERSION
#error not libc++
#endif
int main() { return 0; }" LIBFUZZER_USES_LIBCXX)
if(LIBFUZZER_USES_LIBCXX)
    set(LibFuzzer_CXX_STDLIB "libc++")
elseif(MSVC)
    set(LibFuzzer_CXX_STDLIB "msvc")
else()
    set(LibFuzzer_CXX_STDLIB "libstdc++")
endif()

add_library(FuzzerObjects OBJECT ${sources})
add_library(Fuzzer STATIC $<TARGET_OBJECTS:FuzzerObjects> "${fuzzer}/FuzzerMain.cpp")
add_library(FuzzerNoMain STATIC $<TARGET_OBJECTS:FuzzerObjects>)
set(runtime_targets Fuzzer FuzzerNoMain)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    add_library(FuzzerInterceptors STATIC "${fuzzer}/FuzzerInterceptors.cpp")
    set_target_properties(FuzzerInterceptors PROPERTIES EXPORT_NAME Interceptors)
    list(APPEND runtime_targets FuzzerInterceptors)
endif()

foreach(target FuzzerObjects ${runtime_targets})
    target_compile_features(${target} PRIVATE cxx_std_17)
    target_include_directories(${target} PRIVATE "${compiler_rt}/include")
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")
        # Instrument the code under test, never the fuzzing engine itself.
        target_compile_options(${target} PRIVATE -fno-sanitize=all -fsanitize-coverage=0)
    elseif(CMAKE_CXX_FLAGS MATCHES "sanitize")
        message(FATAL_ERROR "Use Clang to build libFuzzer in a sanitizer-enabled prefix")
    endif()
    if(MSVC)
        target_compile_definitions(${target} PRIVATE _HAS_EXCEPTIONS=0)
    else()
        target_compile_options(${target} PRIVATE -fno-exceptions -fno-rtti -fno-omit-frame-pointer)
    endif()
endforeach()

foreach(target IN LISTS runtime_targets)
    target_link_libraries(${target} PUBLIC Threads::Threads ${CMAKE_DL_LIBS})
    if(WIN32)
        target_link_libraries(${target} PUBLIC psapi)
    endif()
    target_include_directories(${target} INTERFACE "$<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>")
    if(LIBFUZZER_USES_LIBCXX)
        target_compile_options(${target} INTERFACE "$<$<COMPILE_LANGUAGE:CXX>:-stdlib=libc++>")
        target_link_options(${target} INTERFACE -stdlib=libc++)
    endif()
endforeach()

install(TARGETS ${runtime_targets} EXPORT LibFuzzerTargets ARCHIVE DESTINATION "${CMAKE_INSTALL_LIBDIR}")
install(FILES "${compiler_rt}/include/fuzzer/FuzzedDataProvider.h" "${fuzzer}/FuzzerInterface.h"
        DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/fuzzer")
install(FILES "${compiler_rt}/LICENSE.TXT" DESTINATION "${CMAKE_INSTALL_DATADIR}/licenses/libfuzzer")
set(package_dir "${CMAKE_INSTALL_LIBDIR}/cmake/LibFuzzer")
configure_package_config_file(LibFuzzerConfig.cmake.in "${CMAKE_CURRENT_BINARY_DIR}/LibFuzzerConfig.cmake"
    INSTALL_DESTINATION "${package_dir}")
write_basic_package_version_file("${CMAKE_CURRENT_BINARY_DIR}/LibFuzzerConfigVersion.cmake"
    VERSION "${PROJECT_VERSION}" COMPATIBILITY SameMajorVersion)
install(EXPORT LibFuzzerTargets NAMESPACE LibFuzzer:: DESTINATION "${package_dir}")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/LibFuzzerConfig.cmake"
              "${CMAKE_CURRENT_BINARY_DIR}/LibFuzzerConfigVersion.cmake" DESTINATION "${package_dir}")
