from __future__ import annotations

STAMP_REVISION = "2"


def cmake_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    args = [f"-DBUILD_CODEC={builder._resolve_openjpeg_build_codec()}"]
    if builder.platform.os == "windows":
        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        lib_dir = (ctx.install_prefix / "lib").resolve()
        include_dir = (ctx.install_prefix / "include").resolve()

        zlib_release = lib_dir / "zlibstatic.lib"
        zlib_debug = lib_dir / f"zlibstatic{debug_postfix}.lib"
        zlib_lib = zlib_debug if ctx.build_type == "Debug" else zlib_release
        if zlib_lib.exists():
            args += [
                f"-DZLIB_LIBRARY={zlib_lib}",
                f"-DZLIB_INCLUDE_DIR={include_dir}",
                "-DZLIB_USE_STATIC_LIBS=ON",
            ]

        lcms_release = lib_dir / "lcms2_static.lib"
        lcms_debug = lib_dir / f"lcms2_static{debug_postfix}.lib"
        lcms_lib = lcms_debug if ctx.build_type == "Debug" else lcms_release
        if not lcms_lib.exists():
            candidates = sorted(lib_dir.glob("lcms2*.lib"))
            if candidates:
                lcms_lib = candidates[0]
        if lcms_lib.exists():
            args += [
                f"-DLCMS2_LIBRARY={lcms_lib}",
                f"-DLCMS2_INCLUDE_DIR={include_dir}",
            ]
    if builder.platform.os == "macos" and builder._resolve_openjpeg_build_codec() == "ON":
        args += [
            f"-DCMAKE_EXE_LINKER_FLAGS_INIT=-L{ctx.install_prefix / 'lib'}",
            f"-DCMAKE_SHARED_LINKER_FLAGS_INIT=-L{ctx.install_prefix / 'lib'}",
            f"-DCMAKE_MODULE_LINKER_FLAGS_INIT=-L{ctx.install_prefix / 'lib'}",
        ]
    return args


def patch_source(builder, src_dir) -> None:
    if builder.platform.os != "windows":
        return

    cmake_file = src_dir / "thirdparty" / "CMakeLists.txt"
    if not cmake_file.exists():
        return
    text = cmake_file.read_text(encoding="utf-8")
    original = "if(BUILD_STATIC_LIBS AND NOT BUILD_SHARED_LIBS)"
    patched = "if(BUILD_STATIC_LIBS AND NOT BUILD_SHARED_LIBS AND NOT WIN32)"
    if original in text:
        text = text.replace(original, patched)

    old_block = (
        "    # OPJ_WIN_ZLIB_TARGET_FALLBACK\n"
        "    if(WIN32 AND \"${ZLIB_LIBRARIES}\" STREQUAL \"ZLIB::ZLIB\" AND NOT TARGET ZLIB::ZLIB)\n"
        "      set(_opj_zlib_fallback \"\")\n"
        "      foreach(_opj_zlib_name zlibstaticd zlibstatic zlibd zlib)\n"
        "        find_library(_opj_zlib_candidate NAMES ${_opj_zlib_name})\n"
        "        if(_opj_zlib_candidate)\n"
        "          set(_opj_zlib_fallback \"${_opj_zlib_candidate}\")\n"
        "          break()\n"
        "        endif()\n"
        "      endforeach()\n"
        "      if(_opj_zlib_fallback)\n"
        "        set(Z_LIBNAME ${_opj_zlib_fallback} PARENT_SCOPE)\n"
        "      else()\n"
        "        set(Z_LIBNAME ${ZLIB_LIBRARIES} PARENT_SCOPE)\n"
        "      endif()\n"
        "    else()\n"
        "      set(Z_LIBNAME ${ZLIB_LIBRARIES} PARENT_SCOPE)\n"
        "    endif()"
    )
    if old_block in text:
        text = text.replace(old_block, "    set(Z_LIBNAME ${ZLIB_LIBRARIES} PARENT_SCOPE)")

    begin_marker = "    # OPJ_WIN_ZLIB_FIX_BEGIN"
    end_marker = "    # OPJ_WIN_ZLIB_FIX_END"
    replacement = (
        "    # OPJ_WIN_ZLIB_FIX_BEGIN\n"
        "    if(WIN32 AND \"${ZLIB_LIBRARIES}\" STREQUAL \"ZLIB::ZLIB\")\n"
        "      set(_opj_zlib_resolved \"\")\n"
        "      if(TARGET ZLIB::ZLIB)\n"
        "        get_target_property(_opj_zlib_debug ZLIB::ZLIB IMPORTED_LOCATION_DEBUG)\n"
        "        get_target_property(_opj_zlib_release ZLIB::ZLIB IMPORTED_LOCATION_RELEASE)\n"
        "        get_target_property(_opj_zlib_default ZLIB::ZLIB IMPORTED_LOCATION)\n"
        "        if(CMAKE_BUILD_TYPE STREQUAL \"Debug\" AND _opj_zlib_debug)\n"
        "          set(_opj_zlib_resolved \"${_opj_zlib_debug}\")\n"
        "        elseif(_opj_zlib_release)\n"
        "          set(_opj_zlib_resolved \"${_opj_zlib_release}\")\n"
        "        elseif(_opj_zlib_default)\n"
        "          set(_opj_zlib_resolved \"${_opj_zlib_default}\")\n"
        "        endif()\n"
        "      endif()\n"
        "      if(NOT _opj_zlib_resolved)\n"
        "        foreach(_opj_zlib_name zlibstaticd zlibstatic zlibd zlib)\n"
        "          find_library(_opj_zlib_candidate NAMES ${_opj_zlib_name})\n"
        "          if(_opj_zlib_candidate)\n"
        "            set(_opj_zlib_resolved \"${_opj_zlib_candidate}\")\n"
        "            break()\n"
        "          endif()\n"
        "        endforeach()\n"
        "      endif()\n"
        "      if(_opj_zlib_resolved)\n"
        "        set(Z_LIBNAME \"${_opj_zlib_resolved}\" PARENT_SCOPE)\n"
        "      else()\n"
        "        set(Z_LIBNAME ${ZLIB_LIBRARIES} PARENT_SCOPE)\n"
        "      endif()\n"
        "    else()\n"
        "      set(Z_LIBNAME ${ZLIB_LIBRARIES} PARENT_SCOPE)\n"
        "    endif()\n"
        "    # OPJ_WIN_ZLIB_FIX_END"
    )

    if begin_marker in text and end_marker in text:
        start = text.index(begin_marker)
        end = text.index(end_marker, start) + len(end_marker)
        text = text[:start] + replacement + text[end:]
    else:
        needle = "    set(Z_LIBNAME ${ZLIB_LIBRARIES} PARENT_SCOPE)"
        if needle in text:
            text = text.replace(needle, replacement, 1)
    cmake_file.write_text(text, encoding="utf-8")

    jp2_cmake = src_dir / "src" / "bin" / "jp2" / "CMakeLists.txt"
    if not jp2_cmake.exists():
        return
    jp2_text = jp2_cmake.read_text(encoding="utf-8")
    jp2_begin = "# OPJ_WIN_TIFF_SCOPE_FIX_BEGIN"
    jp2_end = "# OPJ_WIN_TIFF_SCOPE_FIX_END"
    jp2_fix = (
        "# OPJ_WIN_TIFF_SCOPE_FIX_BEGIN\n"
        "if(WIN32 AND OPJ_HAVE_LIBTIFF)\n"
        "  if(NOT TARGET TIFF::tiff)\n"
        "    find_package(TIFF QUIET)\n"
        "  endif()\n"
        "endif()\n"
        "# OPJ_WIN_TIFF_SCOPE_FIX_END"
    )
    insert_after = "if(OPJ_HAVE_LIBPNG)\n\tlist(APPEND common_SRCS convertpng.c)\nendif()"
    if jp2_begin in jp2_text and jp2_end in jp2_text:
        start = jp2_text.index(jp2_begin)
        end = jp2_text.index(jp2_end, start) + len(jp2_end)
        jp2_text = jp2_text[:start] + jp2_fix + jp2_text[end:]
    elif insert_after in jp2_text:
        jp2_text = jp2_text.replace(insert_after, f"{insert_after}\n\n{jp2_fix}", 1)
    jp2_cmake.write_text(jp2_text, encoding="utf-8")
