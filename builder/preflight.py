from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import shutil
import os
import sys

from .config import Config, _expand_path
from .core import Builder
from .platform import PlatformInfo

_PREFLIGHT_REPO_URLS: dict[str, str] = {
    "libxml2": "https://gitlab.gnome.org/GNOME/libxml2.git",
    "pugixml": "https://github.com/zeux/pugixml.git",
    "expat": "https://github.com/libexpat/libexpat.git",
    "yaml-cpp": "https://github.com/jbeder/yaml-cpp.git",
    "pybind11": "https://github.com/pybind/pybind11.git",
    "cpython": "https://github.com/python/cpython.git",
    "lcms2": "https://github.com/mm2/Little-CMS.git",
    "glew": "https://github.com/Perlmint/glew-cmake.git",
    "glfw": "https://github.com/glfw/glfw.git",
    "pystring": "https://github.com/imageworks/pystring.git",
    "freetype": "https://github.com/freetype/freetype.git",
    "harfbuzz": "https://github.com/harfbuzz/harfbuzz.git",
    "bzip2": "https://gitlab.com/federicomenaquintero/bzip2.git",
    "sqlite": "https://github.com/sqlite/sqlite.git",
    "libffi": "https://github.com/libffi/libffi.git",
}


@dataclass
class ToolCheck:
    name: str
    candidates: list[str]
    required: bool
    note: str = ""


def _find_any(candidates: list[str]) -> tuple[str | None, str]:
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path, name
    return None, ""


def _normalize_override(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"\"", "'"}:
        trimmed = trimmed[1:-1]
    return trimmed or None


def _resolve_dngsdk_archive_path(env: dict[str, str], repo_root: Path) -> Path | None:
    override = _normalize_override(env.get("DNGSDK_ARCHIVE") or env.get("DNG_SDK_ARCHIVE"))
    if override:
        value = Path(os.path.expandvars(override)).expanduser()
        if not value.is_absolute():
            value = (repo_root / value).resolve()
        return value

    external_dir = repo_root / "external"
    if not external_dir.is_dir():
        return None

    preferred = [
        external_dir / "dng_sdk_1_7_1_0.zip",
        external_dir / "dng_sdk_1_7_1_0.tar.gz",
        external_dir / "dng_sdk_1_7_1_0.tgz",
    ]
    for candidate in preferred:
        if candidate.exists():
            return candidate

    patterns = [
        "dng_sdk*.zip",
        "dng_sdk*.tar.gz",
        "dng_sdk*.tgz",
        "*dng*sdk*.zip",
        "*DNG*SDK*.zip",
    ]
    for pattern in patterns:
        matches = sorted(external_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def _read_os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key:
                data[key] = value
    except OSError:
        return {}
    return data


def _is_debian_like(os_release: dict[str, str]) -> bool:
    distro_id = os_release.get("ID", "").strip().lower()
    if distro_id in {"debian", "ubuntu", "pop", "linuxmint"}:
        return True
    id_like = os_release.get("ID_LIKE", "").strip().lower()
    return any(item in {"debian", "ubuntu"} for item in id_like.split())


def _pkg_config_check(pkg_config: str, module: str, env: dict[str, str]) -> tuple[bool, str]:
    """Return (ok, version_or_empty)."""
    try:
        ok = subprocess.run(
            [pkg_config, "--exists", module],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        return False, ""
    if not ok:
        return False, ""
    try:
        proc = subprocess.run(
            [pkg_config, "--modversion", module],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        version = (proc.stdout or "").strip() if proc.returncode == 0 else ""
    except OSError:
        version = ""
    return True, version


def _bool_from_cache_value(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "on", "true", "yes", "y"}:
            return True
        if lowered in {"0", "off", "false", "no", "n", ""}:
            return False
    return default


def _build_env_for_pkg_config(builder: Builder, build_type: str) -> dict[str, str]:
    """Approximate the builder's environment used for pkg-config resolution."""
    cfg = builder.config.global_cfg
    env = dict(os.environ)
    env.update(cfg.env)
    if builder.platform.os == "windows":
        env.update(cfg.windows_env)

    prefix = builder.prefixes.get(build_type)
    if not prefix:
        return env

    override_dir = cfg.build_root / "pkgconfig_override" / build_type
    pkg_paths = [
        str(override_dir),
        str(prefix / "lib" / "pkgconfig"),
        str(prefix / "share" / "pkgconfig"),
    ]
    existing_pkg_path = env.get("PKG_CONFIG_PATH")
    if existing_pkg_path:
        pkg_paths.extend(existing_pkg_path.split(os.pathsep))
    deduped_paths: list[str] = []
    seen: set[str] = set()
    for path_item in pkg_paths:
        if not path_item:
            continue
        normalized = os.path.normcase(os.path.normpath(path_item))
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped_paths.append(path_item)
    env["PKG_CONFIG_PATH"] = os.pathsep.join(deduped_paths)
    return env


def _tool_checks(platform: PlatformInfo, env: dict[str, str]) -> list[ToolCheck]:
    doxygen_override = _normalize_override(env.get("DOXYGEN_EXECUTABLE"))
    pkg_override = _normalize_override(env.get("PKG_CONFIG_EXECUTABLE") or env.get("PKG_CONFIG"))
    python_override = _normalize_override(
        env.get("Python3_EXECUTABLE")
        or env.get("PYTHON3_EXECUTABLE")
        or env.get("Python_EXECUTABLE")
        or env.get("PYTHON_EXECUTABLE")
    )
    python_candidates: list[str] = []
    for candidate in [python_override, sys.executable, "python3", "python"]:
        if candidate and candidate not in python_candidates:
            python_candidates.append(candidate)
    checks = [
        ToolCheck("git", ["git"], True),
        ToolCheck("cmake", ["cmake"], True),
        ToolCheck("ninja", ["ninja"], True),
        ToolCheck("pkg-config", [pkg_override] if pkg_override else ["pkg-config", "pkgconf"], True),
        ToolCheck("doxygen", [doxygen_override] if doxygen_override else ["doxygen"], True),
        ToolCheck("sphinx-build", ["sphinx-build", "sphinx-build-3"], True),
    ]
    if platform.os in {"macos", "linux"}:
        checks.append(ToolCheck("make", ["make", "gmake"], True))
    if platform.arch == "x86_64":
        checks.append(ToolCheck("nasm", ["nasm", "yasm"], True, "x86/x64 asm"))
    else:
        checks.append(ToolCheck("nasm", ["nasm", "yasm"], False, "not required on arm64"))
    checks.append(ToolCheck("python", python_candidates, True))
    checks.append(ToolCheck("uv", ["uv"], False, "optional"))
    if platform.os == "macos":
        checks.append(ToolCheck("xcrun", ["xcrun"], True))
    return checks


def run_preflight(config: Config, platform: PlatformInfo, no_update: bool) -> int:
    builder = Builder(config, platform, dry_run=True, no_update=no_update, force=False)
    env = dict(os.environ)
    env.update(config.global_cfg.env)
    if platform.os == "windows":
        env.update(config.global_cfg.windows_env)
    missing_tools = 0
    missing_repos = 0
    missing_assets = 0

    lines = ["", "=== Preflight Report ==="]
    lines.append(f"Platform: {platform.os} {platform.arch}")
    lines.append("Paths:")
    lines.append(f"  repo_root: {config.global_cfg.repo_root}")
    lines.append(f"  src_root: {config.global_cfg.src_root}")
    lines.append(f"  build_root: {config.global_cfg.build_root}")
    prefix_base = config.global_cfg.prefix_base
    if prefix_base:
        prefix_base_display = str(_expand_path(prefix_base, config.global_cfg.repo_root))
    else:
        prefix_base_display = "(default)"
    lines.append(f"  prefix_base: {prefix_base_display}")
    for key in ("Release", "Debug", "ASAN"):
        prefix = builder.prefixes.get(key)
        if prefix:
            lines.append(f"  install_prefix[{key}]: {prefix}")

    if builder.toolchain:
        lines.append("Toolchain:")
        for key in ("cc", "cxx", "ld", "ar", "ranlib"):
            value = builder.toolchain.get(key)
            if value:
                lines.append(f"  {key}: {value}")
    lines.append("Tools:")
    checks = _tool_checks(platform, env)
    if platform.os == "linux" and any(repo.build_system == "qt6" for repo in builder.repos):
        checks.append(ToolCheck("wayland-scanner", ["wayland-scanner"], True, "qtwayland"))
    for check in checks:
        path, resolved = _find_any(check.candidates)
        if path:
            note = f" ({resolved})" if resolved and resolved != check.name and resolved != path else ""
            lines.append(f"  {check.name}: ok ({path}){note}")
            continue
        if check.required:
            missing_tools += 1
            extra = f" [{check.note}]" if check.note else ""
            lines.append(f"  {check.name}: missing{extra}")
        else:
            extra = f" [{check.note}]" if check.note else ""
            lines.append(f"  {check.name}: not found{extra}")

    if platform.os == "windows" and builder._ffmpeg_enabled():
        lines.append("FFmpeg (Windows mode):")
        if builder._windows_ffmpeg_native_build_enabled():
            lines.append("  source build: enabled (MSYS2 environment detected)")
            bash_path, _ = _find_any(["bash", "bash.exe"])
            make_path, make_name = _find_any(["make", "mingw32-make"])
            if bash_path:
                lines.append(f"  bash: ok ({bash_path})")
            else:
                missing_tools += 1
                lines.append("  bash: missing [required for FFmpeg source build]")
            if make_path:
                lines.append(f"  make: ok ({make_path}) ({make_name})")
            else:
                missing_tools += 1
                lines.append("  make: missing [required for FFmpeg source build]")
        else:
            lines.append("  source build: disabled (MSYS2 environment not detected)")
            lines.append("  fallback: expects prebuilt FFmpeg in the install prefix")

    # Qt6 system dependency checks (Linux)
    qt6_enabled = any(repo.build_system == "qt6" for repo in builder.repos)
    if platform.os == "linux" and qt6_enabled:
        os_release = _read_os_release()
        pkg_override = _normalize_override(env.get("PKG_CONFIG_EXECUTABLE") or env.get("PKG_CONFIG"))
        pkg_candidates = [pkg_override] if pkg_override else ["pkg-config", "pkgconf"]
        pkg_path, _ = _find_any(pkg_candidates)
        build_env = _build_env_for_pkg_config(builder, "Release")

        lines.append("Qt6 (Linux prerequisites):")
        if not pkg_path:
            lines.append("  pkg-config: missing (required for Qt6 dependency detection)")
        else:
            xcb_modules = [
                "x11",
                "x11-xcb",
                "xcb",
                "xcb-cursor",
                "xcb-icccm",
                "xcb-image",
                "xcb-keysyms",
                "xcb-randr",
                "xcb-render",
                "xcb-renderutil",
                "xcb-shape",
                "xcb-shm",
                "xcb-sync",
                "xcb-xfixes",
                "xcb-xkb",
                "xcb-xinerama",
                "xkbcommon",
                "xkbcommon-x11",
            ]
            wayland_modules = [
                "wayland-client",
                "wayland-cursor",
                "wayland-egl",
                "wayland-server",
                "wayland-protocols",
            ]
            desktop_modules = [
                "glib-2.0",
                "gthread-2.0",
                "fontconfig",
            ]
            required_modules: list[str] = []
            required_modules.extend(xcb_modules)
            required_modules.extend(wayland_modules)
            required_modules.extend(desktop_modules)
            # QtMultimedia FFmpeg backend on Linux requires PulseAudio.
            if config.global_cfg.build_ffmpeg:
                required_modules.append("libpulse")

            missing: list[str] = []
            for module in required_modules:
                ok, _ver = _pkg_config_check(pkg_path, module, build_env)
                if not ok:
                    missing.append(module)

            def _group_missing(group: list[str]) -> list[str]:
                return [m for m in group if m in missing]

            xcb_missing = _group_missing(xcb_modules)
            wayland_missing = _group_missing(wayland_modules)
            desktop_missing = _group_missing(desktop_modules)
            pulse_missing = ["libpulse"] if "libpulse" in missing else []

            if not xcb_missing:
                lines.append("  xcb: ok")
            else:
                lines.append(f"  xcb: missing ({', '.join(xcb_missing)})")

            if not wayland_missing:
                lines.append("  wayland: ok")
            else:
                lines.append(f"  wayland: missing ({', '.join(wayland_missing)})")

            if config.global_cfg.build_ffmpeg:
                if not pulse_missing:
                    lines.append("  pulseaudio (QtMultimedia ffmpeg): ok")
                else:
                    lines.append("  pulseaudio (QtMultimedia ffmpeg): missing (libpulse)")

            if not desktop_missing:
                lines.append("  glib/fontconfig: ok")
            else:
                lines.append(f"  glib/fontconfig: missing ({', '.join(desktop_missing)})")

            if missing:
                lines.append("  install hints:")
                if _is_debian_like(os_release):
                    apt_packages: list[str] = []
                    if xcb_missing:
                        apt_packages += [
                            "libx11-dev",
                            "libx11-xcb-dev",
                            "libxcb1-dev",
                            "libxcb-cursor-dev",
                            "libxcb-icccm4-dev",
                            "libxcb-image0-dev",
                            "libxcb-keysyms1-dev",
                            "libxcb-randr0-dev",
                            "libxcb-render0-dev",
                            "libxcb-render-util0-dev",
                            "libxcb-shape0-dev",
                            "libxcb-shm0-dev",
                            "libxcb-sync-dev",
                            "libxcb-xfixes0-dev",
                            "libxcb-xkb-dev",
                            "libxcb-xinerama0-dev",
                            "libxkbcommon-dev",
                            "libxkbcommon-x11-dev",
                        ]
                    if wayland_missing:
                        apt_packages += [
                            "libwayland-dev",
                            "wayland-protocols",
                            "libegl1-mesa-dev",
                            "libgl1-mesa-dev",
                        ]
                    if pulse_missing:
                        apt_packages += ["libpulse-dev"]
                    if desktop_missing:
                        apt_packages += ["libglib2.0-dev", "libfontconfig1-dev"]
                    apt_packages = sorted(set(apt_packages))
                    if apt_packages:
                        lines.append(f"    Debian/Ubuntu: sudo apt-get install {' '.join(apt_packages)}")
                lines.append("    Install the development packages that provide the missing pkg-config modules above.")

    # OpenImageIO static linkage can pull FFmpeg transitive system libs on Linux.
    # In particular, FFmpeg builds with VDPAU support require -lvdpau at link time.
    if (
        platform.os == "linux"
        and config.global_cfg.build_ffmpeg
        and any(repo.name == "OpenImageIO" for repo in builder.repos)
    ):
        lines.append("FFmpeg/OpenImageIO (Linux transitive system libs):")
        pkg_override = _normalize_override(env.get("PKG_CONFIG_EXECUTABLE") or env.get("PKG_CONFIG"))
        pkg_candidates = [pkg_override] if pkg_override else ["pkg-config", "pkgconf"]
        pkg_path, _ = _find_any(pkg_candidates)
        build_env = _build_env_for_pkg_config(builder, "Release")

        if not pkg_path:
            lines.append("  pkg-config: missing (required to resolve vdpau)")
        else:
            vdpau_ok, vdpau_version = _pkg_config_check(pkg_path, "vdpau", build_env)
            if vdpau_ok:
                version_note = f" ({vdpau_version})" if vdpau_version else ""
                lines.append(f"  vdpau: ok{version_note}")
            else:
                lines.append("  vdpau: missing (ld.lld would fail with `-lvdpau`)")
                os_release = _read_os_release()
                if _is_debian_like(os_release):
                    lines.append("  install hint (Debian/Ubuntu): sudo apt-get install libvdpau-dev")
                lines.append("  used for FFmpeg VDPAU hardware-acceleration support pulled into static OIIO link.")

    # nativefiledialog-extended Linux dependency checks
    if platform.os == "linux" and any(repo.name == "nativefiledialog-extended" for repo in builder.repos):
        lines.append("nativefiledialog-extended (Linux prerequisites):")
        nfd_options = builder._repo_cmake_effective_toml_options("nativefiledialog-extended")
        portal_enabled = _bool_from_cache_value(nfd_options.cache.get("NFD_PORTAL"), default=False)
        if portal_enabled:
            lines.append("  backend: portal (NFD_PORTAL=ON), gtk+-3.0 check skipped")
        else:
            lines.append("  backend: gtk3 (NFD_PORTAL=OFF)")
            pkg_override = _normalize_override(env.get("PKG_CONFIG_EXECUTABLE") or env.get("PKG_CONFIG"))
            pkg_candidates = [pkg_override] if pkg_override else ["pkg-config", "pkgconf"]
            pkg_path, _ = _find_any(pkg_candidates)
            build_env = _build_env_for_pkg_config(builder, "Release")
            if not pkg_path:
                lines.append("  pkg-config: missing (required to resolve gtk+-3.0)")
            else:
                gtk_ok, gtk_version = _pkg_config_check(pkg_path, "gtk+-3.0", build_env)
                if gtk_ok:
                    version_note = f" ({gtk_version})" if gtk_version else ""
                    lines.append(f"  gtk+-3.0: ok{version_note}")
                else:
                    lines.append("  gtk+-3.0: missing")
                    os_release = _read_os_release()
                    if _is_debian_like(os_release):
                        lines.append("  install hint (Debian/Ubuntu): sudo apt-get install pkg-config libgtk-3-dev")
                    lines.append("  install GTK3 development packages so pkg-config can resolve `gtk+-3.0`.")

    # Adobe DNG SDK (optional) - we do not vendor sources; users must provide the archive/dir.
    if getattr(config.global_cfg, "build_dng_sdk", False):
        lines.append("DNG SDK (optional prerequisites):")
        dng_path = _resolve_dngsdk_archive_path(env, config.global_cfg.repo_root)
        if not dng_path:
            missing_assets += 1
            lines.append("  archive: missing (required when build_dng_sdk=true)")
            lines.append("  hint: place it under `external/` (e.g. `external/dng_sdk_1_7_1_0.zip`) or set `DNGSDK_ARCHIVE`.")
        elif not dng_path.exists():
            missing_assets += 1
            lines.append(f"  archive: missing (configured at {dng_path})")
        else:
            kind = "dir" if dng_path.is_dir() else "archive"
            lines.append(f"  archive: ok ({kind}: {dng_path})")

    lines.append("Repos:")
    for repo in builder.repos:
        url = repo.url or _PREFLIGHT_REPO_URLS.get(repo.name, "")
        if repo.name == "libiconv" and platform.os == "windows":
            zip_path = builder._libiconv_export_zip()
            if zip_path.exists():
                lines.append(f"  {repo.name}: ok (vcpkg export zip: {zip_path})")
            else:
                lines.append(f"  {repo.name}: missing (vcpkg export zip expected at {zip_path})")
                if not repo.optional:
                    missing_repos += 1
            continue
        if repo.name == "openssl" and platform.os == "windows":
            zip_path = builder._openssl_export_zip()
            if zip_path.exists():
                lines.append(f"  {repo.name}: ok (vcpkg export zip: {zip_path})")
            else:
                lines.append(f"  {repo.name}: missing (vcpkg export zip expected at {zip_path})")
                if not repo.optional:
                    missing_repos += 1
            continue
        if repo.name == "sqlite" and platform.os == "windows":
            zip_path = builder._sqlite_export_zip()
            if zip_path.exists():
                lines.append(f"  {repo.name}: ok (vcpkg export zip: {zip_path})")
            else:
                lines.append(f"  {repo.name}: missing (vcpkg export zip expected at {zip_path})")
                if not repo.optional:
                    missing_repos += 1
            continue
        if repo.name == "libffi" and platform.os == "windows":
            zip_path = builder._libffi_export_zip()
            if zip_path.exists():
                lines.append(f"  {repo.name}: ok (vcpkg export zip: {zip_path})")
            else:
                lines.append(f"  {repo.name}: missing (vcpkg export zip expected at {zip_path})")
                if not repo.optional:
                    missing_repos += 1
            continue
        path = builder._resolve_repo_dir(repo)
        if path.exists():
            if url:
                lines.append(f"  {repo.name}: ok ({path}, url={url})")
            else:
                lines.append(f"  {repo.name}: ok ({path})")
            continue
        if repo.optional and not repo.url:
            if url:
                lines.append(f"  {repo.name}: missing (optional, url={url})")
            else:
                lines.append(f"  {repo.name}: missing (optional)")
        else:
            missing_repos += 1
            lines.append(f"  {repo.name}: missing (expected at {path}, url={url or 'no-url'})")

    lines.append("Summary:")
    lines.append(f"  missing tools: {missing_tools}")
    lines.append(f"  missing repos: {missing_repos}")
    lines.append(f"  missing assets: {missing_assets}")
    lines.append("Example build:")
    lines.append("  uv run build.py --build-types Debug,Release")
    print("\n".join(lines))
    return 0
