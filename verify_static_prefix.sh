#!/usr/bin/env bash
set -eEuo pipefail

prefixes=()
if [[ $# -gt 0 ]]; then
  prefixes=("$@")
else
  prefixes=(/Users/s02299/MOSd /Users/s02299/MOS)
fi

CC_BIN="${CC_BIN:-clang-20}"
CXX_BIN="${CXX_BIN:-clang++-20}"
JOBS="${JOBS:-$(nproc)}"

if [[ -t 1 ]]; then
  _YELLOW="$(printf '\033[33m')"
  _RESET="$(printf '\033[0m')"
else
  _YELLOW=""
  _RESET=""
fi

banner() {
  local msg="$1"
  printf '%s==========================================%s\n' "${_YELLOW}" "${_RESET}"
  printf '%s%s%s\n' "${_YELLOW}" "${msg}" "${_RESET}"
  printf '%s==========================================%s\n' "${_YELLOW}" "${_RESET}"
}

die() {
  echo "ERROR: $*" >&2
  exit 2
}

print_cmd() {
  local out=""
  local arg
  for arg in "$@"; do
    out+=$(printf '%q ' "${arg}")
  done
  printf '%s' "${out}"
}

run() {
  printf '+ %s\n' "$(print_cmd "$@")"
  "$@"
}

require_file() {
  local f="$1"
  [[ -f "${f}" ]] || die "missing file: ${f}"
}

require_dir() {
  local d="$1"
  [[ -d "${d}" ]] || die "missing dir: ${d}"
}

check_no_shared_objects() {
  local prefix="$1"
  local found
  found="$(find "${prefix}/lib" -maxdepth 1 -type f -name '*.so*' -print | head -n 20 || true)"
  if [[ -n "${found}" ]]; then
    echo "${found}" >&2
    die "${prefix}: found shared objects under lib/ (expected static-only prefix)"
  fi
}

check_archives_are_ar() {
  local prefix="$1"
  local a
  while IFS= read -r a; do
    # "current ar archive" is typical GNU binutils output.
    # llvm-ar archives are still recognized as "ar archive".
    if ! file -b "${a}" | rg -q "ar archive"; then
      die "${prefix}: not an ar archive: ${a}"
    fi
  done < <(find "${prefix}/lib" -maxdepth 1 -type f -name '*.a' -print | sort)
}

check_no_libcxx_markers() {
  local prefix="$1"
  # Best-effort: scan text-ish config files for libc++ usage hints.
  local hits=""
  if command -v rg >/dev/null 2>&1; then
    hits="$(rg -n "(-stdlib=libc\\+\\+|\\blibc\\+\\+\\b|std::__1\\b)" \
      "${prefix}/lib/pkgconfig" "${prefix}/lib/cmake" "${prefix}/share" 2>/dev/null | head -n 20 || true)"
  fi
  if [[ -n "${hits}" ]]; then
    echo "${hits}" >&2
    die "${prefix}: found libc++ markers in installed metadata (expected libstdc++)"
  fi
}

check_bin_linkers() {
  local prefix="$1"
  local any=0
  if [[ ! -d "${prefix}/bin" ]]; then
    return 0
  fi
  while IFS= read -r exe; do
    any=1
    if command -v ldd >/dev/null 2>&1; then
      if ldd "${exe}" 2>/dev/null | rg -q "libc\\+\\+"; then
        die "${prefix}: ${exe} links against libc++ (expected libstdc++)"
      fi
    fi
  done < <(find "${prefix}/bin" -maxdepth 1 -type f -executable -print | sort)
  if [[ "${any}" -eq 0 ]]; then
    echo "(no executables under ${prefix}/bin to inspect)"
  fi
}

pic_link_test_c() {
  local name="$1"; shift
  local prefix="$1"; shift
  local code="$1"; shift
  shift || true
  local -a libs=("$@")

  local tmp="${_TMPDIR}/${name}"
  mkdir -p "${tmp}"
  printf '%s\n' "${code}" > "${tmp}/t.c"

  run "${CC_BIN}" -fPIC -I"${prefix}/include" -c "${tmp}/t.c" -o "${tmp}/t.o"
  # Linking as a shared library will fail if the archive contains non-PIC code
  # that must be pulled in to satisfy t.o (typical error: "recompile with -fPIC").
  run "${CC_BIN}" -shared -o "${tmp}/libcheck_${name}.so" "${tmp}/t.o" "${libs[@]}"
}

verify_prefix() {
  local prefix="$1"
  banner "Verify ${prefix}"
  require_dir "${prefix}"
  require_dir "${prefix}/lib"

  # Presence checks for the core stack used by JXLGPU/libjxl + OIIO-style builds.
  require_file "${prefix}/include/zlib.h"
  require_file "${prefix}/lib/libz.a"
  require_file "${prefix}/lib/liblzma.a"
  require_file "${prefix}/lib/libdeflate.a"
  require_file "${prefix}/lib/libzstd.a"
  require_file "${prefix}/lib/libjpeg.a"
  require_file "${prefix}/lib/libpng.a"
  require_file "${prefix}/lib/libbrotlicommon.a"
  require_file "${prefix}/lib/libbrotlidec.a"
  require_file "${prefix}/lib/libbrotlienc.a"
  require_file "${prefix}/lib/libhwy.a"
  require_file "${prefix}/lib/liblcms2.a"

  check_no_shared_objects "${prefix}"
  check_archives_are_ar "${prefix}"
  check_no_libcxx_markers "${prefix}"
  check_bin_linkers "${prefix}"

  banner "PIC Link Checks ${prefix}"
  pic_link_test_c zlib "${prefix}" \
    '#include <zlib.h>\nconst char* v(){return zlibVersion();}\n' \
    "${prefix}/lib/libz.a"
  pic_link_test_c lzma "${prefix}" \
    '#include <lzma.h>\nunsigned v(){return lzma_version_number();}\n' \
    "${prefix}/lib/liblzma.a"
  pic_link_test_c zstd "${prefix}" \
    '#include <zstd.h>\nunsigned v(){return (unsigned)ZSTD_versionNumber();}\n' \
    "${prefix}/lib/libzstd.a"
  pic_link_test_c libdeflate "${prefix}" \
    '#include <libdeflate.h>\nconst char* v(){return libdeflate_version_string();}\n' \
    "${prefix}/lib/libdeflate.a"
  pic_link_test_c libjpeg "${prefix}" \
    '#include <jpeglib.h>\nint v(){struct jpeg_error_mgr e; (void)jpeg_std_error(&e); return 0;}\n' \
    "${prefix}/lib/libjpeg.a" -lm
  pic_link_test_c libpng "${prefix}" \
    '#include <png.h>\nint v(){png_structp p=png_create_read_struct(PNG_LIBPNG_VER_STRING,0,0,0); if(p) png_destroy_read_struct(&p,0,0); return 0;}\n' \
    "${prefix}/lib/libpng.a" "${prefix}/lib/libz.a" -lm

  banner "OK ${prefix}"
}

_TMPDIR="$(mktemp -d)"
trap 'rm -rf "${_TMPDIR}"' EXIT

for p in "${prefixes[@]}"; do
  verify_prefix "${p}"
done

echo "Done."
