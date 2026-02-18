from __future__ import annotations

from pathlib import Path

STAMP_REVISION = "4"


def cmake_args(builder, ctx) -> list[str]:
    args = [
        "-Dtiff-tests=OFF",
        "-Dtiff-tools=ON",
        "-Dtiff-docs=OFF",
        "-Dtiff-contrib=OFF",
        "-Dlerc=OFF",
        "-Dwebp=OFF",
        "-DJPEG_SUPPORT=ON",
        "-DJPEG_DUAL_MODE_8_12=ON",
    ]
    if builder.platform.os == "windows":
        args.append("-Dtiff-opengl=ON")
        # Keep ASAN/Debug flags intact; inject required static-link defines via a
        # top-level include instead of overwriting CMAKE_*_FLAGS.
        include_path = Path(ctx.build_dir) / "oiio_builder_libtiff_defines.cmake"
        try:
            include_path.write_text(
                "\n".join(
                    [
                        "if(WIN32)",
                        "  if(NOT BUILD_SHARED_LIBS)",
                        "    add_compile_definitions(LZMA_API_STATIC FREEGLUT_STATIC)",
                        "  endif()",
                        "endif()",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        args.append(f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={include_path.as_posix()}")
    else:
        args.append("-Dtiff-opengl=OFF")
    return args


def patch_source(_builder, src_dir) -> None:
    cmake_config_in = src_dir / "cmake" / "tiff-config.cmake.in"
    libtiff_cmake_lists = src_dir / "libtiff" / "CMakeLists.txt"

    if cmake_config_in.exists():
        original_text = cmake_config_in.read_text(encoding="utf-8")
        text = original_text
        marker_begin = "# OIIO_BUILDER_TIFF_STATIC_CONFIG_BEGIN"
        marker_end = "# OIIO_BUILDER_TIFF_STATIC_CONFIG_END"
        replacement = (
            "# OIIO_BUILDER_TIFF_STATIC_CONFIG_BEGIN\n"
            "if(\"@TIFF_BUILD_LIB_VALUE@\" STREQUAL \"STATIC\")\n"
            "    include(CMakeFindDependencyMacro)\n"
            "\n"
            "    # For static builds, consumers must also link our codec dependencies.\n"
            "    #\n"
            "    # Libtiff ships custom Find-modules for some dependencies and also uses\n"
            "    # non-standard imported target names (e.g. liblzma::liblzma), so make sure\n"
            "    # our modules are discoverable when find_dependency() runs.\n"
            "    list(PREPEND CMAKE_MODULE_PATH \"${CMAKE_CURRENT_LIST_DIR}/modules\")\n"
            "\n"
            "    set(_tiff_static_deps \"@TIFF_STATIC_PACKAGE_DEPENDENCIES@\")\n"
            "    foreach(_dep IN LISTS _tiff_static_deps)\n"
            "        find_dependency(${_dep})\n"
            "    endforeach()\n"
            "    if(NOT TARGET Deflate::Deflate)\n"
            "        find_package(libdeflate QUIET CONFIG)\n"
            "        if(TARGET libdeflate::libdeflate_static)\n"
            "            add_library(Deflate::Deflate INTERFACE IMPORTED)\n"
            "            target_link_libraries(Deflate::Deflate INTERFACE libdeflate::libdeflate_static)\n"
            "        elseif(TARGET libdeflate::libdeflate_shared)\n"
            "            add_library(Deflate::Deflate INTERFACE IMPORTED)\n"
            "            target_link_libraries(Deflate::Deflate INTERFACE libdeflate::libdeflate_shared)\n"
            "        endif()\n"
            "    endif()\n"
            "    unset(_dep)\n"
            "    unset(_tiff_static_deps)\n"
            "endif()\n"
            "# OIIO_BUILDER_TIFF_STATIC_CONFIG_END"
        )
        if marker_begin in text and marker_end in text:
            start = text.index(marker_begin)
            stop = text.index(marker_end, start) + len(marker_end)
            text = text[:start] + replacement + text[stop:]
        else:
            original = "if(NOT \"@BUILD_SHARED_LIBS@\")\n    # TODO: import dependencies\nendif()"
            if original in text:
                text = text.replace(original, replacement, 1)
        if text != original_text:
            cmake_config_in.write_text(text, encoding="utf-8")

    if libtiff_cmake_lists.exists():
        original_text = libtiff_cmake_lists.read_text(encoding="utf-8")
        text = original_text
        marker_begin = "# OIIO_BUILDER_TIFF_CMAKELISTS_STATIC_DEPS_BEGIN"
        marker_end = "# OIIO_BUILDER_TIFF_CMAKELISTS_STATIC_DEPS_END"
        block = (
            "  # OIIO_BUILDER_TIFF_CMAKELISTS_STATIC_DEPS_BEGIN\n"
            "  # For static builds, consumers must also link our codec dependencies.\n"
            "  # Teach the installed tiff-config.cmake which packages to find, and install\n"
            "  # the corresponding Find-modules we ship (when present).\n"
            "  set(TIFF_STATIC_PACKAGE_DEPENDENCIES \"\")\n"
            "  set(_tiff_find_modules \"\")\n"
            "  if(TIFF_BUILD_LIB_VALUE STREQUAL \"STATIC\")\n"
            "    # Derive package dependencies from the exported target interface. This keeps\n"
            "    # tiff-config.cmake and the exported link interface consistent.\n"
            "    get_target_property(_tiff_iface_libs tiff INTERFACE_LINK_LIBRARIES)\n"
            "    if(NOT _tiff_iface_libs OR _tiff_iface_libs STREQUAL \"_tiff_iface_libs-NOTFOUND\")\n"
            "      # Fallback: be explicit (should be rare; depends on CMake behavior).\n"
            "      if(ZIP_SUPPORT)\n"
            "        list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES ZLIB)\n"
            "      endif()\n"
            "      if(LIBDEFLATE_SUPPORT)\n"
            "        list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES Deflate)\n"
            "      endif()\n"
            "      if(JPEG_SUPPORT)\n"
            "        list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES JPEG)\n"
            "      endif()\n"
            "      if(JBIG_SUPPORT)\n"
            "        list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES JBIG)\n"
            "      endif()\n"
            "      if(LERC_SUPPORT)\n"
            "        list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES LERC)\n"
            "      endif()\n"
            "      if(LZMA_SUPPORT)\n"
            "        list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES liblzma)\n"
            "      endif()\n"
            "      if(ZSTD_SUPPORT)\n"
            "        list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES ZSTD)\n"
            "      endif()\n"
            "      if(WEBP_SUPPORT)\n"
            "        list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES WebP)\n"
            "      endif()\n"
            "\n"
            "      foreach(_pkg IN LISTS TIFF_STATIC_PACKAGE_DEPENDENCIES)\n"
            "        if(EXISTS \"${PROJECT_SOURCE_DIR}/cmake/Find${_pkg}.cmake\")\n"
            "          list(APPEND _tiff_find_modules \"${PROJECT_SOURCE_DIR}/cmake/Find${_pkg}.cmake\")\n"
            "        endif()\n"
            "      endforeach()\n"
            "      unset(_pkg)\n"
            "    else()\n"
            "      foreach(_item IN LISTS _tiff_iface_libs)\n"
            "        # Exported static link interfaces may wrap dependencies with LINK_ONLY.\n"
            "        string(REGEX REPLACE \"^\\\\$<LINK_ONLY:(.*)>$\" \"\\\\1\" _item \"${_item}\")\n"
            "        if(_item MATCHES \"^([^:]+)::\")\n"
            "          set(_pkg \"${CMAKE_MATCH_1}\")\n"
            "          list(APPEND TIFF_STATIC_PACKAGE_DEPENDENCIES \"${_pkg}\")\n"
            "          if(EXISTS \"${PROJECT_SOURCE_DIR}/cmake/Find${_pkg}.cmake\")\n"
            "            list(APPEND _tiff_find_modules \"${PROJECT_SOURCE_DIR}/cmake/Find${_pkg}.cmake\")\n"
            "          endif()\n"
            "        endif()\n"
            "      endforeach()\n"
            "      unset(_item)\n"
            "      unset(_pkg)\n"
            "    endif()\n"
            "    unset(_tiff_iface_libs)\n"
            "\n"
            "    list(REMOVE_DUPLICATES TIFF_STATIC_PACKAGE_DEPENDENCIES)\n"
            "    list(REMOVE_DUPLICATES _tiff_find_modules)\n"
            "  endif()\n"
            "\n"
            "  if(_tiff_find_modules)\n"
            "    install(FILES ${_tiff_find_modules} DESTINATION ${TIFF_CONFIGDIR}/modules)\n"
            "  endif()\n"
            "  unset(_tiff_find_modules)\n"
            "  # OIIO_BUILDER_TIFF_CMAKELISTS_STATIC_DEPS_END\n"
        )
        if marker_begin in text and marker_end in text:
            start = text.index(marker_begin)
            stop = text.index(marker_end, start) + len(marker_end)
            text = text[:start] + block + text[stop:]
        else:
            anchor = "  include(CMakePackageConfigHelpers)"
            if anchor in text:
                text = text.replace(anchor, f"{block}\n{anchor}", 1)
        if text != original_text:
            libtiff_cmake_lists.write_text(text, encoding="utf-8")
