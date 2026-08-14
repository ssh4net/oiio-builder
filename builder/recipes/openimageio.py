from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .policy import ffmpeg_enabled, imageio_enabled, windows_use_ffmpeg_from_prefix
from ..license_policy import LGPL_DYNAMIC
from ..tooling import resolve_openmp_root


STAMP_REVISION = "15"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_oiio)


def _patch_compiled_fmt_option(src_dir: Path) -> None:
    def replace_once(path: Path, old: str, new: str, description: str) -> None:
        if not path.exists():
            raise RuntimeError(f"OpenImageIO {description} patch target is missing: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if new in text:
            return
        if old not in text:
            raise RuntimeError(f"OpenImageIO {description} patch no longer matches upstream source: {path}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    externalpackages = src_dir / "src" / "cmake" / "externalpackages.cmake"
    text = externalpackages.read_text(encoding="utf-8", errors="replace")
    if "OIIO_USE_COMPILED_FMT" not in text:
        fmt_block = re.compile(
            r"(?P<header># fmtlib\r?\n)"
            r"(?P<option>(?P<option_cmd>set_option|option) "
            r"\(OIIO_INTERNALIZE_FMT [^\r\n]*\r?\n)"
            r"(?P<find>checked_find_package \(fmt REQUIRED\r?\n"
            r".*?\r?\n"
            r"[ \t]*\)\r?\n)"
            r"get_target_property\(FMT_INCLUDE_DIR fmt::fmt-header-only INTERFACE_INCLUDE_DIRECTORIES\)\r?\n",
            re.DOTALL,
        )
        match = fmt_block.search(text)
        if match is not None:
            option_cmd = match.group("option_cmd")
            new = (
                f"{match.group('header')}"
                f"{match.group('option')}"
                f'{option_cmd} (OIIO_USE_COMPILED_FMT "Link against compiled fmt::fmt instead of header-only fmt" OFF)\n'
                "if (OIIO_USE_COMPILED_FMT)\n"
                "    set (OIIO_USE_COMPILED_FMT_VALUE 1)\n"
                "else ()\n"
                "    set (OIIO_USE_COMPILED_FMT_VALUE 0)\n"
                "endif ()\n"
                f"{match.group('find')}"
                "if (OIIO_USE_COMPILED_FMT)\n"
                "    get_target_property(FMT_INCLUDE_DIR fmt::fmt INTERFACE_INCLUDE_DIRECTORIES)\n"
                "else ()\n"
                "    get_target_property(FMT_INCLUDE_DIR fmt::fmt-header-only INTERFACE_INCLUDE_DIRECTORIES)\n"
                "endif ()\n"
            )
            externalpackages.write_text(text[: match.start()] + new + text[match.end() :], encoding="utf-8")
        else:
            raise RuntimeError(f"OpenImageIO compiled fmt CMake option patch no longer matches upstream source: {externalpackages}")

    oiioversion = src_dir / "src" / "include" / "OpenImageIO" / "oiioversion.h.in"
    if not oiioversion.exists():
        raise RuntimeError(f"OpenImageIO version header patch target is missing: {oiioversion}")
    text = oiioversion.read_text(encoding="utf-8", errors="replace")
    marker = "#define OIIO_USE_COMPILED_FMT @OIIO_USE_COMPILED_FMT_VALUE@"
    if marker not in text:
        anchor = "#define OIIO_VERSION_RELEASE_TYPE @PROJECT_VERSION_RELEASE_TYPE@\n"
        replacement = anchor + marker + "\n"
        if anchor not in text:
            raise RuntimeError(f"OpenImageIO version header patch no longer matches upstream source: {oiioversion}")
        text = text.replace(anchor, replacement, 1)
        oiioversion.write_text(text, encoding="utf-8")

    libutil = src_dir / "src" / "libutil" / "CMakeLists.txt"
    old = """\
    if (OIIO_INTERNALIZE_FMT OR fmt_LOCAL_BUILD)
        add_dependencies(${targetname} fmt_internal_target)
    else ()
        target_link_libraries (${targetname}
                               PUBLIC fmt::fmt-header-only)
    endif ()
"""
    new = """\
    if (OIIO_USE_COMPILED_FMT)
        target_link_libraries (${targetname}
                               PUBLIC fmt::fmt)
    elseif (OIIO_INTERNALIZE_FMT OR fmt_LOCAL_BUILD)
        add_dependencies(${targetname} fmt_internal_target)
    else ()
        target_link_libraries (${targetname}
                               PUBLIC fmt::fmt-header-only)
    endif ()
"""
    libutil_text = libutil.read_text(encoding="utf-8", errors="replace")
    if new in libutil_text:
        pass
    elif old in libutil_text:
        libutil.write_text(libutil_text.replace(old, new, 1), encoding="utf-8")
    elif (
        "if (OIIO_USE_COMPILED_FMT)" in libutil_text
        and "PUBLIC fmt::fmt)" in libutil_text
        and "PUBLIC fmt::fmt-header-only)" in libutil_text
    ):
        pass
    else:
        raise RuntimeError(f"OpenImageIO libutil fmt linkage patch no longer matches upstream source: {libutil}")

    _patch_ustring_fmt_runtime(src_dir)


def _patch_ustring_fmt_runtime(src_dir: Path) -> None:
    ustring = src_dir / "src" / "libutil" / "ustring.cpp"
    if not ustring.exists():
        raise RuntimeError(f"OpenImageIO fmt runtime format compatibility patch target is missing: {ustring}")

    text = ustring.read_text(encoding="utf-8", errors="replace")
    old = """\
            for (auto c : s)
                print(stderr, c > 0 ? "{:c}" : "\\\\{:03o}",
                      static_cast<unsigned char>(c));
"""
    new = """\
            for (auto c : s)
                print(stderr, fmt::runtime(c > 0 ? "{:c}" : "\\\\{:03o}"),
                      static_cast<unsigned char>(c));
"""
    if new in text:
        return
    if old in text:
        ustring.write_text(text.replace(old, new, 1), encoding="utf-8")
        return

    # Newer OIIO rewrote this diagnostic path to stream output and uses fmt only
    # with a literal format string, so no fmt::runtime compatibility patch is
    # needed.
    compatible_new_format = 'Strutil::fmt::format("    {} \\"{}\\"\\n", c.hash(), c)'
    if compatible_new_format in text and "std::ostringstream out;" in text:
        return

    raise RuntimeError(f"OpenImageIO fmt runtime format compatibility patch no longer matches upstream source: {ustring}")


def _patch_msvc_python_module_link(src_dir: Path) -> None:
    pythonutils = src_dir / "src" / "cmake" / "pythonutils.cmake"
    if not pythonutils.exists():
        raise RuntimeError(f"OpenImageIO python module patch target is missing: {pythonutils}")

    text = pythonutils.read_text(encoding="utf-8", errors="replace")
    new = """\
    set_target_properties(${target_name} PROPERTIES
                          DEBUG_POSTFIX "")
    if (MSVC)
        set_target_properties(${target_name} PROPERTIES
                              PDB_NAME ${target_name}
                              COMPILE_PDB_NAME ${target_name})
        target_link_options (${target_name} PRIVATE /INCREMENTAL:NO)
    endif ()
"""
    if new in text:
        return

    old = """\
    set_target_properties(PyOpenImageIO PROPERTIES
                          DEBUG_POSTFIX "")
"""
    if old not in text:
        raise RuntimeError(f"OpenImageIO python module patch no longer matches upstream source: {pythonutils}")

    pythonutils.write_text(text.replace(old, new, 1), encoding="utf-8")


def _patch_giflib_windows_macro_leak(src_dir: Path) -> None:
    gifinput = src_dir / "src" / "gif.imageio" / "gifinput.cpp"
    if not gifinput.exists():
        raise RuntimeError(f"OpenImageIO giflib macro patch target is missing: {gifinput}")

    text = gifinput.read_text(encoding="utf-8", errors="replace")
    marker = "// OIIO_BUILDER_GIFLIB_WIN32_MACROS_BEGIN"
    if marker in text:
        return

    include = re.search(
        r"^#include <gif_lib\.h>\r?\n(?:#undef reallocarray\r?\n)?",
        text,
        re.MULTILINE,
    )
    if include is None:
        raise RuntimeError(f"OpenImageIO giflib macro patch no longer matches upstream source: {gifinput}")

    # giflib's Windows compatibility header maps generic POSIX function names
    # to MSVC CRT names. Those aliases are needed while building giflib, but
    # must not rewrite OpenImageIO class members declared after gif_lib.h.
    block = """\

#ifdef _WIN32
// OIIO_BUILDER_GIFLIB_WIN32_MACROS_BEGIN
#    undef open
#    undef close
#    undef fdopen
#    undef unlink
#    undef strdup
#    undef strtok_r
// OIIO_BUILDER_GIFLIB_WIN32_MACROS_END
#endif
"""
    gifinput.write_text(text[: include.end()] + block + text[include.end() :], encoding="utf-8")


def _patch_static_robinmap_config(src_dir: Path) -> None:
    config_template = src_dir / "src" / "cmake" / "Config.cmake.in"
    if not config_template.exists():
        raise RuntimeError(f"OpenImageIO static robin-map config patch target is missing: {config_template}")

    original_text = config_template.read_text(encoding="utf-8", errors="replace")
    text = original_text
    marker_begin = "# OIIO_BUILDER_ROBINMAP_STATIC_BEGIN"
    marker_end = "# OIIO_BUILDER_ROBINMAP_STATIC_END"
    block = """\
    # OIIO_BUILDER_ROBINMAP_STATIC_BEGIN
    # Static exported OIIO targets directly reference tsl::robin_map.
    # Import its installed package before OpenImageIOTargets.cmake is loaded.
    if (NOT TARGET tsl::robin_map)
        find_dependency(tsl-robin-map CONFIG)
    endif ()
    # OIIO_BUILDER_ROBINMAP_STATIC_END
"""
    if marker_begin in text and marker_end in text:
        marker_start = text.index(marker_begin)
        start = text.rfind("\n", 0, marker_start) + 1
        stop = text.index(marker_end, marker_start) + len(marker_end)
        if text[stop : stop + 1] == "\n":
            stop += 1
        text = text[:start] + block + text[stop:]
    elif "find_dependency(tsl-robin-map CONFIG)" not in text:
        anchor = "if (NOT @BUILD_SHARED_LIBS@)\n"
        if anchor not in text:
            raise RuntimeError(f"OpenImageIO static robin-map config patch no longer matches upstream source: {config_template}")
        text = text.replace(anchor, anchor + block, 1)

    if text != original_text:
        config_template.write_text(text, encoding="utf-8")


def cmake_args(builder, ctx) -> list[str]:
    return _cache_args(builder, ctx)


def pre_build(builder, _repo, ctx, _env) -> None:
    builder._ensure_pystring_package(ctx.install_prefix, ctx.build_type)
    builder._ensure_dng_sdk_lcms2_compat(ctx.install_prefix, ctx.build_type)
    builder._ensure_png16_include_alias(ctx.install_prefix)


def _ffmpeg_args(
    builder,
    ctx,
    *,
    include_repo_roots: bool,
    emit_missing_note: bool,
) -> tuple[list[str], bool]:
    cfg = builder.config.global_cfg
    args: list[str] = []
    prefix_root = ctx.install_prefix.resolve()

    def _normalize_ffmpeg_override(value: str | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _ffmpeg_override(name: str) -> str | None:
        if builder.platform.os == "windows":
            return _normalize_ffmpeg_override(cfg.windows_env.get(name) or cfg.env.get(name) or os.environ.get(name))
        return _normalize_ffmpeg_override(cfg.env.get(name) or os.environ.get(name))

    def _expand_override_path(value: str) -> Path:
        expanded = Path(os.path.expandvars(value)).expanduser()
        if expanded.is_absolute():
            return expanded.resolve()
        return (cfg.repo_root / expanded).resolve()

    def _is_within_prefix(path: Path) -> bool:
        try:
            path.resolve().relative_to(prefix_root)
            return True
        except ValueError:
            return False

    ffmpeg_roots: list[Path] = []
    ffmpeg_root_overrides: list[Path] = []
    for key in ("FFmpeg_ROOT", "FFMPEG_ROOT"):
        value = _ffmpeg_override(key)
        if not value:
            continue
        expanded = _expand_override_path(value)
        ffmpeg_root_overrides.append(expanded)
        ffmpeg_roots.append(expanded)

    if builder.platform.os == "windows":
        if ffmpeg_root_overrides:
            for root in ffmpeg_root_overrides:
                if not _is_within_prefix(root):
                    print(f"[note] ignoring FFmpeg_ROOT outside install prefix: {root}", flush=True)
        ffmpeg_roots = [prefix_root]
    elif not ffmpeg_root_overrides and include_repo_roots:
        repo_ffmpeg_root = builder.repo_paths.get("ffmpeg")
        if repo_ffmpeg_root is None:
            ffmpeg_repo = next((repo for repo in builder.config.repos if repo.name == "ffmpeg"), None)
            if ffmpeg_repo is not None:
                repo_ffmpeg_root = builder._resolve_repo_dir(ffmpeg_repo)
        if repo_ffmpeg_root is not None and repo_ffmpeg_root.exists():
            ffmpeg_roots.append(repo_ffmpeg_root)

        for candidate_name in ("ffmpeg", "FFmpeg", "FFMPEG"):
            source_ffmpeg_root = cfg.src_root / candidate_name
            if source_ffmpeg_root.exists():
                ffmpeg_roots.append(source_ffmpeg_root)

    ffmpeg_roots.append(ctx.install_prefix)

    deduped_roots: list[Path] = []
    seen_roots: set[str] = set()
    for root in ffmpeg_roots:
        normalized = os.path.normcase(os.path.normpath(str(root)))
        if normalized in seen_roots:
            continue
        seen_roots.add(normalized)
        if root.exists():
            deduped_roots.append(root)
    ffmpeg_roots = deduped_roots

    ffmpeg_include_override = _ffmpeg_override("FFMPEG_AVCODEC_INCLUDE_DIR") or _ffmpeg_override("FFMPEG_INCLUDE_DIR")
    if ffmpeg_include_override:
        candidate = _expand_override_path(ffmpeg_include_override)
        if not _is_within_prefix(candidate):
            print(f"[note] ignoring FFmpeg include override outside install prefix: {candidate}", flush=True)
            ffmpeg_include = None
        else:
            ffmpeg_include = candidate
    else:
        ffmpeg_include = None
        for root in ffmpeg_roots:
            for candidate in (root / "include", root / "include" / "ffmpeg", root):
                if (
                    (candidate / "libavcodec" / "version.h").exists()
                    or (candidate / "libavcodec" / "version_major.h").exists()
                    or (candidate / "libavcodec" / "avcodec.h").exists()
                ):
                    ffmpeg_include = candidate
                    break
            if ffmpeg_include is not None:
                break
    if ffmpeg_include is not None:
        args.append(f"-DFFMPEG_AVCODEC_INCLUDE_DIR={ffmpeg_include.as_posix()}")
        args.append(f"-DFFMPEG_INCLUDE_DIR={ffmpeg_include.as_posix()}")

    ffmpeg_lib_dirs: list[Path] = []
    for root in ffmpeg_roots:
        ffmpeg_lib_dirs.extend(
            [
                root / "lib",
                root / "lib64",
                root / "libavcodec",
                root / "libavformat",
                root / "libavutil",
                root / "libswscale",
            ]
        )
    deduped_lib_dirs: list[Path] = []
    seen_lib_dirs: set[str] = set()
    for directory in ffmpeg_lib_dirs:
        normalized = os.path.normcase(os.path.normpath(str(directory)))
        if normalized in seen_lib_dirs:
            continue
        seen_lib_dirs.add(normalized)
        if directory.exists():
            deduped_lib_dirs.append(directory)
    ffmpeg_lib_dirs = deduped_lib_dirs

    def _pick_ffmpeg_lib(stem: str) -> Path | None:
        if builder.platform.os == "windows":
            debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
            if ctx.build_type == "Debug":
                candidate_names = [
                    f"{stem}{debug_postfix}.lib",
                    f"lib{stem}{debug_postfix}.lib",
                    f"{stem}.lib",
                    f"lib{stem}.lib",
                ]
            else:
                candidate_names = [
                    f"{stem}.lib",
                    f"lib{stem}.lib",
                    f"{stem}{debug_postfix}.lib",
                    f"lib{stem}{debug_postfix}.lib",
                ]
        else:
            lgpl_dynamic = builder.license_profile is not None and builder.license_profile.name == LGPL_DYNAMIC
            if lgpl_dynamic:
                candidate_names = [f"lib{stem}.so", f"lib{stem}.dylib"]
            elif cfg.static_default:
                candidate_names = [f"lib{stem}.a", f"lib{stem}.so", f"lib{stem}.dylib"]
            else:
                candidate_names = [f"lib{stem}.so", f"lib{stem}.dylib", f"lib{stem}.a"]

        for directory in ffmpeg_lib_dirs:
            for name in candidate_names:
                candidate = directory / name
                if candidate.exists():
                    return candidate

        for directory in ffmpeg_lib_dirs:
            if builder.platform.os == "windows":
                patterns = [f"{stem}*.lib", f"lib{stem}*.lib"]
            else:
                patterns = [f"lib{stem}.*"]
            for pattern in patterns:
                matches = sorted(directory.glob(pattern))
                if matches:
                    return matches[0]
        return None

    ffmpeg_codec_override = _ffmpeg_override("FFMPEG_LIBAVCODEC")
    ffmpeg_format_override = _ffmpeg_override("FFMPEG_LIBAVFORMAT")
    ffmpeg_util_override = _ffmpeg_override("FFMPEG_LIBAVUTIL")
    ffmpeg_swscale_override = _ffmpeg_override("FFMPEG_LIBSWSCALE")

    def _maybe_override_lib(value: str | None) -> Path | None:
        if not value:
            return None
        candidate = _expand_override_path(value)
        if not _is_within_prefix(candidate):
            print(f"[note] ignoring FFmpeg lib override outside install prefix: {candidate}", flush=True)
            return None
        return candidate

    ffmpeg_codec = _maybe_override_lib(ffmpeg_codec_override) or _pick_ffmpeg_lib("avcodec")
    ffmpeg_format = _maybe_override_lib(ffmpeg_format_override) or _pick_ffmpeg_lib("avformat")
    ffmpeg_util = _maybe_override_lib(ffmpeg_util_override) or _pick_ffmpeg_lib("avutil")
    ffmpeg_swscale = _maybe_override_lib(ffmpeg_swscale_override) or _pick_ffmpeg_lib("swscale")

    ffmpeg_root_hint: Path | None = None
    if builder.platform.os == "windows":
        ffmpeg_root_hint = prefix_root
    else:
        ffmpeg_root_hint = ffmpeg_root_overrides[0] if ffmpeg_root_overrides else None
    if ffmpeg_root_hint is None:
        for chosen in (ffmpeg_codec, ffmpeg_format, ffmpeg_util, ffmpeg_swscale):
            if chosen is None:
                continue
            parent = chosen.parent
            parent_name = parent.name.lower()
            if parent_name in {"lib", "lib64", "libavcodec", "libavformat", "libavutil", "libswscale", "libswresample"}:
                ffmpeg_root_hint = parent.parent
            else:
                ffmpeg_root_hint = parent
            break
    if ffmpeg_root_hint is None and ffmpeg_roots:
        ffmpeg_root_hint = ffmpeg_roots[0]
    if ffmpeg_root_hint is None:
        ffmpeg_root_hint = ctx.install_prefix
    args.append(f"-DFFmpeg_ROOT={ffmpeg_root_hint.as_posix()}")
    args.append(f"-DFFMPEG_ROOT={ffmpeg_root_hint.as_posix()}")
    if ffmpeg_codec is not None:
        args.append(f"-DFFMPEG_LIBAVCODEC={ffmpeg_codec.as_posix()}")
    if ffmpeg_format is not None:
        args.append(f"-DFFMPEG_LIBAVFORMAT={ffmpeg_format.as_posix()}")
    if ffmpeg_util is not None:
        args.append(f"-DFFMPEG_LIBAVUTIL={ffmpeg_util.as_posix()}")
    if ffmpeg_swscale is not None:
        args.append(f"-DFFMPEG_LIBSWSCALE={ffmpeg_swscale.as_posix()}")

    complete = (
        ffmpeg_include is not None
        and ffmpeg_codec is not None
        and ffmpeg_format is not None
        and ffmpeg_util is not None
        and ffmpeg_swscale is not None
    )
    lgpl_dynamic = builder.license_profile is not None and builder.license_profile.name == LGPL_DYNAMIC
    if complete and lgpl_dynamic and builder.platform.os == "windows":
        dll_dir = ctx.install_prefix / "bin"
        required_dlls = ("avcodec", "avformat", "avutil", "swscale")
        missing_dlls = [stem for stem in required_dlls if not any(dll_dir.glob(f"*{stem}*.dll"))]
        if missing_dlls:
            raise RuntimeError(
                "lgpl-dynamic found FFmpeg import/static libraries but no matching shared DLLs for: "
                f"{', '.join(missing_dlls)}. Install an LGPL-only shared FFmpeg build into the profile prefix."
            )
    if builder.platform.os == "windows" and emit_missing_note and not complete:
        missing_libs: list[str] = []
        if ffmpeg_codec is None:
            missing_libs.append("avcodec")
        if ffmpeg_format is None:
            missing_libs.append("avformat")
        if ffmpeg_util is None:
            missing_libs.append("avutil")
        if ffmpeg_swscale is None:
            missing_libs.append("swscale")
        searched = ", ".join(str(d) for d in ffmpeg_lib_dirs) if ffmpeg_lib_dirs else "<none>"
        message = "[note] FFmpeg files missing"
        if missing_libs:
            message += " for " + ", ".join(missing_libs)
        if ffmpeg_include is None:
            message += " (headers not found)"
        message += (
            f"; searched: {searched}. Install an MSVC-built {'shared' if lgpl_dynamic else 'static'} FFmpeg "
            f"into the build prefix ({ctx.install_prefix}), "
            "or define FFMPEG_LIBAV* overrides that point inside it."
        )
        print(message, flush=True)

    return args, complete


def _cache_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    ffmpeg_requested = ffmpeg_enabled(builder)
    ffmpeg_auto_from_prefix = (
        builder.platform.os == "windows" and not ffmpeg_requested and windows_use_ffmpeg_from_prefix(builder)
    )
    use_ffmpeg = ffmpeg_requested
    args: list[str] = []
    builder._ensure_bzip2_alias(ctx.install_prefix, ctx.build_type)
    builder._ensure_ppmd_package(ctx.install_prefix, ctx.build_type)
    builder._ensure_freetype_harfbuzz_compat(ctx.install_prefix, ctx.build_type)
    cache_path = cfg.src_root / "OpenImageIO" / "build" / "CMakeCache.txt"
    allow = {
        "BUILD_SHARED_LIBS",
        "EMBEDPLUGINS",
        "OIIO_BUILD_TOOLS",
        "OIIO_BUILD_TESTS",
        "OIIO_IV_EXTRA_IV_LIBRARIES",
        "OIIO_INTERNALIZE_FMT",
        "OIIO_USE_COMPILED_FMT",
        "USE_PYTHON",
        "USE_JXL",
        "USE_FREETYPE",
        "USE_LIBUHDR",
        "USE_FFMPEG",
        "USE_QT",
        "USE_TBB",
        "USE_LIBCPLUSPLUS",
        "USE_EXTERNAL_PUGIXML",
        "LINKSTATIC",
    }
    values: dict[str, str] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("//"):
                continue
            if ":" not in line or "=" not in line:
                continue
            key = line.split(":", 1)[0]
            if key in allow:
                values[key] = line.split("=", 1)[1]

    # Defaults aligned with the shell script.
    defaults = {
        "BUILD_SHARED_LIBS": "OFF",
        "EMBEDPLUGINS": "ON",
        "OIIO_BUILD_TOOLS": "ON",
        "OIIO_BUILD_TESTS": "OFF",
        "USE_PYTHON": "ON",
        "USE_JXL": "ON" if cfg.build_libjxl else "OFF",
        "USE_FREETYPE": "ON",
        "USE_LIBUHDR": "ON" if cfg.build_libuhdr else "OFF",
        "LINKSTATIC": "ON",
    }
    for key, value in defaults.items():
        values.setdefault(key, value)

    # Python is mandatory for OIIO in this setup.
    values["USE_PYTHON"] = "ON"

    # Keep OIIO aligned with spdlog/rawgl: one compiled fmt library,
    # not header-only fmt definitions embedded in OIIO objects.
    values["OIIO_INTERNALIZE_FMT"] = "OFF"
    values["OIIO_USE_COMPILED_FMT"] = "ON"

    values["USE_QT"] = "ON" if cfg.build_qt6 else "OFF"
    # OIIO treats TBB/oneTBB as an optional dependency that is enabled by
    # default and autodetected from the system. Keep the managed prefix
    # deterministic unless the user explicitly overrides this later.
    values["USE_TBB"] = "OFF"

    if builder.platform.os == "linux" and cfg.build_qt6 and not values.get("OIIO_IV_EXTRA_IV_LIBRARIES"):
        # Qt6 static DBus linkage on Linux may require systemd symbols
        # via libdbus-1.a (_dbus_listen_systemd_sockets).
        if builder._qt_exports_dbus(ctx.install_prefix):
            values["OIIO_IV_EXTRA_IV_LIBRARIES"] = "dbus-1;systemd"

    # Always embed plugins for consistent single-binary plugin loading across platforms.
    values["EMBEDPLUGINS"] = "ON"

    # Pugixml: use external only when it's part of the planned build and present in the prefix.
    # Otherwise, let OIIO fall back to its internal copy.
    pugixml_planned = any(repo.name == "pugixml" for repo in builder.repos)
    pugixml_config_dir = ctx.install_prefix / "lib" / "cmake" / "pugixml"
    pugixml_config_found = any(
        (pugixml_config_dir / name).exists() for name in ("pugixml-config.cmake", "pugixmlConfig.cmake")
    )
    pugixml_header_found = (ctx.install_prefix / "include" / "pugixml.hpp").exists()
    if builder.platform.os == "windows":
        pugixml_lib_found = bool(list((ctx.install_prefix / "lib").glob("pugixml*.lib")))
    else:
        pugixml_lib_found = bool(list((ctx.install_prefix / "lib").glob("libpugixml.*")))
    pugixml_found = pugixml_config_found or (pugixml_header_found and pugixml_lib_found)
    values["USE_EXTERNAL_PUGIXML"] = "ON" if (pugixml_planned and pugixml_found) else "OFF"
    required = ["GIF", "JXL", "LibRaw", "libuhdr", "Freetype"]
    if cfg.build_qt6:
        required.insert(0, "Qt6")
        required.insert(1, "OpenGL")

    # Keep dependency discovery deterministic by hinting the shared prefix.
    root_vars = (
        "ZLIB",
        "GIF",
        "PNG",
        "JPEG",
        "TIFF",
        "JXL",
        "OpenColorIO",
        "Freetype",
        "BZip2",
        "libuhdr",
        "Robinmap",
        "fmt",
        "OpenEXR",
        "Imath",
        "pugixml",
        "pybind11",
    )
    install_prefix_posix = ctx.install_prefix.as_posix()
    include_dir = ctx.install_prefix / "include"
    include_dir_posix = include_dir.as_posix()
    lib_dir = ctx.install_prefix / "lib"
    lib_dir_posix = lib_dir.as_posix()

    def _pick_library(stems: list[str]) -> Path | None:
        if builder.platform.os == "windows":
            debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
            if ctx.build_type == "Debug":
                ordered: list[Path] = []
                for stem in stems:
                    ordered.extend(
                        [
                            lib_dir / f"{stem}{debug_postfix}.lib",
                            lib_dir / f"{stem}.lib",
                            lib_dir / f"lib{stem}{debug_postfix}.lib",
                            lib_dir / f"lib{stem}.lib",
                        ]
                    )
            else:
                ordered = []
                for stem in stems:
                    ordered.extend(
                        [
                            lib_dir / f"{stem}.lib",
                            lib_dir / f"{stem}{debug_postfix}.lib",
                            lib_dir / f"lib{stem}.lib",
                            lib_dir / f"lib{stem}{debug_postfix}.lib",
                        ]
                    )
            found = next((candidate for candidate in ordered if candidate.exists()), None)
            if found is not None:
                return found
            matches: list[Path] = []
            for stem in stems:
                matches.extend(sorted(lib_dir.glob(f"{stem}*.lib")))
                matches.extend(sorted(lib_dir.glob(f"lib{stem}*.lib")))
            return matches[0] if matches else None

        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        ordered = []
        for stem in stems:
            release_names = [
                lib_dir / f"lib{stem}.a",
                lib_dir / f"lib{stem}.so",
                lib_dir / f"lib{stem}.dylib",
                lib_dir / f"{stem}.a",
            ]
            debug_names = [
                lib_dir / f"lib{stem}{debug_postfix}.a",
                lib_dir / f"lib{stem}{debug_postfix}.so",
                lib_dir / f"lib{stem}{debug_postfix}.dylib",
                lib_dir / f"{stem}{debug_postfix}.a",
            ]
            ordered.extend(debug_names + release_names if ctx.build_type == "Debug" else release_names + debug_names)
        found = next((candidate for candidate in ordered if candidate.exists()), None)
        if found is not None:
            return found
        matches = []
        for stem in stems:
            matches.extend(sorted(lib_dir.glob(f"lib{stem}*.*")))
            matches.extend(sorted(lib_dir.glob(f"{stem}*.a")))
        return matches[0] if matches else None

    for var in root_vars:
        args.append(f"-D{var}_ROOT={install_prefix_posix}")

    pystring_include = include_dir / "pystring"
    if not pystring_include.exists():
        pystring_include = include_dir
    args.append(f"-Dpystring_ROOT={install_prefix_posix}")
    args.append(f"-Dpystring_INCLUDE_DIR={pystring_include.as_posix()}")

    robinmap_include = ctx.install_prefix / "include"
    if not (robinmap_include / "tsl" / "robin_map.h").exists():
        source_robinmap_include = cfg.src_root / "robin-map" / "include"
        if (source_robinmap_include / "tsl" / "robin_map.h").exists():
            robinmap_include = source_robinmap_include
    if (robinmap_include / "tsl" / "robin_map.h").exists():
        args.append(f"-DROBINMAP_INCLUDE_DIR={robinmap_include.as_posix()}")

    fmt_dir_candidates = [
        lib_dir / "cmake" / "fmt",
        ctx.install_prefix / "share" / "cmake" / "fmt",
    ]
    for fmt_dir in fmt_dir_candidates:
        if (fmt_dir / "fmt-config.cmake").exists() or (fmt_dir / "fmtConfig.cmake").exists():
            args.append(f"-Dfmt_DIR={fmt_dir.as_posix()}")
            break

    if builder.platform.os == "windows":
        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        if ctx.build_type == "Debug":
            png_candidates = [
                lib_dir / f"libpng16_static{debug_postfix}.lib",
                lib_dir / f"png16_static{debug_postfix}.lib",
                lib_dir / f"libpng16{debug_postfix}.lib",
                lib_dir / f"png{debug_postfix}.lib",
            ]
            pystring_candidates = [lib_dir / f"pystring{debug_postfix}.lib", lib_dir / "pystring.lib"]
        else:
            png_candidates = [
                lib_dir / "libpng16_static.lib",
                lib_dir / "png16_static.lib",
                lib_dir / "libpng16.lib",
                lib_dir / "png.lib",
            ]
            pystring_candidates = [lib_dir / "pystring.lib", lib_dir / f"pystring{debug_postfix}.lib"]
    else:
        png_candidates = [
            lib_dir / "libpng16.a",
            lib_dir / "libpng.a",
            lib_dir / "libpng16d.a",
        ]
        pystring_candidates = [lib_dir / "libpystring.a", lib_dir / "libpystringd.a", lib_dir / "libpystring_d.a"]

    png_library = next((candidate for candidate in png_candidates if candidate.exists()), None)
    if png_library is None:
        if builder.platform.os == "windows":
            matches = sorted(lib_dir.glob("libpng*.lib")) + sorted(lib_dir.glob("png*.lib"))
        else:
            matches = sorted(lib_dir.glob("libpng*.a"))
        if matches:
            png_library = matches[0]

    if png_library is not None:
        args.append(f"-DPNG_LIBRARY={png_library.as_posix()}")
        args.append(f"-DPNG_PNG_INCLUDE_DIR={include_dir_posix}")

    pystring_library = next((candidate for candidate in pystring_candidates if candidate.exists()), None)
    if pystring_library is None:
        if builder.platform.os == "windows":
            matches = sorted(lib_dir.glob("pystring*.lib"))
        else:
            matches = sorted(lib_dir.glob("libpystring*.a"))
        if matches:
            pystring_library = matches[0]
    if pystring_library is not None:
        args.append(f"-Dpystring_LIBRARY={pystring_library.as_posix()}")

    if (include_dir / "jxl" / "decode.h").exists():
        args.append(f"-DJXL_INCLUDE_DIR={include_dir_posix}")
    jxl_library = _pick_library(["jxl"])
    if jxl_library is not None:
        args.append(f"-DJXL_LIBRARY={jxl_library.as_posix()}")
    jxl_threads_library = _pick_library(["jxl_threads"])
    if jxl_threads_library is not None:
        args.append(f"-DJXL_THREADS_LIBRARY={jxl_threads_library.as_posix()}")

    # DNG-CMake's package fallback logic may search only non-debug names
    # (e.g. hwy, brotlicommon) on Windows. Provide explicit paths so
    # Debug-only prefixes with *d.lib names resolve correctly.
    if builder.platform.os == "windows" and cfg.build_dng_sdk:
        jxl_cms_library = _pick_library(["jxl_cms"])
        if jxl_cms_library is not None:
            args.append(f"-DJXL_CMS_LIBRARY={jxl_cms_library.as_posix()}")
        hwy_library = _pick_library(["hwy"])
        if hwy_library is not None:
            args.append(f"-DHWY_LIBRARY={hwy_library.as_posix()}")
        brotli_common_library = _pick_library(["brotlicommon"])
        if brotli_common_library is not None:
            args.append(f"-DBROTLI_COMMON_LIBRARY={brotli_common_library.as_posix()}")
        brotli_dec_library = _pick_library(["brotlidec"])
        if brotli_dec_library is not None:
            args.append(f"-DBROTLI_DEC_LIBRARY={brotli_dec_library.as_posix()}")
        brotli_enc_library = _pick_library(["brotlienc"])
        if brotli_enc_library is not None:
            args.append(f"-DBROTLI_ENC_LIBRARY={brotli_enc_library.as_posix()}")

    gif_include = include_dir if (include_dir / "gif_lib.h").exists() else None
    gif_library = _pick_library(["gif", "giflib", "libgif"])
    if gif_include is not None:
        args.append(f"-DGIF_INCLUDE_DIR={gif_include.as_posix()}")
    if gif_library is not None:
        args.append(f"-DGIF_LIBRARY={gif_library.as_posix()}")

    libraw_include = include_dir if (include_dir / "libraw" / "libraw.h").exists() else None
    libraw_library = _pick_library(["raw", "raw_r", "libraw", "libraw_r"])
    libraw_r_library = _pick_library(["raw_r", "libraw_r", "raw", "libraw"])
    if libraw_include is not None:
        args.append(f"-DLibRaw_ROOT={install_prefix_posix}")
        args.append(f"-DLIBRAW_INCLUDEDIR_HINT={include_dir_posix}")
        args.append(f"-DLibRaw_INCLUDE_DIR={libraw_include.as_posix()}")
    args.append(f"-DLIBRAW_LIBDIR_HINT={lib_dir_posix}")
    if libraw_library is not None:
        args.append(f"-DLibRaw_LIBRARIES={libraw_library.as_posix()}")
    if libraw_r_library is not None:
        args.append(f"-DLibRaw_r_LIBRARIES={libraw_r_library.as_posix()}")

    libuhdr_include = None
    for candidate in (include_dir, include_dir / "libuhdr", include_dir / "ultrahdr"):
        if (candidate / "ultrahdr_api.h").exists():
            libuhdr_include = candidate
            break
    # Windows static builds of libultrahdr often install as `uhdr-static(.lib)`
    # and don't provide a module/config that lets CMake choose Debug vs Release
    # automatically. Prefer the `-static` name so we pick `...d.lib` for Debug.
    libuhdr_library = _pick_library(["uhdr-static", "uhdr", "libuhdr"])
    if libuhdr_include is not None:
        args.append(f"-DLIBUHDR_INCLUDE_DIR={libuhdr_include.as_posix()}")
    if libuhdr_library is not None:
        args.append(f"-DLIBUHDR_LIBRARY={libuhdr_library.as_posix()}")

    heif_library = None
    if builder.platform.os == "windows":
        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        if ctx.build_type == "Debug":
            heif_candidates = [lib_dir / f"heif{debug_postfix}.lib", lib_dir / "heif.lib", lib_dir / f"libheif{debug_postfix}.lib"]
        else:
            heif_candidates = [lib_dir / "heif.lib", lib_dir / f"heif{debug_postfix}.lib", lib_dir / "libheif.lib"]
        heif_library = next((candidate for candidate in heif_candidates if candidate.exists()), None)
        if heif_library is None:
            heif_matches = sorted(lib_dir.glob("*heif*.lib"))
            if heif_matches:
                heif_library = heif_matches[0]
    else:
        heif_candidates = [lib_dir / "libheif.a", lib_dir / "libheif.so", lib_dir / "libheif.dylib"]
        heif_library = next((candidate for candidate in heif_candidates if candidate.exists()), None)
        if heif_library is None:
            heif_matches = sorted(lib_dir.glob("libheif.*"))
            if heif_matches:
                heif_library = heif_matches[0]
    if heif_library is not None:
        args.append(f"-DLibheif_ROOT={install_prefix_posix}")
        args.append(f"-DLIBHEIF_INCLUDE_PATH={include_dir_posix}")
        args.append(f"-DLIBHEIF_LIBRARY_PATH={lib_dir_posix}")
        args.append(f"-DLIBHEIF_INCLUDE_DIR={include_dir_posix}")
        args.append(f"-DLIBHEIF_LIBRARY={heif_library.as_posix()}")

    ffmpeg_probe = ffmpeg_requested or ffmpeg_auto_from_prefix
    if ffmpeg_probe:
        ffmpeg_args, ffmpeg_complete = _ffmpeg_args(
            builder,
            ctx,
            include_repo_roots=not ffmpeg_auto_from_prefix,
            emit_missing_note=ffmpeg_requested,
        )
        if ffmpeg_requested or ffmpeg_complete:
            use_ffmpeg = True
            values.setdefault("USE_FFMPEG", "ON")
            args.extend(ffmpeg_args)
    values["USE_FFMPEG"] = "ON" if use_ffmpeg else "OFF"
    if use_ffmpeg:
        required.insert(0, "FFmpeg")
    args.append(f"-DOpenImageIO_REQUIRED_DEPS={';'.join(required)}")

    # Ensure static dependency linking is propagated for static builds.
    args.append(f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={_linkstatic_include(builder, ctx)}")

    for key in sorted(values):
        args.append(f"-D{key}={values[key]}")
    return args


def _linkstatic_include(builder, ctx) -> str:
    include_path = ctx.build_dir / "oiio_linkstatic.cmake"
    extra_libs = _extra_static_libs(builder, ctx.install_prefix, ctx.build_type)

    def _cmake_quote(value: str) -> str:
        # CMake treats backslashes as escapes inside strings, so always normalize
        # Windows paths to forward slashes before embedding.
        if builder.platform.os == "windows":
            value = value.replace("\\", "/")
        value = value.replace('"', '\\"')
        return f"\"{value}\""

    extra_list = "\n  ".join(_cmake_quote(entry) for entry in extra_libs) if extra_libs else ""
    static_defs = _static_preprocessor_definitions(builder, ctx.install_prefix)
    static_defs_list = "\n  ".join(static_defs) if static_defs else ""
    content = """\
set(_oiio_static_defs
  __EXTRA_DEFINITIONS__
)
if (NOT BUILD_SHARED_LIBS)
  foreach(_oiio_def IN LISTS _oiio_static_defs)
    if (NOT "${_oiio_def}" STREQUAL "")
      add_compile_definitions(${_oiio_def})
    endif()
  endforeach()
endif()

function(_oiio_sanitize_split_define_options _target)
  if (NOT TARGET "${_target}")
    return()
  endif()
  get_target_property(_oiio_opts "${_target}" COMPILE_OPTIONS)
  if (NOT _oiio_opts)
    return()
  endif()
  set(_oiio_sanitized_opts)
  set(_oiio_pending_define OFF)
  foreach(_oiio_opt IN LISTS _oiio_opts)
    if (_oiio_pending_define)
      if (NOT "${_oiio_opt}" STREQUAL "")
        if (_oiio_opt MATCHES "^[-/]")
          list(APPEND _oiio_sanitized_opts "${_oiio_opt}")
        else()
          target_compile_definitions("${_target}" PRIVATE "${_oiio_opt}")
        endif()
      endif()
      set(_oiio_pending_define OFF)
      continue()
    endif()
    if ("${_oiio_opt}" STREQUAL "-D" OR "${_oiio_opt}" STREQUAL "/D")
      set(_oiio_pending_define ON)
    else()
      list(APPEND _oiio_sanitized_opts "${_oiio_opt}")
    endif()
  endforeach()
  set_target_properties("${_target}" PROPERTIES COMPILE_OPTIONS "${_oiio_sanitized_opts}")
endfunction()

function(_oiio_linkstatic_fixup)
  if (NOT TARGET OpenImageIO)
    return()
  endif()
  if (BUILD_SHARED_LIBS)
    return()
  endif()
  _oiio_sanitize_split_define_options(OpenImageIO)
  set(_oiio_extra_libs
  __EXTRA_LIBS__
  )
  get_target_property(_oiio_private OpenImageIO LINK_LIBRARIES)
  if (_oiio_private)
    set_property(TARGET OpenImageIO APPEND PROPERTY INTERFACE_LINK_LIBRARIES "${_oiio_private}")
  endif()
  if (TARGET OpenImageIO_Util)
    set_property(TARGET OpenImageIO_Util APPEND PROPERTY INTERFACE_LINK_LIBRARIES "${_oiio_extra_libs}")
  endif()
  set_property(TARGET OpenImageIO APPEND PROPERTY INTERFACE_LINK_LIBRARIES "${_oiio_extra_libs}")
endfunction()

if (CMAKE_VERSION VERSION_GREATER_EQUAL \"3.19\")
  cmake_language(DEFER CALL _oiio_linkstatic_fixup)
else()
  _oiio_linkstatic_fixup()
endif()
"""
    include_path.write_text(
        content.replace("__EXTRA_DEFINITIONS__", static_defs_list).replace("__EXTRA_LIBS__", extra_list),
        encoding="utf-8",
    )
    return include_path.as_posix() if builder.platform.os == "windows" else str(include_path)


def _extra_static_libs(builder, prefix: Path, build_type: str) -> list[str]:
    prefix = prefix.resolve()
    libs: list[str] = []
    libdir = prefix / "lib"
    seen: set[str] = set()
    minizip_requires_ppmd = builder._minizip_exports_ppmd(prefix)

    libraw_openmp_value = str(builder.config.global_cfg.libraw_enable_openmp).strip().lower()
    libraw_openmp_enabled = libraw_openmp_value in {"1", "on", "true", "yes"}

    def add_entry(entry: str) -> None:
        normalized = os.path.normcase(os.path.normpath(entry))
        if normalized in seen:
            return
        seen.add(normalized)
        libs.append(entry)

    def add_lib(name: str) -> None:
        path = libdir / name
        if path.exists():
            add_entry(str(path))

    if builder.platform.os == "windows":
        debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
        prefer_debug = build_type == "Debug"

        def add_windows_library(stems: list[str]) -> None:
            candidates: list[Path] = []
            for stem in stems:
                if prefer_debug:
                    candidates.extend(
                        [
                            libdir / f"{stem}{debug_postfix}.lib",
                            libdir / f"lib{stem}{debug_postfix}.lib",
                            libdir / f"{stem}.lib",
                            libdir / f"lib{stem}.lib",
                        ]
                    )
                else:
                    candidates.extend(
                        [
                            libdir / f"{stem}.lib",
                            libdir / f"lib{stem}.lib",
                            libdir / f"{stem}{debug_postfix}.lib",
                            libdir / f"lib{stem}{debug_postfix}.lib",
                        ]
                    )
            for candidate in candidates:
                if candidate.exists():
                    add_entry(str(candidate))
                    return

            matches: list[Path] = []
            for stem in stems:
                matches.extend(sorted(libdir.glob(f"{stem}*.lib")))
                matches.extend(sorted(libdir.glob(f"lib{stem}*.lib")))
            if matches:
                add_entry(str(matches[0]))

        # JXL deps
        add_windows_library(["jxl_cms"])
        add_windows_library(["brotlidec"])
        add_windows_library(["brotlienc"])
        add_windows_library(["brotlicommon"])
        add_windows_library(["hwy"])
        add_windows_library(["hwy_contrib"])

        # LibRaw deps
        # Prefer the static LCMS2 library to avoid accidentally pulling in
        # the DLL import library when both exist in the prefix.
        add_windows_library(["lcms2_static", "lcms2"])
        add_windows_library(["jasper"])

        # HEIF deps
        add_windows_library(["aom"])
        add_windows_library(["de265", "libde265"])
        add_windows_library(["x265-static", "x265"])
        add_windows_library(["kvazaar", "libkvazaar"])

        # FFmpeg deps (if locally installed as .lib)
        add_windows_library(["avformat"])
        add_windows_library(["avcodec"])
        add_windows_library(["swresample"])
        add_windows_library(["swscale"])
        add_windows_library(["avutil"])

        # minizip-ng may export PPMD when built with MZ_PPMD=ON.
        if minizip_requires_ppmd:
            add_windows_library(["ppmd"])

        # Freetype deps (HarfBuzz)
        add_windows_library(["harfbuzz"])

        # System libs needed by static deps (minizip-ng, FFmpeg, etc.).
        # FFmpeg builds with Media Foundation support reference IIDs from
        # mfuuid/strmiids through avcodec's mfenc/mf_utils objects.
        for syslib in (
            "bcrypt.lib",
            "ncrypt.lib",
            "crypt32.lib",
            "ws2_32.lib",
            "secur32.lib",
            "mfuuid.lib",
            "strmiids.lib",
        ):
            add_entry(syslib)
        # `ucrt(d).lib` are the import libraries for the UCRT DLL and should
        # not be forced for `/MT` builds (it causes CRT mixing).
        if builder._windows_runtime_mode() == "dynamic":
            add_entry("ucrtd.lib" if prefer_debug else "ucrt.lib")
    else:
        # JXL deps
        add_lib("libjxl_cms.a")
        add_lib("libbrotlidec.a")
        add_lib("libbrotlienc.a")
        add_lib("libbrotlicommon.a")
        add_lib("libhwy.a")
        add_lib("libhwy_contrib.a")

        # LibRaw deps
        add_lib("liblcms2.a")
        add_lib("libjasper.a")

        # HEIF deps
        add_lib("libaom.a")
        add_lib("libde265.a")
        add_lib("libx265.a")
        add_lib("libkvazaar.a")

        # FFmpeg deps
        add_lib("libavformat.a")
        add_lib("libavcodec.a")
        add_lib("libswresample.a")
        add_lib("libswscale.a")
        add_lib("libavutil.a")
        if builder.platform.os == "linux" and ffmpeg_enabled(builder):
            # FFmpeg static libs may reference system hwaccel/display libs
            # (e.g. vdpau/x11/drm) via transitive symbols.
            for syslib in ("vdpau", "X11", "drm", "xcb", "Xau", "Xdmcp", "pthread", "atomic"):
                add_entry(syslib)

        # minizip-ng may export PPMD when built with MZ_PPMD=ON.
        if minizip_requires_ppmd:
            add_lib("libppmd.a")

        # Freetype deps (HarfBuzz)
        add_lib("libharfbuzz.a")

    # OpenMP runtime for LibRaw when OIIO is linked statically.
    omp_env = dict(builder.config.global_cfg.env)
    if builder.platform.os == "windows":
        omp_env.update(builder.config.global_cfg.windows_env)
    omp_root = resolve_openmp_root(omp_env, platform_os=builder.platform.os)
    omp_added = False
    if libraw_openmp_enabled:
        windows_clang_openmp = builder.platform.os == "windows" and builder._windows_generator() in {
            "msvc-clang-cl",
            "ninja-clang-cl",
        }

        if builder.platform.os == "windows" and not windows_clang_openmp:
            # MSVC cl.exe `/openmp` emits vcomp default-lib directives.
            # Keep the exported static OIIO link interface aligned with
            # the objects LibRaw actually produced instead of forcing LLVM
            # libomp into an MSVC OpenMP build.
            add_entry("vcompd.lib" if build_type == "Debug" else "vcomp.lib")
            omp_added = True
        else:
            if omp_root:
                candidates = ["libomp.dylib", "libomp.a", "libomp.so", "libiomp5.so", "libgomp.so"]
                if builder.platform.os == "windows":
                    candidates = ["libomp.lib", "libompd.lib", "libiomp5md.lib", "libiomp5mdd.lib"] + candidates
                for candidate in candidates:
                    path = Path(omp_root) / "lib" / candidate
                    if path.exists():
                        add_entry(str(path))
                        omp_added = True
                        break

        if builder.platform.os == "windows" and windows_clang_openmp and not omp_added:
            # Prefer using the clang toolchain's bundled OpenMP runtime when
            # present (VS clang-cl includes `libomp.lib` and `libomp.dll`).
            def _try_add_libomp_root(root: Path) -> bool:
                if not root:
                    return False
                for name in ("libomp.lib", "libompd.lib", "libiomp5md.lib", "libiomp5mdd.lib"):
                    path = root / "lib" / name
                    if path.exists():
                        add_entry(str(path))
                        return True
                return False

            # VS developer prompt env vars (best effort).
            vc_tools = os.environ.get("VCToolsInstallDir")
            if vc_tools:
                try:
                    tools_dir = Path(vc_tools).resolve().parents[1]  # .../VC/Tools
                    if _try_add_libomp_root(tools_dir / "Llvm" / "x64"):
                        omp_added = True
                except Exception:
                    pass

            if not omp_added:
                vc_install = os.environ.get("VCINSTALLDIR")
                if vc_install:
                    try:
                        if _try_add_libomp_root(Path(vc_install).resolve() / "Tools" / "Llvm" / "x64"):
                            omp_added = True
                    except Exception:
                        pass

            if not omp_added:
                vs_install = os.environ.get("VSINSTALLDIR")
                if vs_install:
                    try:
                        if _try_add_libomp_root(Path(vs_install).resolve() / "VC" / "Tools" / "Llvm" / "x64"):
                            omp_added = True
                    except Exception:
                        pass

            if not omp_added:
                clang_cl = shutil.which("clang-cl")
                if clang_cl:
                    try:
                        root = Path(clang_cl).resolve().parent.parent  # .../x64
                        if _try_add_libomp_root(root):
                            omp_added = True
                    except Exception:
                        pass

            if not omp_added:
                # Last-resort: scan common Visual Studio install layouts under Program Files.
                search_bases: list[str] = []
                for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
                    base = os.environ.get(env_name)
                    if base:
                        search_bases.append(base)
                # Visual Studio can be installed on non-system drives, so also probe common drive letters.
                for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                    search_bases.append(f"{drive}:\\Program Files")
                    search_bases.append(f"{drive}:\\Program Files (x86)")

                seen_bases: set[str] = set()
                for base in search_bases:
                    if base in seen_bases:
                        continue
                    seen_bases.add(base)
                    vs_root = Path(base) / "Microsoft Visual Studio"
                    if not vs_root.exists():
                        continue
                    for pattern in (
                        "*/*/VC/Tools/Llvm/*/lib/libomp.lib",
                        "*/*/VC/Tools/Llvm/*/lib/libiomp5md.lib",
                    ):
                        matches = sorted(vs_root.glob(pattern))
                        if matches:
                            add_entry(str(matches[0]))
                            omp_added = True
                            break
                    if omp_added:
                        break

    if builder.platform.os == "linux" and not omp_added:
        for path in (
            Path("/usr/lib/x86_64-linux-gnu/libiomp5.so"),
            Path("/usr/lib/x86_64-linux-gnu/libomp.so"),
            Path("/usr/lib/x86_64-linux-gnu/libomp.so.5"),
            Path("/usr/lib/llvm-20/lib/libomp.so"),
            Path("/usr/lib/x86_64-linux-gnu/libgomp.so.1"),
        ):
            if path.exists():
                add_entry(str(path))
                omp_added = True
                break

    if builder.platform.os == "linux" and libraw_openmp_enabled and not omp_added:
        # Last-resort fallback when LibRaw was compiled with OpenMP but no
        # absolute runtime path was discovered.
        add_entry("omp")

    # iconv (system)
    if (libdir / "libiconv.a").exists():
        add_entry(str(libdir / "libiconv.a"))
    else:
        if builder.platform.os == "macos":
            add_entry("iconv")

    # macOS Security framework (minizip-ng uses SecRandomCopyBytes)
    if builder.platform.os == "macos":
        # Static HarfBuzz uses CoreText, and newer SDK FreeType builds may
        # reference the system HVF font renderer.
        add_entry("-Wl,-framework,CoreText")
        add_entry("hvf")
        add_entry("-Wl,-framework,Security")
        if ffmpeg_enabled(builder):
            for framework_flag in (
                "-Wl,-framework,AudioToolbox",
                "-Wl,-framework,VideoToolbox",
                "-Wl,-framework,CoreMedia",
                "-Wl,-framework,CoreVideo",
                "-Wl,-framework,CoreFoundation",
            ):
                add_entry(framework_flag)

    return libs


def _static_preprocessor_definitions(builder, prefix: Path) -> list[str]:
    prefix = prefix.resolve()
    include_dir = prefix / "include"
    lib_dir = prefix / "lib"

    def _has_any_library(stems: list[str]) -> bool:
        for stem in stems:
            if builder.platform.os == "windows":
                if any(lib_dir.glob(f"{stem}*.lib")) or any(lib_dir.glob(f"lib{stem}*.lib")):
                    return True
            else:
                if any(lib_dir.glob(f"lib{stem}.a")) or any(lib_dir.glob(f"lib{stem}.so")) or any(lib_dir.glob(f"lib{stem}.dylib")):
                    return True
        return False

    defs: list[str] = []

    if (include_dir / "jxl" / "jxl_export.h").exists() and _has_any_library(["jxl", "jxl_threads"]):
        defs.append("JXL_STATIC_DEFINE=1")
    if (
        ((include_dir / "openjpeg-2.5" / "openjpeg.h").exists() or (include_dir / "openjpeg" / "openjpeg.h").exists())
        and _has_any_library(["openjp2"])
    ):
        defs.append("OPJ_STATIC")
    if (include_dir / "libheif" / "heif.h").exists() and _has_any_library(["heif", "libheif"]):
        defs.append("LIBHEIF_STATIC_BUILD")
    if (include_dir / "libde265" / "de265.h").exists() and _has_any_library(["de265", "libde265"]):
        defs.append("LIBDE265_STATIC_BUILD")
    if (include_dir / "kvazaar.h").exists() and _has_any_library(["kvazaar"]):
        defs.append("KVZ_STATIC_LIB")

    return defs


def patch_source(builder, src_dir: Path) -> None:
    if not builder.dry_run:
        _patch_compiled_fmt_option(src_dir)
        _patch_msvc_python_module_link(src_dir)
        if builder.platform.os == "windows":
            _patch_giflib_windows_macro_leak(src_dir)
        _patch_static_robinmap_config(src_dir)

    cfg = builder.config.global_cfg
    if not getattr(cfg, "build_dng_sdk", False):
        return
    if builder.dry_run:
        return

    find_libraw = src_dir / "src" / "cmake" / "modules" / "FindLibRaw.cmake"
    if not find_libraw.exists():
        return

    text = find_libraw.read_text(encoding="utf-8", errors="replace")
    block = """\
    # OIIO_BUILDER_DNGSDK_BEGIN
    # If LibRaw was compiled with -DUSE_DNGSDK, static consumers must also link
    # the DNG SDK + XMP libraries (and transitive deps).
    #
    # Prefer the CMake package produced by DNG-CMake. It propagates platform
    # compile definitions (qLinux/qWinOS/...) and static transitive deps
    # (XMPCoreStatic/XMPFilesStatic, libjxl/brotli/hwy, Threads, JPEG, ...).
    find_package (dng_sdk CONFIG QUIET)
    if (TARGET dng_sdk::dng_sdk)
        set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} dng_sdk::dng_sdk)
        set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} dng_sdk::dng_sdk)
    else ()
        # Fallback to direct library discovery for older/non-packaged SDK builds.
        find_library (DNGSDK_LIBRARY NAMES dng_sdk dng)
        find_library (XMPCORE_LIBRARY NAMES XMPCoreStatic XMPCore)
        find_library (XMPFILES_LIBRARY NAMES XMPFilesStatic XMPFiles)
        if (DNGSDK_LIBRARY AND XMPCORE_LIBRARY)
            set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} ${DNGSDK_LIBRARY} ${XMPCORE_LIBRARY})
            set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} ${DNGSDK_LIBRARY} ${XMPCORE_LIBRARY})
            if (XMPFILES_LIBRARY)
                set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} ${XMPFILES_LIBRARY})
                set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} ${XMPFILES_LIBRARY})
            endif ()

            find_package (EXPAT QUIET)
            if (EXPAT_FOUND)
                if (TARGET EXPAT::EXPAT)
                    set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} EXPAT::EXPAT)
                    set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} EXPAT::EXPAT)
                elseif (TARGET expat::expat)
                    set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} expat::expat)
                    set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} expat::expat)
                elseif (EXPAT_LIBRARIES)
                    set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} ${EXPAT_LIBRARIES})
                    set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} ${EXPAT_LIBRARIES})
                endif ()
            endif ()
        endif ()
    endif ()
    # OIIO_BUILDER_DNGSDK_END
"""

    lines = text.splitlines()
    marker = "OIIO_BUILDER_DNGSDK_BEGIN"
    if marker in text:
        begin: int | None = None
        end: int | None = None
        for i, line in enumerate(lines):
            if marker in line:
                begin = i
                break
        if begin is None:
            return
        for j in range(begin + 1, len(lines)):
            if "OIIO_BUILDER_DNGSDK_END" in lines[j]:
                end = j
                break
        if end is None:
            return
        replacement = block.rstrip("\n").splitlines()
        if lines[begin : end + 1] != replacement:
            lines[begin : end + 1] = replacement
            find_libraw.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    inserted = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("if (MSVC)"):
            lines.insert(i, block.rstrip("\n"))
            inserted = True
            break
    if not inserted:
        return

    find_libraw.write_text("\n".join(lines) + "\n", encoding="utf-8")


def post_install(builder, install_prefix, _build_type: str) -> None:
    builder._ensure_png16_include_alias(install_prefix)
