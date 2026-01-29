#!/usr/bin/env bash
set -eEuo pipefail

CURRENT_CFG=""
CURRENT_PKG=""
CURRENT_PHASE=""

log() {
  # Keep log lines grep-friendly.
  printf '[%s] %s\n' "${CURRENT_CFG:-?}" "$*"
}

if [[ -t 1 ]]; then
  _YELLOW="$(printf '\033[33m')"
  _RESET="$(printf '\033[0m')"
else
  _YELLOW=""
  _RESET=""
fi

print_cmd() {
  local out=""
  local arg
  for arg in "$@"; do
    out+=$(printf '%q ' "${arg}")
  done
  printf '%s' "${out}"
}

run() {
  log "+ $(print_cmd "$@")"
  "$@"
}

banner() {
  local msg="$1"
  printf '%s==========================================%s\n' "${_YELLOW}" "${_RESET}"
  printf '%sBuilding %s%s\n' "${_YELLOW}" "${msg}" "${_RESET}"
  printf '%s==========================================%s\n' "${_YELLOW}" "${_RESET}"
}

banner_phase() {
  local phase="$1"
  local msg="$2"
  printf '%s==========================================%s\n' "${_YELLOW}" "${_RESET}"
  printf '%s%s %s%s\n' "${_YELLOW}" "${phase}" "${msg}" "${_RESET}"
  printf '%s==========================================%s\n' "${_YELLOW}" "${_RESET}"
}

setup_logging() {
  local log_file="$1"
  if [[ -z "${log_file}" ]]; then
    return 0
  fi
  mkdir -p "$(dirname -- "${log_file}")"
  exec > >(tee -a "${log_file}") 2>&1
  printf 'Log file: %s\n' "${log_file}"
}

on_err() {
  local exit_code=$?
  log "ERROR: package='${CURRENT_PKG:-?}' phase='${CURRENT_PHASE:-?}' exit=${exit_code} (line ${BASH_LINENO[0]})"
  exit "${exit_code}"
}
trap on_err ERR

# Builds a clang static dependency stack (Release+Debug) into:
#   - Release prefix: /Users/s02299/MOS
#   - Debug prefix:   /Users/s02299/MOSd
#
# Sources are expected under:
#   - /Users/s02299/GH (override via SRC_ROOT)
#
# Goal:
# - Provide a reproducible “static prefix” suitable for building OpenImageIO
#
# Notes:
# - On Linux/WSL, this script intentionally strips "-stdlib=libc++" from flags.
# - On macOS, it uses Apple Clang + libc++ and avoids lld-specific linker flags.
# - Most projects are configured with tests OFF, but tools/examples are often ON
#   when they are useful for quick smoke checks.

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

OS_NAME="$(uname -s)"
IS_MACOS=0
if [[ "${OS_NAME}" == "Darwin" ]]; then
  IS_MACOS=1
fi

if [[ "${IS_MACOS}" -eq 1 ]]; then
  DEFAULT_SRC_ROOT="${HOME}/GH"
else
  DEFAULT_SRC_ROOT="/mnt/e/GH"
fi
SRC_ROOT="${SRC_ROOT:-${DEFAULT_SRC_ROOT}}"
BUILD_ROOT="${BUILD_ROOT:-${SRC_ROOT}/_build_UBG}"

PREFIX_RELEASE="${PREFIX_RELEASE:-/Users/s02299/MOS}"
PREFIX_DEBUG="${PREFIX_DEBUG:-/Users/s02299/MOSd}"

LOG_FILE_DEFAULT="${BUILD_ROOT}/logs/ubg_stack_$(date +%Y%m%d_%H%M%S).log"
# Set LOG_FILE="" to disable logging.
LOG_FILE="${LOG_FILE:-${LOG_FILE_DEFAULT}}"

if [[ "${IS_MACOS}" -eq 1 ]]; then
  CC_BIN="${CC_BIN:-clang}"
  CXX_BIN="${CXX_BIN:-clang++}"
  LD_BIN="${LD_BIN:-ld}"
  AR_BIN="${AR_BIN:-ar}"
  RANLIB_BIN="${RANLIB_BIN:-ranlib}"
else
  CC_BIN="${CC_BIN:-clang-20}"
  CXX_BIN="${CXX_BIN:-clang++-20}"
  LD_BIN="${LD_BIN:-ld.lld-20}"
  AR_BIN="${AR_BIN:-llvm-ar-20}"
  RANLIB_BIN="${RANLIB_BIN:-llvm-ranlib-20}"
fi

default_jobs() {
  if command -v nproc >/dev/null 2>&1; then
    nproc
  elif command -v sysctl >/dev/null 2>&1; then
    sysctl -n hw.ncpu 2>/dev/null || echo 4
  else
    echo 4
  fi
}
JOBS="${JOBS:-$(default_jobs)}"

# Enable/disable larger groups.
BUILD_GL_STACK="${BUILD_GL_STACK:-ON}"        # glfw/freeglut/glew
BUILD_EXR_STACK="${BUILD_EXR_STACK:-ON}"      # Imath/openjph/OpenEXR
BUILD_IMAGEIO_STACK="${BUILD_IMAGEIO_STACK:-ON}"  # png/jpeg/tiff/openjpeg/jasper/gif
BUILD_GTEST="${BUILD_GTEST:-OFF}"            # googletest (only needed for libjxl tests)
BUILD_LIBJXL="${BUILD_LIBJXL:-ON}"
BUILD_LIBUHDR="${BUILD_LIBUHDR:-ON}"
BUILD_OCIO="${BUILD_OCIO:-ON}"
if [[ "${IS_MACOS}" -eq 1 ]]; then
  DEFAULT_OPENJPEG_BUILD_CODEC="OFF"
else
  DEFAULT_OPENJPEG_BUILD_CODEC="ON"
fi
OPENJPEG_BUILD_CODEC="${OPENJPEG_BUILD_CODEC:-${DEFAULT_OPENJPEG_BUILD_CODEC}}"
LIBJXL_ENABLE_TOOLS="${LIBJXL_ENABLE_TOOLS:-ON}"
OCIO_BUILD_APPS="${OCIO_BUILD_APPS:-OFF}"

# Prefer CMake-based builds when available.
XZ_USE_AUTOTOOLS="${XZ_USE_AUTOTOOLS:-OFF}"
LCMS2_USE_AUTOTOOLS="${LCMS2_USE_AUTOTOOLS:-OFF}"

strip_libcxx_flag() {
  local s="${1:-}"
  if [[ "${IS_MACOS}" -eq 1 ]]; then
    echo "${s}" | tr -s ' '
    return 0
  fi
  s="${s//-stdlib=libc++/}"
  echo "${s}" | tr -s ' '
}

find_xcrun_tool() {
  local name="$1"
  if command -v xcrun >/dev/null 2>&1; then
    xcrun --find "${name}" 2>/dev/null || true
  fi
}

resolve_prog() {
  local p="$1"
  if [[ -z "${p}" ]]; then
    return 1
  fi
  if [[ "${p}" == /* && -x "${p}" ]]; then
    echo "${p}"
    return 0
  fi
  if command -v "${p}" >/dev/null 2>&1; then
    command -v "${p}"
    return 0
  fi
  return 1
}

patch_glew_cmake_macos() {
  local src="$1"
  if [[ "${IS_MACOS}" -ne 1 ]]; then
    return 0
  fi
  local cmake_lists="${src}/CMakeLists.txt"
  if [[ ! -f "${cmake_lists}" ]]; then
    return 0
  fi
  if grep -q "AGL_LIBRARY AGL REQUIRED" "${cmake_lists}"; then
    log "Patching glew-cmake to make AGL optional on macOS"
    run perl -0pi -e 's/find_library\(AGL_LIBRARY AGL REQUIRED\)\s*\n\s*list\(APPEND LIBRARIES \\\$\{AGL_LIBRARY\}\)/find_library(AGL_LIBRARY AGL)\n  if(AGL_LIBRARY)\n    list(APPEND LIBRARIES ${AGL_LIBRARY})\n  endif()/s' "${cmake_lists}"
  fi
}

patch_libjxl_openexr_static() {
  local src="$1"
  local cmake_file="${src}/lib/jxl_extras.cmake"
  if [[ ! -f "${cmake_file}" ]]; then
    return 0
  fi
  if rg -q "JXL_OPENEXR_STATIC_PATCH" "${cmake_file}"; then
    return 0
  fi
  log "Patching libjxl to use OpenEXR static libs when available"
  run perl -0pi -e 's/list\\(APPEND JXL_EXTRAS_CODEC_INTERNAL_LIBRARIES PkgConfig::OpenEXR\\)/# JXL_OPENEXR_STATIC_PATCH\\n    list(APPEND JXL_EXTRAS_CODEC_INTERNAL_LIBRARIES PkgConfig::OpenEXR ${OpenEXR_STATIC_LIBRARIES})/s' "${cmake_file}"
  run perl -0pi -e 's/(if\\s*\\(\\s*OpenEXR_FOUND\\s*\\)\\s*\\n)/$1  # JXL_OPENEXR_STATIC_PATCH\\n  if (OpenEXR_STATIC_LIBRARIES AND TARGET PkgConfig::OpenEXR)\\n    set_property(TARGET PkgConfig::OpenEXR APPEND PROPERTY INTERFACE_LINK_LIBRARIES \"${OpenEXR_STATIC_LIBRARIES}\")\\n  endif()\\n  if (OpenEXR_LIBRARY_DIRS AND TARGET PkgConfig::OpenEXR)\\n    set_property(TARGET PkgConfig::OpenEXR APPEND PROPERTY INTERFACE_LINK_DIRECTORIES \"${OpenEXR_LIBRARY_DIRS}\")\\n  endif()\\n/s' "${cmake_file}"
}

if [[ "${IS_MACOS}" -eq 1 ]]; then
  CC_BIN="$(resolve_prog "${CC_BIN}" || resolve_prog "$(find_xcrun_tool clang)" || resolve_prog clang)"
  CXX_BIN="$(resolve_prog "${CXX_BIN}" || resolve_prog "$(find_xcrun_tool clang++)" || resolve_prog clang++)"
  LD_BIN="$(resolve_prog "${LD_BIN}" || resolve_prog "$(find_xcrun_tool ld)" || resolve_prog ld)"
  AR_BIN="$(resolve_prog "${AR_BIN}" || resolve_prog "$(find_xcrun_tool ar)" || resolve_prog ar)"
  RANLIB_BIN="$(resolve_prog "${RANLIB_BIN}" || resolve_prog "$(find_xcrun_tool ranlib)" || resolve_prog ranlib)"
else
  CC_BIN="$(resolve_prog "${CC_BIN}" || resolve_prog clang-20 || resolve_prog clang)"
  CXX_BIN="$(resolve_prog "${CXX_BIN}" || resolve_prog clang++-20 || resolve_prog clang++)"
  LD_BIN="$(resolve_prog "${LD_BIN}" || resolve_prog /usr/bin/ld.lld-20 || resolve_prog ld.lld)"
  AR_BIN="$(resolve_prog "${AR_BIN}" || resolve_prog llvm-ar-20 || resolve_prog llvm-ar || resolve_prog ar)"
  RANLIB_BIN="$(resolve_prog "${RANLIB_BIN}" || resolve_prog llvm-ranlib-20 || resolve_prog llvm-ranlib || resolve_prog ranlib)"
fi

if [[ "${IS_MACOS}" -eq 1 ]]; then
  CXX_STDLIB_FLAG="-stdlib=libc++"
  LINKER_FLAGS_INIT=""
else
  CXX_STDLIB_FLAG=""
LINKER_FLAGS_INIT="-fuse-ld=lld"
fi

PKG_CONFIG_OVERRIDE_ROOT="${PKG_CONFIG_OVERRIDE_ROOT:-${BUILD_ROOT}/pkgconfig_override}"

make_openexr_pc_override() {
  local prefix="$1"
  local cfg="$2"
  local src="${prefix}/lib/pkgconfig/OpenEXR.pc"
  if [[ ! -f "${src}" ]]; then
    return 0
  fi
  local openjph_lib="openjph"
  if [[ "${cfg}" == "Debug" && -f "${prefix}/lib/libopenjph_d.a" ]]; then
    openjph_lib="openjph_d"
  fi
  local override_dir="${PKG_CONFIG_OVERRIDE_ROOT}/${cfg}"
  local dst="${override_dir}/OpenEXR.pc"
  mkdir -p "${override_dir}"
  # If already patched, keep as-is.
  if [[ -f "${dst}" ]] && rg -q "openjph" "${dst}"; then
    return 0
  fi
  awk -v ojph="${openjph_lib}" '
    /^Libs:/ {
      if ($0 ~ /openjph/ || $0 ~ /deflate/) { print; next }
      print $0 " -ldeflate -l" ojph
      next
    }
    { print }
  ' "${src}" > "${dst}"
}

find_src_dir() {
  local label="$1"; shift
  local -a patterns=("$@")

  for pat in "${patterns[@]}"; do
    # Allow both exact paths and glob patterns relative to SRC_ROOT.
    local abs="${SRC_ROOT}/${pat}"
    if [[ -d "${abs}" ]]; then
      echo "${abs}"
      return 0
    fi

    # Try glob expansion (e.g. giflib-*).
    local matches=()
    # shellcheck disable=SC2206
    matches=( ${SRC_ROOT}/${pat} )
    for m in "${matches[@]}"; do
      if [[ -d "${m}" ]]; then
        echo "${m}"
        return 0
      fi
    done
  done

  echo "Missing source dir for ${label} under ${SRC_ROOT}. Tried: ${patterns[*]}" >&2
  exit 2
}

base_flags_for() {
  local cfg="$1"
  if [[ "${cfg}" == "Debug" ]]; then
    echo "-O0 -g -fPIC"
  else
    echo "-O3 -DNDEBUG -fPIC"
  fi
}

cmake_build_install() {
  local name="$1"; shift
  local src="$1"; shift
  local cfg="$1"; shift
  local prefix="$1"; shift
  local -a extra_args=()
  if (( $# )); then
    extra_args=("$@")
  fi

  CURRENT_PKG="${name}"
  CURRENT_PHASE="configure"

  local bld="${BUILD_ROOT}/${cfg}/${name}"
  # If the cached toolchain points at missing tools (e.g. moved llvm-ar),
  # discard the build dir to avoid confusing errors.
  if [[ -f "${bld}/CMakeCache.txt" ]]; then
    local cached_ar=""
    cached_ar="$(grep -m1 '^CMAKE_AR:FILEPATH=' "${bld}/CMakeCache.txt" | cut -d= -f2- || true)"
    if [[ -n "${cached_ar}" && ! -x "${cached_ar}" ]]; then
      rm -rf "${bld}"
    fi
    local cached_ranlib=""
    cached_ranlib="$(grep -m1 '^CMAKE_RANLIB:FILEPATH=' "${bld}/CMakeCache.txt" | cut -d= -f2- || true)"
    if [[ -n "${cached_ranlib}" && ! -x "${cached_ranlib}" ]]; then
      rm -rf "${bld}"
    fi
  fi
  mkdir -p "${bld}" "${prefix}"

  local cflags
  cflags="$(strip_libcxx_flag "$(base_flags_for "${cfg}")")"
  local cxxflags
  cxxflags="$(strip_libcxx_flag "$(base_flags_for "${cfg}") ${CXX_STDLIB_FLAG}")"

  local override_dir="${PKG_CONFIG_OVERRIDE_ROOT}/${cfg}"
  if [[ -d "${override_dir}" ]]; then
    export PKG_CONFIG_PATH="${override_dir}:${prefix}/lib/pkgconfig:${prefix}/share/pkgconfig:${PKG_CONFIG_PATH:-}"
  else
    export PKG_CONFIG_PATH="${prefix}/lib/pkgconfig:${prefix}/share/pkgconfig:${PKG_CONFIG_PATH:-}"
  fi

  local -a common=(
    -G Ninja
    -DCMAKE_BUILD_TYPE="${cfg}"
    -DCMAKE_INSTALL_PREFIX="${prefix}"
    -DCMAKE_PREFIX_PATH="${prefix}"
    -DCMAKE_INCLUDE_PATH="${prefix}/include"
    -DCMAKE_LIBRARY_PATH="${prefix}/lib"
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON
    -DBUILD_SHARED_LIBS=OFF
    -DCMAKE_C_COMPILER="${CC_BIN}"
    -DCMAKE_CXX_COMPILER="${CXX_BIN}"
    -DCMAKE_LINKER="${LD_BIN}"
    -DCMAKE_AR="${AR_BIN}"
    -DCMAKE_RANLIB="${RANLIB_BIN}"
    -DCMAKE_CXX_STANDARD=20
    -DCMAKE_CXX_EXTENSIONS=OFF
    -DCMAKE_C_FLAGS_INIT="${cflags}"
    -DCMAKE_CXX_FLAGS_INIT="${cxxflags}"
    -DPKG_CONFIG_USE_STATIC_LIBS=ON
  )
  if [[ -n "${LINKER_FLAGS_INIT}" ]]; then
    common+=(
      -DCMAKE_EXE_LINKER_FLAGS_INIT="${LINKER_FLAGS_INIT}"
      -DCMAKE_SHARED_LINKER_FLAGS_INIT="${LINKER_FLAGS_INIT}"
      -DCMAKE_MODULE_LINKER_FLAGS_INIT="${LINKER_FLAGS_INIT}"
    )
  fi

  banner_phase "Configure" "${name} (${cfg})"
  log "Configuring ${name}"
  log "  src=${src}"
  log "  bld=${bld}"
  log "  prefix=${prefix}"
  if (( ${#extra_args[@]} )); then
    run cmake -S "${src}" -B "${bld}" "${common[@]}" "${extra_args[@]}"
  else
    run cmake -S "${src}" -B "${bld}" "${common[@]}"
  fi

  CURRENT_PHASE="build"
  banner "${name} (${cfg})"
  log "Building ${name}"
  run cmake --build "${bld}" -j "${JOBS}"

  CURRENT_PHASE="install"
  banner_phase "Install" "${name} (${cfg})"
  log "Installing ${name}"
  run cmake --install "${bld}"
}

autotools_build_install() {
  local name="$1"; shift
  local src="$1"; shift
  local cfg="$1"; shift
  local prefix="$1"; shift
  local -a extra_args=()
  if (( $# )); then
    extra_args=("$@")
  fi

  CURRENT_PKG="${name}"
  CURRENT_PHASE="bootstrap"

  require_cmd() {
    local c="$1"
    if ! command -v "${c}" >/dev/null 2>&1; then
      echo "Missing required build tool '${c}' for autotools project '${name}'." >&2
      echo "Install it (WSL/Ubuntu): sudo apt install -y autoconf automake libtool gettext" >&2
      exit 2
    fi
  }

  fix_crlf_in_place() {
    local f="$1"
    if [[ -f "${f}" ]]; then
      sed -i 's/\r$//' "${f}" || true
    fi
  }

  run_script() {
    local f="$1"
    local label="$2"
    fix_crlf_in_place "${f}"
    if command -v bash >/dev/null 2>&1; then
      bash "${f}"
    else
      sh "${f}"
    fi
  }

  if [[ ! -f "${src}/configure" ]]; then
    # If we're building from a git checkout, make sure the autotools toolchain
    # exists before attempting to generate configure scripts.
    require_cmd autoconf
    require_cmd automake
    # Some projects call libtoolize, others glibtoolize.
    if ! command -v libtoolize >/dev/null 2>&1 && ! command -v glibtoolize >/dev/null 2>&1; then
      echo "Missing required build tool 'libtoolize' (or 'glibtoolize') for '${name}'." >&2
      echo "Install it (WSL/Ubuntu): sudo apt install -y libtool" >&2
      exit 2
    fi
    # Many projects require autopoint (gettext) during bootstrap.
    if [[ -f "${src}/autogen.sh" || -f "${src}/bootstrap" ]]; then
      require_cmd autopoint
    fi

    if [[ -f "${src}/autogen.sh" ]]; then
      (cd "${src}" && run_script "./autogen.sh" "autogen.sh")
    elif [[ -f "${src}/bootstrap" ]]; then
      (cd "${src}" && run_script "./bootstrap" "bootstrap")
    elif command -v autoreconf >/dev/null 2>&1; then
      (cd "${src}" && autoreconf -fi)
    else
      echo "Missing ${src}/configure; also no autogen.sh/bootstrap (and no autoreconf) for ${name}" >&2
      exit 2
    fi
  fi
  fix_crlf_in_place "${src}/configure"

  local bld="${BUILD_ROOT}/${cfg}/${name}"
  rm -rf "${bld}"
  mkdir -p "${bld}" "${prefix}"

  local cflags
  cflags="$(strip_libcxx_flag "$(base_flags_for "${cfg}")")"
  local cxxflags
  cxxflags="$(strip_libcxx_flag "$(base_flags_for "${cfg}") ${CXX_STDLIB_FLAG}")"

  CURRENT_PHASE="configure"
  banner_phase "Configure" "${name} (${cfg})"
  log "Configuring ${name}"
  log "  src=${src}"
  log "  bld=${bld}"
  log "  prefix=${prefix}"
  (
    cd "${bld}"
    CC="${CC_BIN}" CXX="${CXX_BIN}" AR="${AR_BIN}" RANLIB="${RANLIB_BIN}" \
      CFLAGS="${cflags}" CXXFLAGS="${cxxflags}" LDFLAGS="${LINKER_FLAGS_INIT}" \
      run sh "${src}/configure" --prefix="${prefix}" --disable-shared --enable-static \
      "${extra_args[@]}"

    CURRENT_PHASE="build"
    banner "${name} (${cfg})"
    log "Building ${name}"
    run make -j "${JOBS}"

    CURRENT_PHASE="install"
    banner_phase "Install" "${name} (${cfg})"
    log "Installing ${name}"
    run make install
  )
}

giflib_build_install() {
  local name="$1"; shift
  local src="$1"; shift
  local cfg="$1"; shift
  local prefix="$1"; shift

  CURRENT_PKG="${name}"
  CURRENT_PHASE="build/install"

  local cflags
  cflags="$(strip_libcxx_flag "$(base_flags_for "${cfg}")")"
  cflags="${cflags} -std=gnu99 -Wall -Wno-format-truncation"

  log "Building/Installing ${name}"
  log "  src=${src}"
  log "  prefix=${prefix}"
  banner "${name} (${cfg})"
  (
    cd "${src}"
    run make clean || true
    run make -j "${JOBS}" CC="${CC_BIN}" CFLAGS="${cflags}" \
      PREFIX="${prefix}" BINDIR="${prefix}/bin" INCDIR="${prefix}/include" \
      LIBDIR="${prefix}/lib" MANDIR="${prefix}/share/man" \
      libgif.a libutil.a gif2rgb gifbuild giffix giftext giftool gifclrmp

    # Some giflib Makefiles always try to install a shared library from the
    # `install-lib` target even when only static libs were built. Install
    # artifacts explicitly to keep the stack fully static and portable.
    banner_phase "Install" "${name} (${cfg})"
    run install -d "${prefix}/bin" "${prefix}/include" "${prefix}/lib"
    run install gif2rgb gifbuild giffix giftext giftool gifclrmp "${prefix}/bin"
    run install -m 644 gif_lib.h "${prefix}/include/gif_lib.h"
    run install -m 644 libgif.a "${prefix}/lib/libgif.a"
    run install -m 644 libutil.a "${prefix}/lib/libutil.a"
  )
}

ensure_file() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "Expected file not found: ${path}" >&2
    exit 3
  fi
}

require_dir() {
  local path="$1"
  local label="$2"
  if [[ ! -d "${path}" ]]; then
    echo "Missing source dir for ${label}: ${path}" >&2
    exit 2
  fi
}

build_for_cfg() {
  local cfg="$1"
  local prefix="$2"
  CURRENT_CFG="${cfg}"
  CURRENT_PKG=""
  CURRENT_PHASE="init"

  echo
  echo "=== Building ${cfg} into ${prefix} ==="
  mkdir -p "${prefix}" "${BUILD_ROOT}/${cfg}"

  log "Toolchain:"
  log "  CC=${CC_BIN}"
  log "  CXX=${CXX_BIN}"
  log "  LD=${LD_BIN}"
  log "  AR=${AR_BIN}"
  log "  RANLIB=${RANLIB_BIN}"

  # Resolve source directories (your local clones may be version-suffixed).
  local zlib_ng_src="${ZLIB_NG_SRC:-$(find_src_dir "zlib-ng" "zlib-ng" "zlib-ng-*")}"
  local xz_src="${XZ_SRC:-$(find_src_dir "xz (liblzma)" "xz" "xz-*")}"
  local libdeflate_src="${LIBDEFLATE_SRC:-$(find_src_dir "libdeflate" "libdeflate" "libdeflate-*")}"
  local zstd_src="${ZSTD_SRC:-$(find_src_dir "zstd" "zstd" "zstd-*")}"
  local zstd_cmake_src="${ZSTD_CMAKE_SRC:-"${zstd_src}/build/cmake"}"
  local libjpeg_turbo_src="${LIBJPEG_TURBO_SRC:-$(find_src_dir "libjpeg-turbo" "libjpeg-turbo" "libjpeg-turbo-*")}"
  local libpng_src="${LIBPNG_SRC:-$(find_src_dir "libpng" "libpng" "libpng-*")}"
  local brotli_src="${BROTLI_SRC:-$(find_src_dir "brotli" "brotli" "brotli-*")}"
  local highway_src="${HIGHWAY_SRC:-$(find_src_dir "highway" "highway" "highway-*")}"
  local lcms2_src="${LCMS2_SRC:-$(find_src_dir "Little-CMS (lcms2)" "Little-CMS" "Little-CMS-*" "lcms2" "lcms2-*")}"

  if [[ "${BUILD_GL_STACK}" == "ON" ]]; then
    local glfw_src="${GLFW_SRC:-$(find_src_dir "glfw" "glfw" "glfw-*")}"
    local freeglut_src="${FREEGLUT_SRC:-$(find_src_dir "freeglut" "freeglut" "freeglut-*")}"
    local glew_src="${GLEW_SRC:-$(find_src_dir "glew-cmake" "glew-cmake" "glew-cmake-*")}"
  fi

  if [[ "${BUILD_IMAGEIO_STACK}" == "ON" ]]; then
    local libtiff_src="${LIBTIFF_SRC:-$(find_src_dir "libtiff" "libtiff" "libtiff-*")}"
    local openjpeg_src="${OPENJPEG_SRC:-$(find_src_dir "openjpeg" "openjpeg" "openjpeg-*")}"
    local jasper_src="${JASPER_SRC:-$(find_src_dir "jasper" "jasper" "jasper-*")}"
    # Common naming variants: giflib, giflib-5.2.2, giflib-5.2.2.tar/... extracted.
    local giflib_src="${GIFLIB_SRC:-$(find_src_dir "giflib" "giflib" "giflib-*" "gif*")}"
  fi

  if [[ "${BUILD_EXR_STACK}" == "ON" || "${BUILD_OCIO}" == "ON" ]]; then
    local imath_src="${IMATH_SRC:-$(find_src_dir "Imath" "Imath" "Imath-*")}"
  fi
  if [[ "${BUILD_EXR_STACK}" == "ON" ]]; then
    local openexr_src="${OPENEXR_SRC:-$(find_src_dir "OpenEXR" "openexr" "openexr-*" "OpenEXR" "OpenEXR-*")}"
    local openjph_src="${OPENJPH_SRC:-$(find_src_dir "openjph" "openjph" "openjph-*" "OpenJPH" "OpenJPH-*")}"
  fi
  if [[ "${BUILD_GTEST}" == "ON" ]]; then
    local gtest_src="${GTEST_SRC:-$(find_src_dir "googletest" "googletest" "googletest-*" "gtest" "gtest-*")}"
  fi
  if [[ "${BUILD_LIBJXL}" == "ON" ]]; then
    local libjxl_src="${LIBJXL_SRC:-$(find_src_dir "libjxl" "libjxl" "libjxl-*")}"
  fi
  if [[ "${BUILD_LIBUHDR}" == "ON" ]]; then
    local libultrahdr_src="${LIBUHDR_SRC:-$(find_src_dir "libultrahdr" "libultrahdr" "libultrahdr-*")}"
  fi
  if [[ "${BUILD_OCIO}" == "ON" ]]; then
    local ocio_src="${OCIO_SRC:-$(find_src_dir "OpenColorIO" "OpenColorIO" "OpenColorIO-*")}"
    local minizip_ng_src="${MINIZIP_NG_SRC:-$(find_src_dir "minizip-ng" "minizip-ng" "minizip-ng-*")}"
  fi

  log "Resolved sources:"
  log "  zlib-ng=${zlib_ng_src}"
  log "  xz=${xz_src}"
  log "  libdeflate=${libdeflate_src}"
  log "  zstd=${zstd_cmake_src}"
  log "  libjpeg-turbo=${libjpeg_turbo_src}"
  log "  libpng=${libpng_src}"
  log "  brotli=${brotli_src}"
  log "  highway=${highway_src}"
  log "  lcms2=${lcms2_src}"
  if [[ "${BUILD_LIBJXL}" == "ON" ]]; then
    log "  libjxl=${libjxl_src}"
  fi
  if [[ "${BUILD_LIBUHDR}" == "ON" ]]; then
    log "  libultrahdr=${libultrahdr_src}"
  fi
  if [[ "${BUILD_OCIO}" == "ON" ]]; then
    log "  OpenColorIO=${ocio_src}"
    log "  minizip-ng=${minizip_ng_src}"
  fi

  # Fail fast if mandatory sources are missing.
  require_dir "${zlib_ng_src}" "zlib-ng"
  require_dir "${xz_src}" "xz (liblzma)"
  require_dir "${libdeflate_src}" "libdeflate"
  require_dir "${zstd_cmake_src}" "zstd (CMake at build/cmake)"
  require_dir "${libjpeg_turbo_src}" "libjpeg-turbo"
  require_dir "${libpng_src}" "libpng"
  require_dir "${brotli_src}" "brotli"
  require_dir "${highway_src}" "highway"
  require_dir "${lcms2_src}" "lcms2"

  if [[ "${BUILD_GL_STACK}" == "ON" ]]; then
    require_dir "${glfw_src}" "glfw"
    require_dir "${freeglut_src}" "freeglut"
    require_dir "${glew_src}" "glew-cmake"
  fi
  if [[ "${BUILD_IMAGEIO_STACK}" == "ON" ]]; then
    require_dir "${libtiff_src}" "libtiff"
    require_dir "${openjpeg_src}" "openjpeg"
    require_dir "${jasper_src}" "jasper"
    require_dir "${giflib_src}" "giflib"
  fi
  if [[ "${BUILD_EXR_STACK}" == "ON" ]]; then
    require_dir "${imath_src}" "Imath"
    require_dir "${openexr_src}" "OpenEXR"
    require_dir "${openjph_src}" "openjph"
  fi
  if [[ "${BUILD_OCIO}" == "ON" && "${BUILD_EXR_STACK}" != "ON" ]]; then
    require_dir "${imath_src}" "Imath"
  fi
  if [[ "${BUILD_GTEST}" == "ON" ]]; then
    require_dir "${gtest_src}" "googletest"
  fi
  if [[ "${BUILD_LIBJXL}" == "ON" ]]; then
    require_dir "${libjxl_src}" "libjxl"
  fi
  if [[ "${BUILD_LIBUHDR}" == "ON" ]]; then
    require_dir "${libultrahdr_src}" "libultrahdr"
  fi
  if [[ "${BUILD_OCIO}" == "ON" ]]; then
    require_dir "${ocio_src}" "OpenColorIO"
    require_dir "${minizip_ng_src}" "minizip-ng"
  fi

  # ---- Base compression / containers (mandatory for your workflow) ----
  cmake_build_install zlib-ng "${zlib_ng_src}" "${cfg}" "${prefix}" \
    -DZLIB_COMPAT=ON \
    -DWITH_GTEST=OFF -DWITH_FUZZERS=OFF \
    -DWITH_BENCHMARKS=OFF -DWITH_BENCHMARK_APPS=OFF
  ensure_file "${prefix}/include/zlib.h"
  ensure_file "${prefix}/lib/libz.a"

  # xz (liblzma)
  # Your workflow file uses a CMake build; prefer that unless explicitly asked
  # to bootstrap autotools (which may require gettext/autopoint).
  if [[ -f "${xz_src}/CMakeLists.txt" && "${XZ_USE_AUTOTOOLS}" == "OFF" ]]; then
    log "Using CMake for xz (set XZ_USE_AUTOTOOLS=ON to force autotools)"
    cmake_build_install xz "${xz_src}" "${cfg}" "${prefix}" \
      -DBUILD_SHARED_LIBS=OFF
  else
    if [[ "${XZ_USE_AUTOTOOLS}" == "OFF" ]]; then
      echo "xz: no CMakeLists.txt found at ${xz_src}. Provide a CMake-capable xz checkout," >&2
      echo "or set XZ_USE_AUTOTOOLS=ON (requires gettext/autopoint) to bootstrap autotools." >&2
      exit 2
    fi
    autotools_build_install xz "${xz_src}" "${cfg}" "${prefix}" \
      --disable-nls \
      --disable-xz --disable-xzdec --disable-lzmadec --disable-lzmainfo
  fi
  ensure_file "${prefix}/lib/liblzma.a"

  cmake_build_install libdeflate "${libdeflate_src}" "${cfg}" "${prefix}" \
    -DLIBDEFLATE_BUILD_STATIC_LIB=ON \
    -DLIBDEFLATE_BUILD_SHARED_LIB=OFF \
    -DLIBDEFLATE_BUILD_TESTS=OFF \
    -DLIBDEFLATE_BUILD_GZIP=ON
  ensure_file "${prefix}/lib/libdeflate.a"

  # zstd - CMake project is under build/cmake
  cmake_build_install zstd "${zstd_cmake_src}" "${cfg}" "${prefix}" \
    -DZSTD_BUILD_PROGRAMS=ON \
    -DZSTD_BUILD_TESTS=OFF \
    -DZSTD_BUILD_SHARED=OFF \
    -DZSTD_BUILD_STATIC=ON
  ensure_file "${prefix}/lib/libzstd.a"

  # ---- “plumbing” libs frequently used in OIIO stacks ----
  if [[ -d "${SRC_ROOT}/libiconv" ]]; then
    cmake_build_install libiconv "${SRC_ROOT}/libiconv" "${cfg}" "${prefix}" \
      -DCMAKE_POLICY_VERSION_MINIMUM=3.5
  fi

  if [[ -d "${SRC_ROOT}/libxml2" ]]; then
    cmake_build_install libxml2 "${SRC_ROOT}/libxml2" "${cfg}" "${prefix}" \
      -DLIBXML2_WITH_LZMA=ON \
      -DLIBXML2_WITH_PYTHON=OFF \
      -DLIBXML2_WITH_TESTS=OFF \
      -DLIBXML2_WITH_PROGRAMS=OFF
  fi

  if [[ "${BUILD_GL_STACK}" == "ON" ]]; then
    cmake_build_install glfw "${glfw_src}" "${cfg}" "${prefix}" \
      -DGLFW_BUILD_EXAMPLES=ON \
      -DGLFW_BUILD_TESTS=OFF \
      -DGLFW_BUILD_DOCS=OFF
    ensure_file "${prefix}/lib/libglfw3.a"

    cmake_build_install freeglut "${freeglut_src}" "${cfg}" "${prefix}" \
      -DFREEGLUT_BUILD_STATIC_LIBS=ON \
      -DFREEGLUT_BUILD_SHARED_LIBS=OFF \
      -DFREEGLUT_BUILD_DEMOS=ON

    patch_glew_cmake_macos "${glew_src}"
    if [[ "${IS_MACOS}" -eq 1 ]]; then
      cmake_build_install glew "${glew_src}" "${cfg}" "${prefix}" \
        -Dglew-cmake_BUILD_SHARED=OFF \
        -Dglew-cmake_BUILD_STATIC=ON \
        -DONLY_LIBS=ON
    else
      cmake_build_install glew "${glew_src}" "${cfg}" "${prefix}" \
        -DBUILD_UTILS=ON
    fi
  fi

  # ---- Image IO libs (typical OIIO / toolchain) ----
  if [[ "${BUILD_IMAGEIO_STACK}" == "ON" ]]; then
    cmake_build_install libjpeg-turbo "${libjpeg_turbo_src}" "${cfg}" "${prefix}" \
      -DENABLE_SHARED=OFF -DENABLE_STATIC=ON \
      -DWITH_JPEG7=ON -DWITH_JPEG8=ON -DREQUIRE_SIMD=ON
    ensure_file "${prefix}/lib/libjpeg.a"

    cmake_build_install libpng "${libpng_src}" "${cfg}" "${prefix}" \
      -DPNG_SHARED=OFF -DPNG_STATIC=ON -DPNG_TESTS=OFF
    ensure_file "${prefix}/lib/libpng.a"

    cmake_build_install libtiff "${libtiff_src}" "${cfg}" "${prefix}" \
      -Dtiff-tests=OFF -Dtiff-tools=ON -Dtiff-docs=OFF -Dtiff-contrib=OFF \
      -Dtiff-opengl=OFF \
      -DJPEG_SUPPORT=ON -DJPEG_DUAL_MODE_8_12=ON
    ensure_file "${prefix}/lib/libtiff.a"

    local -a openjpeg_args=(
      -DBUILD_CODEC="${OPENJPEG_BUILD_CODEC}"
    )
    if [[ "${IS_MACOS}" -eq 1 && "${OPENJPEG_BUILD_CODEC}" == "ON" ]]; then
      openjpeg_args+=(
        -DCMAKE_EXE_LINKER_FLAGS_INIT="-L${prefix}/lib"
        -DCMAKE_SHARED_LINKER_FLAGS_INIT="-L${prefix}/lib"
        -DCMAKE_MODULE_LINKER_FLAGS_INIT="-L${prefix}/lib"
      )
    fi
    cmake_build_install openjpeg "${openjpeg_src}" "${cfg}" "${prefix}" \
      "${openjpeg_args[@]}"

    cmake_build_install jasper "${jasper_src}" "${cfg}" "${prefix}" \
      -DBUILD_TESTING=OFF \
      -DJAS_ENABLE_PROGRAMS=OFF \
      -DJAS_ENABLE_LIBJPEG=ON \
      -DJAS_ENABLE_SHARED=OFF \
      -DALLOW_IN_SOURCE_BUILD=ON

    if [[ -d "${SRC_ROOT}/pugixml" ]]; then
      cmake_build_install pugixml "${SRC_ROOT}/pugixml" "${cfg}" "${prefix}" \
        -DBUILD_TESTING=OFF
    fi

    giflib_build_install giflib "${giflib_src}" "${cfg}" "${prefix}"
    ensure_file "${prefix}/lib/libgif.a"
  fi

  # ---- libjxl core deps ----
  cmake_build_install brotli "${brotli_src}" "${cfg}" "${prefix}" \
    -DBROTLI_DISABLE_TESTS=ON -DBROTLI_BUILD_TOOLS=OFF
  ensure_file "${prefix}/lib/libbrotlicommon.a"

  cmake_build_install highway "${highway_src}" "${cfg}" "${prefix}" \
    -DHWY_ENABLE_TESTS=OFF \
    -DHWY_ENABLE_EXAMPLES=OFF \
    -DHWY_ENABLE_CONTRIB=OFF \
    -DHWY_FORCE_STATIC_LIBS=ON \
    -DHWY_SYSTEM_GTEST=ON \
    -DHWY_ENABLE_INSTALL=ON
  ensure_file "${prefix}/lib/libhwy.a"

  # LCMS2: prefer CMake if provided by your checkout; otherwise require explicit
  # opt-in to autotools (to avoid surprising bootstrap deps).
  if [[ -f "${lcms2_src}/CMakeLists.txt" && "${LCMS2_USE_AUTOTOOLS}" == "OFF" ]]; then
    log "Using CMake for lcms2 (set LCMS2_USE_AUTOTOOLS=ON to force autotools)"
    cmake_build_install lcms2 "${lcms2_src}" "${cfg}" "${prefix}" \
      -DBUILD_TESTING=OFF -DBUILD_TESTS=OFF
  else
    if [[ "${LCMS2_USE_AUTOTOOLS}" == "OFF" ]]; then
      echo "lcms2: no CMakeLists.txt found at ${lcms2_src}. Provide a CMake-capable lcms2 checkout," >&2
      echo "or set LCMS2_USE_AUTOTOOLS=ON to use autotools." >&2
      exit 2
    fi
    autotools_build_install lcms2 "${lcms2_src}" "${cfg}" "${prefix}" \
      --without-fastfloat --without-threaded
  fi
  ensure_file "${prefix}/lib/liblcms2.a"

  # ---- OpenEXR stack (optional, but used by libjxl extras + OIIO) ----
  if [[ "${BUILD_EXR_STACK}" == "ON" ]]; then
    cmake_build_install imath "${imath_src}" "${cfg}" "${prefix}" \
      -DIMATH_BUILD_TESTS=OFF \
      -DIMATH_BUILD_SHARED_LIBS=OFF

    cmake_build_install openjph "${openjph_src}" "${cfg}" "${prefix}" \
      -DOJPH_ENABLE_TIFF_SUPPORT=ON \
      -DOJPH_BUILD_STREAM_EXPAND=ON \
      -DBUILD_TESTING=OFF

    cmake_build_install openexr "${openexr_src}" "${cfg}" "${prefix}" \
      -DOPENEXR_BUILD_TOOLS=ON \
      -DOPENEXR_INSTALL_TOOLS=ON \
      -DOPENEXR_BUILD_EXAMPLES=ON \
      -DOPENEXR_BUILD_TESTS=OFF \
      -DBUILD_TESTING=OFF \
      -DOPENEXR_FORCE_INTERNAL_IMATH=OFF \
      -DOPENEXR_FORCE_INTERNAL_DEFLATE=OFF \
      -DOPENEXR_FORCE_INTERNAL_OPENJPH=OFF
    make_openexr_pc_override "${prefix}" "${cfg}"

    if [[ -d "${SRC_ROOT}/expat/expat" ]]; then
      cmake_build_install expat "${SRC_ROOT}/expat/expat" "${cfg}" "${prefix}" \
        -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=ON
    elif [[ -d "${SRC_ROOT}/libexpat/expat" ]]; then
      cmake_build_install expat "${SRC_ROOT}/libexpat/expat" "${cfg}" "${prefix}" \
        -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=ON
    elif [[ -d "${SRC_ROOT}/expat" ]]; then
      cmake_build_install expat "${SRC_ROOT}/expat" "${cfg}" "${prefix}" \
        -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=ON
    elif [[ -d "${SRC_ROOT}/libexpat" ]]; then
      cmake_build_install expat "${SRC_ROOT}/libexpat" "${cfg}" "${prefix}" \
        -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=ON
    fi
  fi
  if [[ "${BUILD_OCIO}" == "ON" && "${BUILD_EXR_STACK}" != "ON" ]]; then
    cmake_build_install imath "${imath_src}" "${cfg}" "${prefix}" \
      -DIMATH_BUILD_TESTS=OFF \
      -DIMATH_BUILD_SHARED_LIBS=OFF
  fi

  # ---- libjxl (uses brotli/highway/lcms2 + optional image codecs) ----
  if [[ "${BUILD_LIBJXL}" == "ON" ]]; then
    local jxl_enable_openexr="ON"
    if [[ "${BUILD_EXR_STACK}" != "ON" ]]; then
      jxl_enable_openexr="OFF"
    fi
    patch_libjxl_openexr_static "${libjxl_src}"
    cmake_build_install libjxl "${libjxl_src}" "${cfg}" "${prefix}" \
      -DBUILD_TESTING=OFF \
      -DJPEGXL_ENABLE_TOOLS="${LIBJXL_ENABLE_TOOLS}" \
      -DJPEGXL_ENABLE_OPENEXR="${jxl_enable_openexr}" \
      -DJPEGXL_ENABLE_BENCHMARK=OFF \
      -DJPEGXL_ENABLE_DEVTOOLS=OFF \
      -DJPEGXL_ENABLE_EXAMPLES=OFF \
      -DJPEGXL_ENABLE_DOXYGEN=OFF \
      -DJPEGXL_ENABLE_MANPAGES=OFF \
      -DJPEGXL_ENABLE_VIEWERS=OFF \
      -DJPEGXL_ENABLE_JNI=OFF \
      -DJPEGXL_ENABLE_PLUGINS=OFF \
      -DJPEGXL_ENABLE_SKCMS=OFF \
      -DJPEGXL_ENABLE_SJPEG=OFF \
      -DJPEGXL_FORCE_SYSTEM_BROTLI=ON \
      -DJPEGXL_FORCE_SYSTEM_LCMS2=ON \
      -DJPEGXL_FORCE_SYSTEM_HWY=ON \
      -DJPEGXL_FORCE_SYSTEM_GTEST=ON \
      -DJPEGXL_BUNDLE_LIBPNG=OFF
  fi

  # ---- libuhdr (libultrahdr) ----
  if [[ "${BUILD_LIBUHDR}" == "ON" ]]; then
    cmake_build_install libultrahdr "${libultrahdr_src}" "${cfg}" "${prefix}" \
      -DUHDR_BUILD_DEPS=OFF \
      -DUHDR_BUILD_TESTS=OFF \
      -DUHDR_BUILD_BENCHMARK=OFF
  fi

  # ---- Misc small deps used by OIIO stacks ----
  if [[ "${BUILD_OCIO}" == "ON" && ! -f "${prefix}/lib/libexpat.a" ]]; then
    if [[ -d "${SRC_ROOT}/expat/expat" ]]; then
      cmake_build_install expat "${SRC_ROOT}/expat/expat" "${cfg}" "${prefix}" \
        -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=ON
    elif [[ -d "${SRC_ROOT}/libexpat/expat" ]]; then
      cmake_build_install expat "${SRC_ROOT}/libexpat/expat" "${cfg}" "${prefix}" \
        -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=ON
    elif [[ -d "${SRC_ROOT}/expat" ]]; then
      cmake_build_install expat "${SRC_ROOT}/expat" "${cfg}" "${prefix}" \
        -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=ON
    elif [[ -d "${SRC_ROOT}/libexpat" ]]; then
      cmake_build_install expat "${SRC_ROOT}/libexpat" "${cfg}" "${prefix}" \
        -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_EXAMPLES=ON
    fi
  fi

  if [[ "${BUILD_OCIO}" == "ON" ]]; then
    cmake_build_install minizip-ng "${minizip_ng_src}" "${cfg}" "${prefix}" \
      -DMZ_COMPAT=OFF \
      -DMZ_BUILD_TESTS=OFF \
      -DMZ_FORCE_FETCH_LIBS=OFF \
      -DMZ_ZLIB=ON \
      -DMZ_BZIP2=OFF \
      -DMZ_LZMA=OFF \
      -DMZ_ZSTD=OFF \
      -DMZ_LIBCOMP=OFF \
      -DMZ_OPENSSL=OFF
  fi

  if [[ -d "${SRC_ROOT}/yaml-cpp" ]]; then
    cmake_build_install yaml-cpp "${SRC_ROOT}/yaml-cpp" "${cfg}" "${prefix}" \
      -DYAML_BUILD_SHARED_LIBS=OFF \
      -DYAML_CPP_INSTALL=ON
  fi

  if [[ -d "${SRC_ROOT}/pystring" ]]; then
    cmake_build_install pystring "${SRC_ROOT}/pystring" "${cfg}" "${prefix}"
  fi

  if [[ "${BUILD_OCIO}" == "ON" ]]; then
    cmake_build_install OpenColorIO "${ocio_src}" "${cfg}" "${prefix}" \
      -DOCIO_INSTALL_EXT_PACKAGES=NONE \
      -DOCIO_BUILD_APPS="${OCIO_BUILD_APPS}" \
      -DOCIO_BUILD_OPENFX=OFF \
      -DOCIO_BUILD_NUKE=OFF \
      -DOCIO_BUILD_TESTS=OFF \
      -DOCIO_BUILD_GPU_TESTS=OFF \
      -DOCIO_BUILD_PYTHON=OFF \
      -DOCIO_BUILD_JAVA=OFF \
      -DOCIO_BUILD_DOCS=OFF
  fi

  if [[ "${BUILD_GTEST}" == "ON" ]]; then
    cmake_build_install googletest "${gtest_src}" "${cfg}" "${prefix}" \
      -DINSTALL_GTEST=ON \
      -DBUILD_GMOCK=OFF \
      -Dgtest_build_tests=OFF \
      -Dgtest_build_samples=OFF
  fi

  # Note: this script builds the dependency prefix and optional libs (libjxl,
  # libuhdr, OpenColorIO). Build your app/repo separately, pointing CMake at
  # PREFIX_DEBUG/PREFIX_RELEASE.
}

mkdir -p "${BUILD_ROOT}" "${PREFIX_RELEASE}" "${PREFIX_DEBUG}"
setup_logging "${LOG_FILE}"

build_for_cfg Debug "${PREFIX_DEBUG}"
build_for_cfg Release "${PREFIX_RELEASE}"

echo
echo "Done."
echo "- Debug prefix:   ${PREFIX_DEBUG}"
echo "- Release prefix: ${PREFIX_RELEASE}"
echo "- Build root:     ${BUILD_ROOT}"
