from __future__ import annotations

from pathlib import Path

from .policy import ocio_enabled


STAMP_REVISION = "2"

_PYTHON_SUFFIX_FIX_BEGIN = "# OIIO_BUILDER_PYTHON_SUFFIX_FIX_BEGIN"
_PYTHON_SUFFIX_FIX_END = "# OIIO_BUILDER_PYTHON_SUFFIX_FIX_END"


def enabled(builder, _repo) -> bool:
    return ocio_enabled(builder)


def cmake_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    builder._ensure_ppmd_package(ctx.install_prefix, ctx.build_type)
    ocio_build_python = "ON"
    if builder.platform.os == "windows":
        wrappers_enabled, reason = builder._windows_python_wrappers_enabled()
        ocio_build_python = "ON" if wrappers_enabled else "OFF"
        if ocio_build_python == "OFF" and not builder._ocio_python_note_printed:
            if reason == "forced-off":
                print("[note] OpenColorIO: OCIO_BUILD_PYTHON=OFF (windows.python_wrappers=off)", flush=True)
            else:
                print(
                    "[note] OpenColorIO: OCIO_BUILD_PYTHON=OFF (windows.python_wrappers=auto with static CRT). "
                    "Set windows.python_wrappers=on (or windows.msvc_runtime=dynamic) to enable wrappers.",
                    flush=True,
                )
            builder._ocio_python_note_printed = True
    args = [
        "-DOCIO_INSTALL_EXT_PACKAGES=NONE",
        f"-DOCIO_BUILD_APPS={cfg.ocio_build_apps}",
        "-DOCIO_BUILD_OPENFX=OFF",
        "-DOCIO_BUILD_NUKE=OFF",
        "-DOCIO_BUILD_TESTS=OFF",
        "-DOCIO_BUILD_GPU_TESTS=OFF",
        f"-DOCIO_BUILD_PYTHON={ocio_build_python}",
        "-DOCIO_BUILD_JAVA=OFF",
        "-DOCIO_BUILD_DOCS=OFF",
    ]
    if builder.platform.os == "windows":
        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        pystring_include_dir = ctx.install_prefix / "include" / "pystring"
        release_pystring_lib = ctx.install_prefix / "lib" / "pystring.lib"
        debug_pystring_lib = ctx.install_prefix / "lib" / f"pystring{debug_postfix}.lib"
        pystring_lib = debug_pystring_lib if ctx.build_type == "Debug" else release_pystring_lib
        if not pystring_lib.exists():
            candidates = sorted((ctx.install_prefix / "lib").glob("pystring*.lib"))
            if candidates:
                pystring_lib = candidates[0]
        args += [
            f"-Dpystring_ROOT={ctx.install_prefix}",
            f"-Dpystring_INCLUDE_DIR={pystring_include_dir}",
            f"-Dpystring_LIBRARY={pystring_lib}",
        ]

        minizip_include_dir = ctx.install_prefix / "include" / "minizip-ng"
        minizip_cmake_dir = ctx.install_prefix / "lib" / "cmake" / "minizip-ng"
        release_minizip_lib = ctx.install_prefix / "lib" / "minizip-ng.lib"
        debug_minizip_lib = ctx.install_prefix / "lib" / f"minizip-ng{debug_postfix}.lib"
        minizip_lib = debug_minizip_lib if ctx.build_type == "Debug" else release_minizip_lib
        if not minizip_lib.exists():
            candidates = sorted((ctx.install_prefix / "lib").glob("minizip-ng*.lib"))
            if candidates:
                minizip_lib = candidates[0]
        args += [
            f"-Dminizip-ng_ROOT={ctx.install_prefix}",
            f"-Dminizip-ng_DIR={minizip_cmake_dir}",
            f"-Dminizip-ng_INCLUDE_DIR={minizip_include_dir}",
            f"-Dminizip-ng_LIBRARY={minizip_lib}",
        ]
    return args


def pre_build(builder, _repo, ctx, _env) -> None:
    builder._ensure_pystring_package(ctx.install_prefix, ctx.build_type)


def post_install(builder, install_prefix: Path, build_type: str) -> None:
    if builder.platform.os != "windows":
        return

    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
    package_dir = install_prefix / "lib" / "site-packages" / "PyOpenColorIO"
    if build_type == "Debug":
        legacy_module = package_dir / f"PyOpenColorIO{debug_postfix}.pyd"
        abi_modules = list(package_dir.glob(f"PyOpenColorIO_{debug_postfix}.*.pyd"))
    else:
        legacy_module = package_dir / "PyOpenColorIO.pyd"
        abi_modules = list(package_dir.glob("PyOpenColorIO.*.pyd"))

    if abi_modules and legacy_module.is_file():
        legacy_module.unlink()


def patch_source(builder, src_dir: Path) -> None:
    # clang-cl defines _MSC_VER but doesn't provide MSVC's SVML intrinsic
    # `_mm_pow_ps()` (used by OCIO for a precise SIMD pow()).
    # Gate this path to MSVC-only by excluding clang.
    cpu_file = src_dir / "src" / "OpenColorIO" / "ops" / "fixedfunction" / "FixedFunctionOpCPU.cpp"
    if cpu_file.exists():
        original = cpu_file.read_text(encoding="utf-8", errors="replace")
        text = original

        needle = "#if (_MSC_VER >= 1920) && (OCIO_USE_AVX)"
        replacement = "#if (_MSC_VER >= 1920) && !defined(__clang__) && (OCIO_USE_AVX)"
        text = text.replace(needle, replacement)

        if text != original:
            cpu_file.write_text(text, encoding="utf-8")

    if builder.platform.os != "windows":
        return

    python_cmake = src_dir / "src" / "bindings" / "python" / "CMakeLists.txt"
    if not python_cmake.exists():
        return

    original = python_cmake.read_text(encoding="utf-8", errors="replace")
    if _PYTHON_SUFFIX_FIX_BEGIN in original:
        return

    windows_suffix_block = """\
if(WIN32)
\t# Windows uses .pyd extension for python modules
\tset_target_properties(PyOpenColorIO PROPERTIES
\t\tSUFFIX ".pyd"
\t)
endif()
"""
    if windows_suffix_block not in original:
        raise RuntimeError(f"Could not locate PyOpenColorIO Windows suffix block in: {python_cmake}")

    suffix_fix = f"""\
{windows_suffix_block}
{_PYTHON_SUFFIX_FIX_BEGIN}
if(WIN32)
\texecute_process(
\t\tCOMMAND "${{Python_EXECUTABLE}}" -c
\t\t\t"import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX') or '.pyd')"
\t\tRESULT_VARIABLE _pyocio_extension_suffix_result
\t\tOUTPUT_VARIABLE _pyocio_extension_suffix
\t\tOUTPUT_STRIP_TRAILING_WHITESPACE
\t\tERROR_QUIET
\t)
\tif(NOT _pyocio_extension_suffix_result EQUAL 0 OR NOT _pyocio_extension_suffix)
\t\tset(_pyocio_extension_suffix ".pyd")
\tendif()
\tset_target_properties(PyOpenColorIO PROPERTIES
\t\tSUFFIX "${{_pyocio_extension_suffix}}"
\t\tDEBUG_POSTFIX ""
\t)
endif()
{_PYTHON_SUFFIX_FIX_END}
"""
    python_cmake.write_text(original.replace(windows_suffix_block, suffix_fix, 1), encoding="utf-8")
