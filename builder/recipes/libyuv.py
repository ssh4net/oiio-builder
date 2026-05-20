from __future__ import annotations

from pathlib import Path

STAMP_REVISION = "2"


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_libyuv)


def cmake_args(builder, _ctx) -> list[str]:
    return [f"-DLIBYUV_BUILD_SHARED={'OFF' if builder.config.global_cfg.static_default else 'ON'}"]


def patch_source(_builder, src_dir: Path) -> None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if cmake_lists.exists():
        _patch_cmake_lists(cmake_lists)

    row_win = src_dir / "source" / "row_win.cc"
    if not row_win.exists():
        return

    original_text = row_win.read_text(encoding="utf-8", errors="replace")
    text = original_text

    marker = "#define LIBYUV_NO_SANITIZE_CFI_ICALL"
    if marker not in text:
        needle = (
            "#if defined(__clang__) || defined(__GNUC__)\n"
            "#define LIBYUV_TARGET_AVX2 __attribute__((target(\"avx2\")))\n"
            "#define LIBYUV_TARGET_AVX512BW \\\n"
            "  __attribute__((target(\"avx512bw,avx512vl,avx512f\")))\n"
            "#else\n"
            "#define LIBYUV_TARGET_AVX2\n"
            "#define LIBYUV_TARGET_AVX512BW\n"
            "#endif"
        )
        replacement = (
            "#if defined(__clang__) || defined(__GNUC__)\n"
            "#define LIBYUV_TARGET_AVX2 __attribute__((target(\"avx2\")))\n"
            "#define LIBYUV_TARGET_AVX512BW \\\n"
            "  __attribute__((target(\"avx512bw,avx512vl,avx512f\")))\n"
            "#define LIBYUV_NO_SANITIZE_CFI_ICALL __attribute__((no_sanitize(\"cfi-icall\")))\n"
            "#else\n"
            "#define LIBYUV_TARGET_AVX2\n"
            "#define LIBYUV_TARGET_AVX512BW\n"
            "#define LIBYUV_NO_SANITIZE_CFI_ICALL\n"
            "#endif"
        )
        if needle in text:
            text = text.replace(needle, replacement, 1)

    text = text.replace(
        'LIBYUV_TARGET_AVX2 __attribute__((no_sanitize("cfi-icall")))',
        "LIBYUV_TARGET_AVX2 LIBYUV_NO_SANITIZE_CFI_ICALL",
    )

    if text != original_text:
        row_win.write_text(text, encoding="utf-8")


def _patch_cmake_lists(cmake_lists: Path) -> None:
    original_text = cmake_lists.read_text(encoding="utf-8", errors="replace")
    text = original_text

    marker = "option(LIBYUV_BUILD_SHARED"
    if marker not in text:
        needle = "set ( ly_lib_shared\t${ly_lib_name}_shared )"
        replacement = (
            "set ( ly_lib_shared\t${ly_lib_name}_shared )\n"
            "if(DEFINED BUILD_SHARED_LIBS)\n"
            "  set(LIBYUV_BUILD_SHARED_DEFAULT ${BUILD_SHARED_LIBS})\n"
            "else()\n"
            "  set(LIBYUV_BUILD_SHARED_DEFAULT ON)\n"
            "endif()\n"
            "option(LIBYUV_BUILD_SHARED \"Build shared libyuv library\" ${LIBYUV_BUILD_SHARED_DEFAULT})"
        )
        if needle in text:
            text = text.replace(needle, replacement, 1)

    shared_block = (
        "# this creates the shared library (.so)\n"
        "add_library( ${ly_lib_shared} SHARED ${ly_lib_parts})\n"
        "set_target_properties( ${ly_lib_shared} PROPERTIES OUTPUT_NAME \"${ly_lib_name}\" )\n"
        "set_target_properties( ${ly_lib_shared} PROPERTIES PREFIX \"lib\" )\n"
        "if(WIN32)\n"
        "  set_target_properties( ${ly_lib_shared} PROPERTIES IMPORT_PREFIX \"lib\" )\n"
        "endif()"
    )
    wrapped_shared_block = (
        "# this creates the shared library (.so)\n"
        "if(LIBYUV_BUILD_SHARED)\n"
        "  add_library( ${ly_lib_shared} SHARED ${ly_lib_parts})\n"
        "  set_target_properties( ${ly_lib_shared} PROPERTIES OUTPUT_NAME \"${ly_lib_name}\" )\n"
        "  set_target_properties( ${ly_lib_shared} PROPERTIES PREFIX \"lib\" )\n"
        "  if(WIN32)\n"
        "    set_target_properties( ${ly_lib_shared} PROPERTIES IMPORT_PREFIX \"lib\" )\n"
        "  endif()\n"
        "endif()"
    )
    if shared_block in text and wrapped_shared_block not in text:
        text = text.replace(shared_block, wrapped_shared_block, 1)

    shared_jpeg = "  target_link_libraries( ${ly_lib_shared} ${JPEG_LIBRARY} )"
    wrapped_shared_jpeg = (
        "  if(LIBYUV_BUILD_SHARED)\n"
        "    target_link_libraries( ${ly_lib_shared} ${JPEG_LIBRARY} )\n"
        "  endif()"
    )
    if shared_jpeg in text and wrapped_shared_jpeg not in text:
        text = text.replace(shared_jpeg, wrapped_shared_jpeg, 1)

    shared_install = "install ( TARGETS ${ly_lib_shared} LIBRARY DESTINATION lib RUNTIME DESTINATION bin ARCHIVE DESTINATION lib )"
    wrapped_shared_install = (
        "if(LIBYUV_BUILD_SHARED)\n"
        "  install ( TARGETS ${ly_lib_shared} LIBRARY DESTINATION lib RUNTIME DESTINATION bin ARCHIVE DESTINATION lib )\n"
        "endif()"
    )
    if shared_install in text and wrapped_shared_install not in text:
        text = text.replace(shared_install, wrapped_shared_install, 1)

    if text != original_text:
        cmake_lists.write_text(text, encoding="utf-8")
