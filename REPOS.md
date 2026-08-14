# Repositories

Repositories referenced by `build_MOS_stack_until_OIIO.sh` plus builder-only additions, with current local clones and their origin URLs.

| Script label | Local dir | Origin URL | Notes |
| --- | --- | --- | --- |
| zlib-ng | zlib-ng | https://github.com/zlib-ng/zlib-ng.git |  |
| xz (liblzma) | xz | https://github.com/tukaani-project/xz.git |  |
| libdeflate | libdeflate | https://github.com/ebiggers/libdeflate.git |  |
| zstd | zstd | https://github.com/facebook/zstd.git |  |
| libjpeg-turbo | libjpeg-turbo | https://github.com/libjpeg-turbo/libjpeg-turbo |  |
| libpng | libpng | https://github.com/pnggroup/libpng.git |  |
| brotli | brotli | https://github.com/google/brotli.git |  |
| highway | highway | https://github.com/google/highway.git |  |
| Little-CMS (lcms2) | Little-CMS | https://github.com/ssh4net/Little-CMS.git |  |
| glfw | glfw | https://github.com/glfw/glfw.git |  |
| freeglut | freeglut | https://github.com/freeglut/freeglut.git |  |
| glew-cmake | glew-cmake | https://github.com/Perlmint/glew-cmake.git |  |
| libtiff | libtiff | https://gitlab.com/libtiff/libtiff.git |  |
| openjpeg | openjpeg | https://github.com/uclouvain/openjpeg.git |  |
| jasper | jasper | https://github.com/jasper-software/jasper.git |  |
| giflib | gif | https://github.com/ssh4net/gif.git | Uses the fork's CMake build |
| Imath | Imath | https://github.com/AcademySoftwareFoundation/Imath.git |  |
| OpenEXR | openexr | https://github.com/AcademySoftwareFoundation/openexr.git |  |
| rapidobj | rapidobj | https://github.com/guybrush77/rapidobj.git | Header-only CMake package; builder disables tests/tools/examples |
| rapidfuzz-cpp | rapidfuzz-cpp | https://github.com/rapidfuzz/rapidfuzz-cpp.git | Header-only CMake package; OpenMeta uses `rapidfuzz::rapidfuzz` for semantic metadata query matching |
| toml11 | toml11 | https://github.com/ToruNiina/toml11.git | Optional header-only TOML CMake package for local consumers |
| miniply | miniply | https://github.com/ssh4net/miniply.git | Small C++ library; builder disables CLI tools |
| OpenMeta | OpenMeta | https://github.com/ssh4net/OpenMeta.git | Builds library + Python wheel; can use optional `dng_sdk` package and `rapidfuzz-cpp` |
| openjph | openjph | https://github.com/ssh4net/OpenJPH.git |  |
| googletest | googletest | https://github.com/google/googletest.git |  |
| libjxl | libjxl | https://github.com/libjxl/libjxl.git |  |
| libultrahdr | libultrahdr | https://github.com/google/libultrahdr.git |  |
| OpenColorIO | OpenColorIO | https://github.com/AcademySoftwareFoundation/OpenColorIO.git |  |
| CPython | cpython | https://github.com/python/cpython.git |  |
| minizip-ng | minizip-ng | https://github.com/zlib-ng/minizip-ng.git |  |
| libwebp | libwebp | https://chromium.googlesource.com/webm/libwebp |  |
| libvpx | libvpx | https://chromium.googlesource.com/webm/libvpx | Optional; disabled by default |
| opus | opus | https://github.com/xiph/opus.git | Optional; disabled by default |
| libyuv | libyuv | https://chromium.googlesource.com/libyuv/libyuv | Optional; disabled by default |
| ptex | ptex | https://github.com/wdas/ptex.git |  |
| LibRaw | LibRaw | https://github.com/LibRaw/LibRaw.git |  |
| LibRaw-cmake | LibRaw-cmake | https://github.com/ssh4net/LibRaw-cmake.git |  |
| libheif | libheif | https://github.com/strukturag/libheif.git |  |
| aom | aom | https://aomedia.googlesource.com/aom |  |
| libde265 | libde265 | https://github.com/strukturag/libde265.git |  |
| x265 | x265_git | https://bitbucket.org/multicoreware/x265_git.git |  |
| kvazaar | kvazaar | https://github.com/ultravideo/kvazaar.git |  |
| ffmpeg | ffmpeg | https://github.com/FFmpeg/FFmpeg.git |  |
| imgui | imgui | https://github.com/ocornut/imgui.git | Optional source-only checkout; uses `docking` branch |
| imgui_test_engine | imgui_test_engine | https://github.com/ocornut/imgui_test_engine.git | Optional source-only checkout |
| sqlite | sqlite | https://github.com/sqlite/sqlite.git | Source-built with JSON, FTS5, RTree, Geopoly, and zlib-backed ZIP support; no ICU/Tcl runtime dependency |
| OpenSSL | openssl | https://github.com/openssl/openssl.git | Source-built from the moving `openssl-4.0` branch; requires Perl and Make/nmake, plus NASM on x86-64 |
