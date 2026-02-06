from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import shutil
import subprocess

from .config import Config, RepoConfig
from .git_ops import ensure_repo, git_head
from .platform import PlatformInfo
from .recipes import registry as recipe_registry
from .runner import banner, print_cmd, run
from .stamps import compute_stamp, read_stamp, write_stamp
from .topo import topo_sort


@dataclass
class BuildContext:
    repo: RepoConfig
    build_type: str
    build_dir: Path
    install_prefix: Path
    src_dir: Path


class BuildReport:
    def __init__(self, build_types: list[str], order: list[str], prefixes: dict[str, Path]) -> None:
        self.build_types = build_types
        self.order = order
        self.prefixes = prefixes
        self.entries: dict[tuple[str, str], tuple[str, str]] = {}

    def record(self, build_type: str, repo: str, status: str, detail: str = "") -> None:
        self.entries[(build_type, repo)] = (status, detail)

    def render(self) -> str:
        lines = ["", "=== Build Report ==="]
        for build_type in self.build_types:
            lines.append(f"{build_type}:")
            for repo in self.order:
                entry = self.entries.get((build_type, repo))
                if not entry:
                    continue
                status, detail = entry
                suffix = f" ({detail})" if detail else ""
                lines.append(f"  {repo}: {status}{suffix}")
            prefix = self.prefixes.get(build_type)
            if prefix:
                lines.append(f"  install_prefix: {prefix}")
        return "\n".join(lines)

    def print(self) -> None:
        print(self.render())


class Builder:
    def __init__(self, config: Config, platform: PlatformInfo, dry_run: bool, no_update: bool, force: bool) -> None:
        self.config = config
        self.platform = platform
        self.dry_run = dry_run
        self.no_update = no_update
        self.force = force
        self.toolchain = self._resolve_toolchain()
        self.repos = self._filter_repos()
        self.prefixes = self._compute_prefixes()
        self.repo_paths: dict[str, Path] = {}
        self.pkg_override_root = self.config.global_cfg.build_root / "pkgconfig_override"
        self._ocio_python_note_printed = False
        self._openexr_python_note_printed = False
        self._windows_python_wrappers_forced_on_note_printed = False

    def _filter_repos(self) -> list[RepoConfig]:
        cfg = self.config.global_cfg
        repos = [r for r in self.config.repos if r.enabled]

        # Apply group toggles to approximate the shell script behavior.
        gl_repos = {"glfw", "freeglut", "glew"}
        imageio_repos = {
            "libjpeg-turbo",
            "libpng",
            "libtiff",
            "openjpeg",
            "jasper",
            "giflib",
            "pugixml",
            "libwebp",
            "ptex",
            "libraw",
            "LibRaw",
            "aom",
            "libde265",
            "x265",
            "kvazaar",
            "libheif",
            "pybind11",
            "ffmpeg",
            "OpenImageIO",
        }
        exr_repos = {"imath", "openjph", "openexr"}
        ocio_repos = {"minizip-ng", "OpenColorIO"}

        def enabled(repo: RepoConfig) -> bool:
            if repo.name == "ffmpeg" and self.platform.os == "windows":
                if cfg.build_ffmpeg:
                    print("[skip] ffmpeg: build is not supported on Windows (set build_ffmpeg=false in build.toml)")
                return False
            if repo.name == "libiconv" and self.platform.os != "windows":
                return False
            if repo.name in gl_repos and not cfg.build_gl_stack:
                return False
            if repo.name in imageio_repos and not cfg.build_imageio_stack:
                return False
            if repo.name in exr_repos and not cfg.build_exr_stack:
                return False
            if repo.name == "googletest" and not cfg.build_gtest:
                return False
            if repo.name == "libjxl" and not cfg.build_libjxl:
                return False
            if repo.name == "libultrahdr" and not cfg.build_libuhdr:
                return False
            if repo.name in ocio_repos and not cfg.build_ocio:
                return False
            if repo.name == "libraw" and not cfg.build_libraw:
                return False
            if repo.name == "libheif" and not cfg.build_libheif:
                return False
            if repo.name == "aom" and not cfg.build_aom:
                return False
            if repo.name == "libde265" and not cfg.build_libde265:
                return False
            if repo.name == "x265" and not cfg.build_x265:
                return False
            if repo.name == "kvazaar" and not cfg.build_kvazaar:
                return False
            if repo.name == "libwebp" and not cfg.build_webp:
                return False
            if repo.name == "ptex" and not cfg.build_ptex:
                return False
            if repo.name == "pybind11" and not cfg.build_pybind11:
                return False
            if repo.name == "ffmpeg" and not cfg.build_ffmpeg:
                return False
            if repo.name == "OpenImageIO" and not cfg.build_oiio:
                return False
            return True

        repos = [r for r in repos if enabled(r)]

        if self.config.only:
            selected = set(self.config.only)
            by_name = {repo.name: repo for repo in repos}
            pending = list(selected)
            while pending:
                current = pending.pop()
                repo = by_name.get(current)
                if not repo:
                    continue
                for dep in repo.deps:
                    if dep not in selected and dep in by_name:
                        selected.add(dep)
                        pending.append(dep)
            repos = [r for r in repos if r.name in selected]
        if self.config.skip:
            repos = [r for r in repos if r.name not in self.config.skip]
        return repos

    def _compute_prefixes(self) -> dict[str, Path]:
        cfg = self.config.global_cfg
        prefixes: dict[str, Path] = {}
        if self.platform.os == "windows":
            win_cfg = cfg.windows
            base = win_cfg.get("install_prefix") or cfg.prefix_base
            if not base:
                base = str(cfg.repo_root / "_install" / "WIN")
            base = os.path.expanduser(os.path.expandvars(str(base)))
            base_path = Path(base)
            if not base_path.is_absolute():
                base_path = (cfg.repo_root / base_path).resolve()
            prefixes["Release"] = base_path
            prefixes["Debug"] = base_path
            if "ASAN" in cfg.build_types:
                asan_base = win_cfg.get("asan_prefix")
                if not asan_base:
                    asan_base = f"{base}_ASAN"
                asan_base = os.path.expanduser(os.path.expandvars(str(asan_base)))
                asan_path = Path(asan_base)
                if not asan_path.is_absolute():
                    asan_path = (cfg.repo_root / asan_path).resolve()
                prefixes["ASAN"] = asan_path
            return prefixes

        base = cfg.prefix_base
        if not base:
            base = str(cfg.repo_root / "_install" / "UBS")
        base = os.path.expanduser(os.path.expandvars(base))
        base_path = Path(base)
        if not base_path.is_absolute():
            base_path = (cfg.repo_root / base_path).resolve()
        prefixes["Release"] = base_path
        prefixes["Debug"] = Path(f"{base_path}{cfg.debug_suffix}")
        prefixes["ASAN"] = Path(f"{base_path}{cfg.asan_suffix}")
        return prefixes

    def _build_type_order(self) -> list[str]:
        types = [t for t in self.config.build_types if t in {"Debug", "Release", "ASAN"}]
        if self.platform.os == "windows":
            order = [t for t in ["Debug", "Release", "ASAN"] if t in types]
            return order
        return types

    def _toolchain_fingerprint(self) -> str:
        cfg = self.config.global_cfg
        parts = [
            self.platform.os,
            self.platform.arch,
            f"cxx{cfg.cxx_standard}",
            f"ext{int(cfg.cxx_extensions)}",
            f"libcxx{int(cfg.use_libcxx)}",
            f"lld{int(cfg.use_lld)}",
            f"static{int(cfg.static_default)}",
        ]
        if self.platform.os == "windows":
            generator = str(cfg.windows.get("generator", ""))
            parts.append(f"gen:{generator}")
        return ";".join(parts)

    def _which(self, name: str) -> str | None:
        for path in os.environ.get("PATH", "").split(os.pathsep):
            candidate = Path(path) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def _xcrun_find(self, name: str) -> str | None:
        try:
            out = subprocess.check_output(["xcrun", "--find", name], text=True).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        return out or None

    def _xcrun_sdk_path(self) -> str | None:
        try:
            out = subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
        return out or None

    def _resolve_toolchain(self) -> dict[str, str]:
        cfg = self.config.global_cfg
        toolchain: dict[str, str] = {}
        if self.platform.os == "windows":
            return toolchain

        if cfg.cc:
            toolchain["cc"] = cfg.cc
        if cfg.cxx:
            toolchain["cxx"] = cfg.cxx
        if cfg.ld:
            toolchain["ld"] = cfg.ld
        if cfg.ar:
            toolchain["ar"] = cfg.ar
        if cfg.ranlib:
            toolchain["ranlib"] = cfg.ranlib

        if self.platform.os == "macos":
            toolchain.setdefault("cc", self._xcrun_find("clang") or self._which("clang") or "clang")
            toolchain.setdefault("cxx", self._xcrun_find("clang++") or self._which("clang++") or "clang++")
            toolchain.setdefault("ld", self._xcrun_find("ld") or self._which("ld") or "ld")
            toolchain.setdefault("ar", self._xcrun_find("ar") or self._which("ar") or "ar")
            toolchain.setdefault("ranlib", self._xcrun_find("ranlib") or self._which("ranlib") or "ranlib")
            sdk = self._xcrun_sdk_path()
            if sdk:
                toolchain.setdefault("sdkroot", sdk)
        else:
            toolchain.setdefault("cc", self._which("clang-20") or self._which("clang") or "clang")
            toolchain.setdefault("cxx", self._which("clang++-20") or self._which("clang++") or "clang++")
            toolchain.setdefault("ld", self._which("ld.lld-20") or self._which("ld.lld") or "ld")
            toolchain.setdefault("ar", self._which("llvm-ar-20") or self._which("llvm-ar") or self._which("ar") or "ar")
            toolchain.setdefault(
                "ranlib", self._which("llvm-ranlib-20") or self._which("llvm-ranlib") or self._which("ranlib") or "ranlib"
            )
        return toolchain

    def _env_for_build(self, build_type: str, prefix: Path) -> dict[str, str]:
        env = dict(self.config.global_cfg.env)
        if self.platform.os == "windows":
            env.update(self.config.global_cfg.windows_env)
        if self.platform.os == "macos":
            sdkroot = self.toolchain.get("sdkroot")
            if sdkroot and not env.get("SDKROOT"):
                env["SDKROOT"] = sdkroot
        override_dir = self.pkg_override_root / build_type
        pkg_paths = [
            str(override_dir),
            str(prefix / "lib" / "pkgconfig"),
            str(prefix / "share" / "pkgconfig"),
        ]
        if env.get("PKG_CONFIG_PATH"):
            pkg_paths.append(env["PKG_CONFIG_PATH"])
        env["PKG_CONFIG_PATH"] = ":".join([p for p in pkg_paths if p])
        return env

    def _windows_runtime_mode(self) -> str:
        mode = str(self.config.global_cfg.windows.get("msvc_runtime", "static")).strip().lower()
        if mode in {"", "static", "mt", "multithreaded"}:
            return "static"
        if mode in {"dynamic", "md", "multithreadeddll"}:
            return "dynamic"
        return mode

    def _windows_python_wrappers_mode(self) -> str:
        mode = str(self.config.global_cfg.windows.get("python_wrappers", "auto")).strip().lower()
        if mode in {"on", "off", "auto"}:
            return mode
        return "auto"

    def _windows_python_wrappers_enabled(self) -> tuple[bool, str]:
        if self.platform.os != "windows":
            return True, "non-windows"
        mode = self._windows_python_wrappers_mode()
        if mode == "on":
            if self._windows_runtime_mode() == "static" and not self._windows_python_wrappers_forced_on_note_printed:
                print(
                    "[note] windows.python_wrappers=on with static CRT may still fail for some projects. "
                    "If wrappers fail, use windows.msvc_runtime=dynamic.",
                    flush=True,
                )
                self._windows_python_wrappers_forced_on_note_printed = True
            return True, "forced-on"
        if mode == "off":
            return False, "forced-off"
        return self._windows_runtime_mode() == "dynamic", "auto"

    def _base_flags(self, build_type: str) -> str:
        cfg = self.config.global_cfg
        if self.platform.os == "windows":
            runtime_mode = self._windows_runtime_mode()
            runtime_flag = ""
            if runtime_mode == "static":
                runtime_flag = "/MTd" if build_type == "Debug" else "/MT"
            elif runtime_mode == "dynamic":
                runtime_flag = "/MDd" if build_type == "Debug" else "/MD"
            if build_type == "Debug":
                return f"/Od /Zi {runtime_flag}".strip()
            return f"/O2 /DNDEBUG {runtime_flag}".strip()
        if build_type == "Debug":
            flags = "-O0 -g"
        else:
            flags = "-O3 -DNDEBUG"
        if cfg.pic:
            flags += " -fPIC"
        return flags

    def _macos_sysroot_flag(self) -> str:
        if self.platform.os != "macos":
            return ""
        sdkroot = self.toolchain.get("sdkroot")
        if not sdkroot:
            return ""
        return f" -isysroot {sdkroot}"

    def _non_cmake_flags(self, build_type: str) -> tuple[str, str, str]:
        cfg = self.config.global_cfg
        cflags = self._base_flags(build_type)
        cxxflags = self._base_flags(build_type)
        if self.platform.os == "windows":
            if build_type == "ASAN":
                cflags += " /fsanitize=address"
                cxxflags += " /fsanitize=address"
            return cflags, cxxflags, ""
        if self.platform.os in {"macos", "linux"} and cfg.use_libcxx:
            cxxflags += " -stdlib=libc++"
        if build_type == "ASAN":
            cflags += " -fsanitize=address -fno-omit-frame-pointer"
            cxxflags += " -fsanitize=address -fno-omit-frame-pointer"
        sysroot_flag = self._macos_sysroot_flag()
        if sysroot_flag:
            cflags += sysroot_flag
            cxxflags += sysroot_flag
        ldflags = sysroot_flag
        if self.platform.os in {"macos", "linux"} and cfg.use_libcxx:
            ldflags += " -stdlib=libc++"
        return cflags, cxxflags, ldflags

    def _linker_flags_init(self) -> str:
        cfg = self.config.global_cfg
        if self.platform.os in {"macos", "windows"}:
            return ""
        return "-fuse-ld=lld" if cfg.use_lld else ""

    def _resolve_openjpeg_build_codec(self) -> str:
        cfg = self.config.global_cfg
        if cfg.openjpeg_build_codec:
            return str(cfg.openjpeg_build_codec)
        return "OFF" if self.platform.os == "macos" else "ON"

    def _expand_args(self, args: list[str], build_type: str, prefix: Path) -> list[str]:
        cfg = self.config.global_cfg
        mapping = {
            "SRC_ROOT": str(cfg.src_root),
            "BUILD_TYPE": build_type,
            "PREFIX": str(prefix),
            "LIBRAW_ENABLE_EXAMPLES": cfg.libraw_enable_examples,
            "LIBRAW_ENABLE_OPENMP": cfg.libraw_enable_openmp,
            "LIBJXL_ENABLE_TOOLS": cfg.libjxl_enable_tools,
            "OPENJPEG_BUILD_CODEC": self._resolve_openjpeg_build_codec(),
            "OCIO_BUILD_APPS": cfg.ocio_build_apps,
        }
        expanded: list[str] = []
        for arg in args:
            out = arg
            for key, value in mapping.items():
                out = out.replace(f"${{{key}}}", str(value))
            expanded.append(out)
        return expanded

    def _repo_specific_args(self, repo: RepoConfig, ctx: BuildContext) -> list[str]:
        cfg = self.config.global_cfg
        name = repo.name
        args: list[str] = []
        recipe_args = recipe_registry.cmake_args(name, self, ctx)
        recipe_applied = recipe_args is not None
        if recipe_applied:
            args.extend(recipe_args)

        if name == "zlib-ng":
            args += [
                "-DZLIB_COMPAT=ON",
                "-DWITH_GTEST=OFF",
                "-DWITH_FUZZERS=OFF",
                "-DWITH_BENCHMARKS=OFF",
                "-DWITH_BENCHMARK_APPS=OFF",
            ]
        elif name == "xz":
            args += ["-DBUILD_SHARED_LIBS=OFF"]
        elif name == "libdeflate":
            args += [
                "-DLIBDEFLATE_BUILD_STATIC_LIB=ON",
                "-DLIBDEFLATE_BUILD_SHARED_LIB=OFF",
                "-DLIBDEFLATE_BUILD_TESTS=OFF",
                "-DLIBDEFLATE_BUILD_GZIP=ON",
            ]
        elif name == "zstd":
            args += [
                "-DZSTD_BUILD_PROGRAMS=ON",
                "-DZSTD_BUILD_TESTS=OFF",
                "-DZSTD_BUILD_SHARED=OFF",
                "-DZSTD_BUILD_STATIC=ON",
            ]
        elif name == "libxml2":
            args += [
                "-DLIBXML2_WITH_LZMA=ON",
                "-DLIBXML2_WITH_PYTHON=OFF",
                "-DLIBXML2_WITH_TESTS=OFF",
                "-DLIBXML2_WITH_PROGRAMS=OFF",
            ]
            if self.platform.os == "windows":
                if not any(a.startswith("-DLIBXML2_WITH_ICONV=") for a in repo.cmake_args):
                    debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
                    iconv_header = ctx.install_prefix / "include" / "iconv.h"
                    iconv_cfg = ctx.install_prefix / "lib" / "cmake" / "Iconv" / "IconvConfig.cmake"
                    if ctx.build_type == "Debug":
                        iconv_lib = ctx.install_prefix / "lib" / f"iconv{debug_postfix}.lib"
                    else:
                        iconv_lib = ctx.install_prefix / "lib" / "iconv.lib"

                    if iconv_cfg.exists() or (iconv_header.exists() and iconv_lib.exists()):
                        args.append("-DLIBXML2_WITH_ICONV=ON")
                    else:
                        args.append("-DLIBXML2_WITH_ICONV=OFF")
        elif name == "glfw":
            args += [
                "-DGLFW_BUILD_EXAMPLES=ON",
                "-DGLFW_BUILD_TESTS=OFF",
                "-DGLFW_BUILD_DOCS=OFF",
            ]
        elif name == "freeglut":
            args += [
                "-DFREEGLUT_BUILD_STATIC_LIBS=ON",
                "-DFREEGLUT_BUILD_SHARED_LIBS=OFF",
                "-DFREEGLUT_BUILD_DEMOS=ON",
            ]
        elif name == "glew":
            if self.platform.os == "macos":
                args += [
                    "-Dglew-cmake_BUILD_SHARED=OFF",
                    "-Dglew-cmake_BUILD_STATIC=ON",
                    "-DONLY_LIBS=ON",
                ]
            else:
                args += ["-DBUILD_UTILS=ON"]
        elif name == "libjpeg-turbo":
            args += [
                "-DENABLE_SHARED=OFF",
                "-DENABLE_STATIC=ON",
                "-DWITH_JPEG7=ON",
                "-DWITH_JPEG8=ON",
                "-DREQUIRE_SIMD=ON",
            ]
        elif name == "libpng":
            args += ["-DPNG_SHARED=OFF", "-DPNG_STATIC=ON", "-DPNG_TESTS=OFF"]
        elif name == "libtiff" and not recipe_applied:
            args += [
                "-Dtiff-tests=OFF",
                "-Dtiff-tools=ON",
                "-Dtiff-docs=OFF",
                "-Dtiff-contrib=OFF",
                "-Dwebp=OFF",
                "-DJPEG_SUPPORT=ON",
                "-DJPEG_DUAL_MODE_8_12=ON",
            ]
            if self.platform.os == "windows":
                # libtiff's Findliblzma module doesn't propagate this define for static linking.
                args.append("-Dtiff-opengl=ON")
                args.append("-DCMAKE_C_FLAGS=/DLZMA_API_STATIC")
                args.append("-DCMAKE_C_FLAGS_DEBUG=/DFREEGLUT_STATIC")
                args.append("-DCMAKE_C_FLAGS_RELEASE=/DFREEGLUT_STATIC")
            else:
                args.append("-Dtiff-opengl=OFF")
        elif name == "openjpeg" and not recipe_applied:
            args += [f"-DBUILD_CODEC={self._resolve_openjpeg_build_codec()}"]
            if self.platform.os == "windows":
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
            if self.platform.os == "macos" and self._resolve_openjpeg_build_codec() == "ON":
                args += [
                    f"-DCMAKE_EXE_LINKER_FLAGS_INIT=-L{ctx.install_prefix / 'lib'}",
                    f"-DCMAKE_SHARED_LINKER_FLAGS_INIT=-L{ctx.install_prefix / 'lib'}",
                    f"-DCMAKE_MODULE_LINKER_FLAGS_INIT=-L{ctx.install_prefix / 'lib'}",
                ]
        elif name == "jasper":
            args += [
                "-DBUILD_TESTING=OFF",
                "-DJAS_ENABLE_PROGRAMS=OFF",
                "-DJAS_ENABLE_LIBJPEG=ON",
                "-DJAS_ENABLE_SHARED=OFF",
                "-DALLOW_IN_SOURCE_BUILD=ON",
            ]
        elif name == "pugixml":
            args += ["-DBUILD_TESTING=OFF"]
        elif name == "libwebp":
            args += [
                "-DWEBP_BUILD_ANIM_UTILS=OFF",
                "-DWEBP_BUILD_CWEBP=OFF",
                "-DWEBP_BUILD_DWEBP=OFF",
                "-DWEBP_BUILD_GIF2WEBP=OFF",
                "-DWEBP_BUILD_IMG2WEBP=OFF",
                "-DWEBP_BUILD_VWEBP=OFF",
                "-DWEBP_BUILD_WEBPINFO=OFF",
                "-DWEBP_BUILD_WEBPMUX=OFF",
                "-DWEBP_BUILD_EXTRAS=OFF",
                "-DWEBP_BUILD_FUZZTEST=OFF",
                "-DWEBP_BUILD_LIBWEBPMUX=ON",
            ]
        elif name == "ptex":
            args += [
                "-DPTEX_BUILD_STATIC_LIBS=ON",
                "-DPTEX_BUILD_SHARED_LIBS=OFF",
                "-DPTEX_BUILD_DOCS=OFF",
            ]
        elif name == "libraw":
            libraw_path = str(self.config.global_cfg.src_root / "LibRaw")
            args += [
                f"-DLIBRAW_PATH={libraw_path}",
                f"-DENABLE_EXAMPLES={cfg.libraw_enable_examples}",
                "-DENABLE_RAWSPEED=OFF",
                f"-DENABLE_OPENMP={cfg.libraw_enable_openmp}",
                "-DENABLE_LCMS=ON",
                "-DENABLE_JASPER=ON",
            ]
        elif name == "aom":
            args += [
                "-DENABLE_TESTS=OFF",
                "-DENABLE_EXAMPLES=OFF",
                "-DENABLE_TOOLS=OFF",
                "-DENABLE_DOCS=OFF",
                "-DENABLE_SHARED=OFF",
            ]
        elif name == "libde265":
            args += [
                "-DENABLE_SDL=OFF",
                "-DENABLE_DECODER=ON",
                "-DENABLE_ENCODER=OFF",
            ]
        elif name == "x265":
            args += [
                "-DENABLE_SHARED=OFF",
                "-DENABLE_CLI=OFF",
                "-DENABLE_TESTS=OFF",
            ]
        elif name == "kvazaar":
            args += [
                "-DBUILD_SHARED_LIBS=OFF",
                "-DBUILD_TESTS=OFF",
            ]
        elif name == "libheif":
            args += [
                "-DENABLE_PLUGIN_LOADING=OFF",
                "-DWITH_LIBDE265=ON",
                "-DWITH_LIBDE265_PLUGIN=OFF",
                "-DWITH_X265=ON",
                "-DWITH_X265_PLUGIN=OFF",
                "-DWITH_KVAZAAR=ON",
                "-DWITH_KVAZAAR_PLUGIN=OFF",
                "-DWITH_AOM_DECODER=ON",
                "-DWITH_AOM_DECODER_PLUGIN=OFF",
                "-DWITH_AOM_ENCODER=ON",
                "-DWITH_AOM_ENCODER_PLUGIN=OFF",
                "-DWITH_DAV1D=OFF",
                "-DWITH_RAV1E=OFF",
            ]
            if self.platform.os == "windows":
                debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
                aom_include_dir = ctx.install_prefix / "include"
                release_aom_lib = ctx.install_prefix / "lib" / "aom.lib"
                debug_aom_lib = ctx.install_prefix / "lib" / f"aom{debug_postfix}.lib"
                aom_lib = debug_aom_lib if ctx.build_type == "Debug" else release_aom_lib
                if not aom_lib.exists():
                    candidates = sorted((ctx.install_prefix / "lib").glob("aom*.lib"))
                    if candidates:
                        aom_lib = candidates[0]
                args += [
                    f"-DAOM_INCLUDE_DIR={aom_include_dir}",
                    f"-DAOM_LIBRARY={aom_lib}",
                ]
        elif name == "brotli":
            args += ["-DBROTLI_DISABLE_TESTS=ON", "-DBROTLI_BUILD_TOOLS=OFF"]
        elif name == "highway":
            args += [
                "-DHWY_ENABLE_TESTS=OFF",
                "-DHWY_ENABLE_EXAMPLES=OFF",
                "-DHWY_ENABLE_CONTRIB=ON",
                "-DHWY_FORCE_STATIC_LIBS=ON",
                "-DHWY_SYSTEM_GTEST=ON",
                "-DHWY_ENABLE_INSTALL=ON",
            ]
        elif name == "lcms2" and not recipe_applied:
            args += [
                "-DBUILD_TESTING=OFF",
                "-DBUILD_TESTS=OFF",
                "-DLCMS2_WITH_TIFF=OFF",
                "-DLCMS2_BUILD_TIFICC=OFF",
            ]
        elif name == "imath":
            args += [
                "-DIMATH_BUILD_TESTS=OFF",
                "-DIMATH_BUILD_SHARED_LIBS=OFF",
                "-DPYTHON=OFF",
            ]
        elif name == "openjph":
            args += [
                "-DOJPH_ENABLE_TIFF_SUPPORT=ON",
                "-DOJPH_BUILD_STREAM_EXPAND=ON",
                "-DBUILD_TESTING=OFF",
            ]
        elif name == "openexr":
            openexr_build_python = "ON"
            if self.platform.os == "windows":
                wrappers_enabled, reason = self._windows_python_wrappers_enabled()
                openexr_build_python = "ON" if wrappers_enabled else "OFF"
                if openexr_build_python == "OFF" and not self._openexr_python_note_printed:
                    if reason == "forced-off":
                        print("[note] OpenEXR: OPENEXR_BUILD_PYTHON=OFF (windows.python_wrappers=off)", flush=True)
                    else:
                        print(
                            "[note] OpenEXR: OPENEXR_BUILD_PYTHON=OFF (windows.python_wrappers=auto with static CRT). "
                            "Set windows.python_wrappers=on (or windows.msvc_runtime=dynamic) to enable wrappers.",
                            flush=True,
                        )
                    self._openexr_python_note_printed = True
            args += [
                "-DOPENEXR_BUILD_TOOLS=ON",
                "-DOPENEXR_INSTALL_TOOLS=ON",
                "-DOPENEXR_BUILD_EXAMPLES=ON",
                "-DOPENEXR_BUILD_TESTS=OFF",
                f"-DOPENEXR_BUILD_PYTHON={openexr_build_python}",
                "-DOPENEXR_TEST_PYTHON=OFF",
                "-DBUILD_TESTING=OFF",
                "-DOPENEXR_FORCE_INTERNAL_IMATH=OFF",
                "-DOPENEXR_FORCE_INTERNAL_DEFLATE=OFF",
                "-DOPENEXR_FORCE_INTERNAL_OPENJPH=OFF",
            ]
        elif name == "libjxl" and not recipe_applied:
            enable_openexr = "ON" if cfg.build_exr_stack else "OFF"
            args += [
                "-DBUILD_TESTING=OFF",
                f"-DJPEGXL_ENABLE_TOOLS={cfg.libjxl_enable_tools}",
                f"-DJPEGXL_ENABLE_OPENEXR={enable_openexr}",
                "-DJPEGXL_ENABLE_BENCHMARK=OFF",
                "-DJPEGXL_ENABLE_DEVTOOLS=OFF",
                "-DJPEGXL_ENABLE_EXAMPLES=OFF",
                "-DJPEGXL_ENABLE_DOXYGEN=OFF",
                "-DJPEGXL_ENABLE_MANPAGES=OFF",
                "-DJPEGXL_ENABLE_VIEWERS=OFF",
                "-DJPEGXL_ENABLE_JNI=OFF",
                "-DJPEGXL_ENABLE_PLUGINS=OFF",
                "-DJPEGXL_ENABLE_SKCMS=OFF",
                "-DJPEGXL_ENABLE_SJPEG=OFF",
                "-DJPEGXL_FORCE_SYSTEM_BROTLI=ON",
                "-DJPEGXL_FORCE_SYSTEM_LCMS2=ON",
                "-DJPEGXL_FORCE_SYSTEM_HWY=ON",
                "-DJPEGXL_FORCE_SYSTEM_GTEST=ON",
                "-DJPEGXL_BUNDLE_LIBPNG=OFF",
            ]
        elif name == "libultrahdr":
            args += ["-DUHDR_BUILD_DEPS=OFF", "-DUHDR_BUILD_TESTS=OFF", "-DUHDR_BUILD_BENCHMARK=OFF"]
        elif name == "minizip-ng":
            args += [
                "-DMZ_COMPAT=OFF",
                "-DMZ_BUILD_TESTS=OFF",
                "-DMZ_FORCE_FETCH_LIBS=OFF",
                "-DMZ_ZLIB=ON",
                "-DMZ_BZIP2=OFF",
                "-DMZ_LZMA=OFF",
                "-DMZ_ZSTD=OFF",
                "-DMZ_LIBCOMP=OFF",
                "-DMZ_OPENSSL=OFF",
            ]
        elif name == "yaml-cpp":
            args += ["-DYAML_BUILD_SHARED_LIBS=OFF", "-DYAML_CPP_INSTALL=ON"]
        elif name == "expat":
            if self.platform.os == "windows":
                runtime_mode = str(cfg.windows.get("msvc_runtime", "static")).strip().lower()
                if runtime_mode in {"", "static", "mt", "multithreaded"}:
                    args.append("-DEXPAT_MSVC_STATIC_CRT=ON")
                elif runtime_mode in {"dynamic", "md", "multithreadeddll"}:
                    args.append("-DEXPAT_MSVC_STATIC_CRT=OFF")
        elif name == "OpenColorIO":
            ocio_build_python = "ON"
            if self.platform.os == "windows":
                wrappers_enabled, reason = self._windows_python_wrappers_enabled()
                ocio_build_python = "ON" if wrappers_enabled else "OFF"
                if ocio_build_python == "OFF" and not self._ocio_python_note_printed:
                    if reason == "forced-off":
                        print("[note] OpenColorIO: OCIO_BUILD_PYTHON=OFF (windows.python_wrappers=off)", flush=True)
                    else:
                        print(
                            "[note] OpenColorIO: OCIO_BUILD_PYTHON=OFF (windows.python_wrappers=auto with static CRT). "
                            "Set windows.python_wrappers=on (or windows.msvc_runtime=dynamic) to enable wrappers.",
                            flush=True,
                        )
                    self._ocio_python_note_printed = True
            args += [
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
            if self.platform.os == "windows":
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
        elif name == "googletest":
            args += [
                "-DINSTALL_GTEST=ON",
                "-DBUILD_GMOCK=OFF",
                "-Dgtest_build_tests=OFF",
                "-Dgtest_build_samples=OFF",
            ]
        elif name == "pybind11":
            args += ["-DPYBIND11_TEST=OFF", "-DPYBIND11_INSTALL=ON"]
        elif name == "OpenImageIO":
            args.extend(self._oiio_cache_args(ctx))

        return args

    def _oiio_cache_args(self, ctx: BuildContext) -> list[str]:
        cfg = self.config.global_cfg
        args: list[str] = []
        cache_path = cfg.src_root / "OpenImageIO" / "build" / "CMakeCache.txt"
        allow = {
            "BUILD_SHARED_LIBS",
            "OIIO_BUILD_TOOLS",
            "OIIO_BUILD_TESTS",
            "OIIO_BUILD_DOCS",
            "USE_PYTHON",
            "USE_JXL",
            "USE_FFMPEG",
            "USE_QT",
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
            "OIIO_BUILD_TOOLS": "ON",
            "OIIO_BUILD_TESTS": "OFF",
            "OIIO_BUILD_DOCS": "OFF",
            "USE_PYTHON": "ON",
            "LINKSTATIC": "ON",
        }
        for key, value in defaults.items():
            values.setdefault(key, value)

        if cfg.build_ffmpeg:
            values.setdefault("USE_FFMPEG", "ON")

        # Keep Windows behavior explicit: ffmpeg is currently not supported by this builder on Windows.
        if self.platform.os == "windows":
            values["USE_FFMPEG"] = "OFF"
        else:
            values["USE_FFMPEG"] = "ON" if cfg.build_ffmpeg else "OFF"

        # Python is mandatory for OIIO in this setup.
        values["USE_PYTHON"] = "ON"

        # Ensure static dependency linking is propagated for static builds.
        args.append(f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={self._oiio_linkstatic_include(ctx)}")

        for key in sorted(values):
            args.append(f"-D{key}={values[key]}")
        return args

    def _oiio_linkstatic_include(self, ctx: BuildContext) -> str:
        include_path = ctx.build_dir / "oiio_linkstatic.cmake"
        extra_libs = self._oiio_extra_static_libs(ctx.install_prefix)
        extra_list = "\n  ".join(extra_libs) if extra_libs else ""
        content = """\
function(_oiio_linkstatic_fixup)
  if (NOT TARGET OpenImageIO)
    return()
  endif()
  if (BUILD_SHARED_LIBS)
    return()
  endif()
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
        include_path.write_text(content.replace("__EXTRA_LIBS__", extra_list), encoding="utf-8")
        return str(include_path)

    def _oiio_extra_static_libs(self, prefix: Path) -> list[str]:
        prefix = prefix.resolve()
        libs: list[str] = []
        libdir = prefix / "lib"

        def add_lib(name: str) -> None:
            path = libdir / name
            if path.exists():
                libs.append(str(path))

        # JXL deps
        add_lib("libjxl_cms.a")
        add_lib("libbrotlidec.a")
        add_lib("libbrotlienc.a")
        add_lib("libbrotlicommon.a")

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

        # OpenMP (libomp)
        omp_root = self.config.global_cfg.env.get("OpenMP_ROOT") or os.environ.get("OpenMP_ROOT")
        if omp_root:
            for candidate in ("libomp.dylib", "libomp.a"):
                path = Path(omp_root) / "lib" / candidate
                if path.exists():
                    libs.append(str(path))
                    break

        # iconv (system)
        if (libdir / "libiconv.a").exists():
            libs.append(str(libdir / "libiconv.a"))
        else:
            if self.platform.os == "macos":
                libs.append("iconv")

        # macOS Security framework (minizip-ng uses SecRandomCopyBytes)
        if self.platform.os == "macos":
            libs.append("-Wl,-framework,Security")
            if self.config.global_cfg.build_ffmpeg:
                libs.extend(
                    [
                        "-Wl,-framework,AudioToolbox",
                        "-Wl,-framework,VideoToolbox",
                        "-Wl,-framework,CoreMedia",
                        "-Wl,-framework,CoreVideo",
                        "-Wl,-framework,CoreFoundation",
                    ]
                )

        return libs

    def _autotools_args(self, repo: RepoConfig) -> list[str]:
        if repo.name == "xz":
            return ["--disable-nls", "--disable-xz", "--disable-xzdec", "--disable-lzmadec", "--disable-lzmainfo"]
        if repo.name == "lcms2":
            return ["--without-fastfloat", "--without-threaded"]
        return []

    def _ffmpeg_configure_args(self, ctx: BuildContext) -> list[str]:
        cfg = self.config.global_cfg
        args = [
            f"--prefix={ctx.install_prefix}",
            "--disable-shared",
            "--enable-static",
            "--enable-pic",
            "--disable-doc",
            "--pkg-config-flags=--static",
        ]
        if ctx.build_type == "Release":
            args.append("--disable-debug")
        else:
            args.append("--enable-debug=3")

        if "cc" in self.toolchain:
            args.append(f"--cc={self.toolchain['cc']}")
        if "cxx" in self.toolchain:
            args.append(f"--cxx={self.toolchain['cxx']}")
        if "ar" in self.toolchain:
            args.append(f"--ar={self.toolchain['ar']}")
        if "ranlib" in self.toolchain:
            args.append(f"--ranlib={self.toolchain['ranlib']}")
        if self.platform.os == "macos":
            sdkroot = self.toolchain.get("sdkroot")
            if sdkroot:
                args.append(f"--sysroot={sdkroot}")

        cflags, cxxflags, ldflags = self._non_cmake_flags(ctx.build_type)
        include_dir = ctx.install_prefix / "include"
        lib_dir = ctx.install_prefix / "lib"
        cflags = f"{cflags} -I{include_dir}"
        cxxflags = f"{cxxflags} -I{include_dir}"
        ldflags = f"{ldflags} -L{lib_dir}"
        args.append(f"--extra-cflags={cflags}")
        args.append(f"--extra-cxxflags={cxxflags}")
        args.append(f"--extra-ldflags={ldflags}")
        return args

    def _cmake_common_args(self, repo: RepoConfig, ctx: BuildContext) -> list[str]:
        cfg = self.config.global_cfg
        args: list[str] = [
            f"-DCMAKE_BUILD_TYPE={ctx.build_type}",
            f"-DCMAKE_INSTALL_PREFIX={ctx.install_prefix}",
            f"-DCMAKE_PREFIX_PATH={ctx.install_prefix}",
            f"-DCMAKE_INCLUDE_PATH={ctx.install_prefix / 'include'}",
            f"-DCMAKE_LIBRARY_PATH={ctx.install_prefix / 'lib'}",
            f"-DCMAKE_CXX_STANDARD={repo.cxx_standard or cfg.cxx_standard}",
            f"-DCMAKE_CXX_EXTENSIONS={'ON' if cfg.cxx_extensions else 'OFF'}",
            "-DPKG_CONFIG_USE_STATIC_LIBS=ON",
        ]

        def _normalize_override(value: str | None) -> str | None:
            if not value:
                return None
            trimmed = value.strip()
            if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"\"", "'"}:
                trimmed = trimmed[1:-1]
            return trimmed or None

        pkg_cfg = _normalize_override(cfg.env.get("PKG_CONFIG_EXECUTABLE") or cfg.env.get("PKG_CONFIG"))
        if self.platform.os == "windows":
            pkg_cfg = _normalize_override(
                cfg.windows_env.get("PKG_CONFIG_EXECUTABLE") or cfg.windows_env.get("PKG_CONFIG") or os.environ.get("PKG_CONFIG_EXECUTABLE") or os.environ.get("PKG_CONFIG")
            ) or pkg_cfg
        else:
            pkg_cfg = _normalize_override(os.environ.get("PKG_CONFIG_EXECUTABLE") or os.environ.get("PKG_CONFIG")) or pkg_cfg
        if pkg_cfg:
            args.append(f"-DPKG_CONFIG_EXECUTABLE={pkg_cfg}")

        doxygen = _normalize_override(cfg.env.get("DOXYGEN_EXECUTABLE"))
        if self.platform.os == "windows":
            doxygen = _normalize_override(cfg.windows_env.get("DOXYGEN_EXECUTABLE") or os.environ.get("DOXYGEN_EXECUTABLE")) or doxygen
        else:
            doxygen = _normalize_override(os.environ.get("DOXYGEN_EXECUTABLE")) or doxygen
        if doxygen:
            args.append(f"-DDOXYGEN_EXECUTABLE={doxygen}")

        if cfg.pic:
            args.append("-DCMAKE_POSITION_INDEPENDENT_CODE=ON")

        if self.platform.os == "windows":
            debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
            args.append(f"-DCMAKE_DEBUG_POSTFIX={debug_postfix}")
            args.append("-DCMAKE_POLICY_DEFAULT_CMP0091=NEW")
            runtime_mode = self._windows_runtime_mode()
            if runtime_mode == "static":
                runtime = "MultiThreaded$<$<CONFIG:Debug>:Debug>"
            elif runtime_mode == "dynamic":
                runtime = "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL"
            else:
                runtime = str(cfg.windows.get("msvc_runtime"))
            args.append(f"-DCMAKE_MSVC_RUNTIME_LIBRARY={runtime}")

        if repo.shared is None:
            build_shared = not cfg.static_default
        else:
            build_shared = repo.shared
        args.append(f"-DBUILD_SHARED_LIBS={'ON' if build_shared else 'OFF'}")

        cflags = self._base_flags(ctx.build_type)
        cxxflags = self._base_flags(ctx.build_type)
        if self.platform.os == "windows":
            cxxflags += " /bigobj"
        if self.platform.os in {"macos", "linux"} and cfg.use_libcxx:
            cxxflags += " -stdlib=libc++"

        if ctx.build_type == "ASAN":
            if self.platform.os == "windows":
                cxxflags += " /fsanitize=address"
                cflags += " /fsanitize=address"
            else:
                cxxflags += " -fsanitize=address -fno-omit-frame-pointer"
                cflags += " -fsanitize=address -fno-omit-frame-pointer"
        args.append(f"-DCMAKE_C_FLAGS_INIT={cflags}")
        args.append(f"-DCMAKE_CXX_FLAGS_INIT={cxxflags}")

        linker_flags = self._linker_flags_init()
        if linker_flags:
            args += [
                f"-DCMAKE_EXE_LINKER_FLAGS_INIT={linker_flags}",
                f"-DCMAKE_SHARED_LINKER_FLAGS_INIT={linker_flags}",
                f"-DCMAKE_MODULE_LINKER_FLAGS_INIT={linker_flags}",
            ]

        if self.toolchain:
            if "cc" in self.toolchain:
                args.append(f"-DCMAKE_C_COMPILER={self.toolchain['cc']}")
            if "cxx" in self.toolchain:
                args.append(f"-DCMAKE_CXX_COMPILER={self.toolchain['cxx']}")
            if "ld" in self.toolchain:
                args.append(f"-DCMAKE_LINKER={self.toolchain['ld']}")
            if "ar" in self.toolchain:
                args.append(f"-DCMAKE_AR={self.toolchain['ar']}")
            if "ranlib" in self.toolchain:
                args.append(f"-DCMAKE_RANLIB={self.toolchain['ranlib']}")

        return args

    def _cmake_generator_args(self) -> list[str]:
        cfg = self.config.global_cfg
        if self.platform.os != "windows":
            return ["-G", "Ninja"]

        generator = str(cfg.windows.get("generator", "ninja-msvc"))
        if generator == "msvc":
            return ["-G", "Visual Studio 17 2022"]
        if generator == "msvc-clang-cl":
            return ["-G", "Visual Studio 17 2022", "-T", "ClangCL"]
        if generator == "ninja-clang-cl":
            return ["-G", "Ninja", "-DCMAKE_C_COMPILER=clang-cl", "-DCMAKE_CXX_COMPILER=clang-cl"]
        # default: ninja + msvc
        return ["-G", "Ninja"]

    def _resolve_repo_dir(self, repo: RepoConfig) -> Path:
        cfg = self.config.global_cfg
        if Path(repo.dir).is_absolute():
            return Path(repo.dir)
        candidates = [repo.dir] + repo.dir_candidates
        for cand in candidates:
            base = cfg.src_root / cand
            if "*" in cand or "?" in cand:
                matches = list(cfg.src_root.glob(cand))
                if matches:
                    return matches[0]
            if base.exists():
                return base
        return cfg.src_root / repo.dir

    def _libiconv_export_zip(self, env: dict[str, str] | None = None) -> Path:
        cfg = self.config.global_cfg
        default = cfg.repo_root / "external" / "vcpkg-export-libiconv.zip"
        override = None
        if env:
            override = env.get("LIBICONV_VCPKG_EXPORT_ZIP") or env.get("VCPKG_LIBICONV_EXPORT_ZIP")
        if not override and self.platform.os == "windows":
            override = (
                cfg.windows_env.get("LIBICONV_VCPKG_EXPORT_ZIP")
                or cfg.windows_env.get("VCPKG_LIBICONV_EXPORT_ZIP")
                or cfg.env.get("LIBICONV_VCPKG_EXPORT_ZIP")
                or cfg.env.get("VCPKG_LIBICONV_EXPORT_ZIP")
                or os.environ.get("LIBICONV_VCPKG_EXPORT_ZIP")
                or os.environ.get("VCPKG_LIBICONV_EXPORT_ZIP")
            )
        if override:
            path = Path(os.path.expandvars(override)).expanduser()
            if not path.is_absolute():
                path = (cfg.repo_root / path).resolve()
            return path

        external_dir = cfg.repo_root / "external"
        if default.exists():
            return default
        if external_dir.is_dir():
            matches = sorted(external_dir.glob("vcpkg-export-libiconv*.zip"))
            if matches:
                return matches[0]
        return default

    def _maybe_skip_missing(self, repo: RepoConfig, path: Path) -> bool:
        if repo.name == "libiconv" and self.platform.os == "windows":
            zip_path = self._libiconv_export_zip()
            if zip_path.exists():
                return False
            if repo.optional:
                print(f"[skip] {repo.name}: missing vcpkg export zip at {zip_path}")
                return True
            return False
        if path.exists():
            return False
        if repo.optional and not repo.url:
            print(f"[skip] {repo.name}: missing optional source at {path}")
            return True
        return False

    def _patch_glew_macos(self, src_dir: Path) -> None:
        if self.platform.os != "macos":
            return
        cmake_lists = src_dir / "CMakeLists.txt"
        if not cmake_lists.exists():
            return
        text = cmake_lists.read_text(encoding="utf-8")
        if "AGL_LIBRARY AGL REQUIRED" not in text:
            return
        pattern = r"find_library\\(AGL_LIBRARY AGL REQUIRED\\)\\s*\\n\\s*list\\(APPEND LIBRARIES \\$\\{AGL_LIBRARY\\}\\)"
        replacement = (
            "find_library(AGL_LIBRARY AGL)\\n"
            "  if(AGL_LIBRARY)\\n"
            "    list(APPEND LIBRARIES ${AGL_LIBRARY})\\n"
            "  endif()"
        )
        patched = re.sub(pattern, replacement, text, flags=re.M)
        if patched != text:
            cmake_lists.write_text(patched, encoding="utf-8")

    def _ensure_png16_include_alias(self, prefix: Path) -> None:
        cfg = self.config.global_cfg
        if not cfg.openimageio_patch_png_include:
            return
        include_dir = prefix / "include"
        src = (include_dir / "png.h").resolve()
        if not src.exists():
            return
        alias_dir = include_dir / "libpng16"
        alias_dir.mkdir(parents=True, exist_ok=True)
        dst = alias_dir / "png.h"
        if dst.exists() or dst.is_symlink():
            try:
                if dst.is_symlink() and dst.resolve() == src:
                    return
            except OSError:
                pass
            try:
                dst.unlink()
            except OSError:
                return
        try:
            dst.symlink_to(src)
        except OSError:
            dst.write_bytes(src.read_bytes())

    def _make_openexr_pc_override(self, prefix: Path, build_type: str) -> None:
        src = prefix / "lib" / "pkgconfig" / "OpenEXR.pc"
        if not src.exists():
            return
        openjph_lib = "openjph"
        if build_type == "Debug" and (prefix / "lib" / "libopenjph_d.a").exists():
            openjph_lib = "openjph_d"
        override_dir = self.pkg_override_root / build_type
        override_dir.mkdir(parents=True, exist_ok=True)
        dst = override_dir / "OpenEXR.pc"
        if dst.exists():
            text = dst.read_text(encoding="utf-8")
            if "openjph" in text:
                return
        lines = []
        for line in src.read_text(encoding="utf-8").splitlines():
            if line.startswith("Libs:") and "openjph" not in line and "deflate" not in line:
                lines.append(f"{line} -ldeflate -l{openjph_lib}")
            else:
                lines.append(line)
        dst.write_text("\\n".join(lines) + "\\n", encoding="utf-8")

    def _ensure_openjph_alias(self, prefix: Path) -> None:
        libdir = prefix / "lib"
        debug_lib = libdir / "libopenjph_d.a"
        release_lib = libdir / "libopenjph.a"
        if debug_lib.exists() and not release_lib.exists():
            try:
                release_lib.symlink_to(debug_lib.name)
            except OSError:
                release_lib.write_bytes(debug_lib.read_bytes())

    def _stamp_state(
        self, repo: RepoConfig, ctx: BuildContext, deps_heads: dict[str, str | None], cflags: str, cxxflags: str
    ) -> tuple[str, bool, str]:
        if self.force:
            return "build", False, "forced"
        stamp_dir = self.config.global_cfg.build_root / ".stamps" / repo.name
        stamp_path = stamp_dir / f"{ctx.build_type}.json"
        existing = read_stamp(stamp_path)
        if not existing:
            return "build", False, "no-stamp"
        payload = self._stamp_payload(repo, ctx, deps_heads, cflags, cxxflags)
        current = compute_stamp(payload)
        if existing.get("stamp") == current:
            return "skip", True, "up-to-date"
        return "build", True, "stamp-changed"

    def _write_stamp(self, repo: RepoConfig, ctx: BuildContext, deps_heads: dict[str, str | None], cflags: str, cxxflags: str) -> None:
        stamp_dir = self.config.global_cfg.build_root / ".stamps" / repo.name
        stamp_path = stamp_dir / f"{ctx.build_type}.json"
        payload = self._stamp_payload(repo, ctx, deps_heads, cflags, cxxflags)
        payload["stamp"] = compute_stamp(payload)
        write_stamp(stamp_path, payload)

    def _stamp_payload(
        self, repo: RepoConfig, ctx: BuildContext, deps_heads: dict[str, str | None], cflags: str, cxxflags: str
    ) -> dict:
        payload = {
            "repo": repo.name,
            "build_type": ctx.build_type,
            "toolchain": self._toolchain_fingerprint(),
            "repo_head": git_head(ctx.src_dir),
            "deps": deps_heads,
            "cmake_args": repo.cmake_args,
            "build_system": repo.build_system,
            "cflags": cflags,
            "cxxflags": cxxflags,
        }
        recipe_revision = recipe_registry.stamp_revision(repo.name)
        if recipe_revision is not None:
            payload["builder_patch_rev"] = recipe_revision
        if repo.name == "libiconv" and self.platform.os == "windows":
            zip_path = self._libiconv_export_zip()
            payload["vcpkg_export_zip"] = str(zip_path)
            if zip_path.exists():
                st = zip_path.stat()
                payload["vcpkg_export_zip_size"] = int(st.st_size)
                payload["vcpkg_export_zip_mtime"] = int(st.st_mtime)
        return payload

    def _build_repo(self, repo: RepoConfig, build_type: str, deps_heads: dict[str, str | None]) -> tuple[str, str]:
        if not repo.build_system:
            return "skipped", "no-build-system"

        install_prefix = self.prefixes[build_type]
        build_dir = self.config.global_cfg.build_root / build_type / repo.name
        src_dir = self.repo_paths[repo.name]
        if repo.source_subdir:
            src_dir = src_dir / repo.source_subdir

        ctx = BuildContext(repo=repo, build_type=build_type, build_dir=build_dir, install_prefix=install_prefix, src_dir=src_dir)

        cflags = self._base_flags(build_type)
        cxxflags = self._base_flags(build_type)
        if self.platform.os in {"macos", "linux"} and self.config.global_cfg.use_libcxx:
            cxxflags += " -stdlib=libc++"
        if build_type == "ASAN":
            if self.platform.os == "windows":
                cflags += " /fsanitize=address"
                cxxflags += " /fsanitize=address"
            else:
                cflags += " -fsanitize=address -fno-omit-frame-pointer"
                cxxflags += " -fsanitize=address -fno-omit-frame-pointer"

        state, had_stamp, reason = self._stamp_state(repo, ctx, deps_heads, cflags, cxxflags)
        if state == "skip":
            print(f"[skip] {repo.name} ({build_type}) up-to-date")
            return "skipped", reason

        banner(f"{repo.name} ({build_type})", color="cyan")

        env = self._env_for_build(build_type, install_prefix)

        if repo.name == "glew":
            self._patch_glew_macos(src_dir)
        recipe_registry.patch_source(repo.name, self, src_dir)
        if repo.name == "libjxl" and build_type == "Debug":
            self._ensure_openjph_alias(install_prefix)
        if repo.name == "libpng":
            self._ensure_png16_include_alias(install_prefix)
        if repo.name == "OpenImageIO":
            self._ensure_png16_include_alias(install_prefix)

        if repo.build_system == "cmake":
            if not self.dry_run:
                cache = build_dir / "CMakeCache.txt"
                if cache.exists():
                    shutil.rmtree(build_dir, ignore_errors=True)
            build_dir.mkdir(parents=True, exist_ok=True)
            cmd = ["cmake", "-S", str(src_dir), "-B", str(build_dir)]
            cmd.extend(self._cmake_generator_args())

            cmake_args = self._cmake_common_args(repo, ctx)
            cmake_args.extend(self._repo_specific_args(repo, ctx))
            cmake_args.extend(self._expand_args(repo.cmake_args, build_type, install_prefix))
            cmd.extend(cmake_args)

            print_cmd("Full cmake config command", cmd)
            banner(f"{repo.name} ({build_type}) - configure")
            run(cmd, env=env, dry_run=self.dry_run)

            build_cmd = ["cmake", "--build", str(build_dir), "--config", build_type, "--parallel", str(self._jobs())]
            print_cmd("build command", build_cmd)
            banner(f"{repo.name} ({build_type}) - building")
            run(build_cmd, env=env, dry_run=self.dry_run)

            install_cmd = ["cmake", "--install", str(build_dir), "--config", build_type]
            print_cmd("install command", install_cmd)
            banner(f"{repo.name} ({build_type}) - install")
            run(install_cmd, env=env, dry_run=self.dry_run)
        elif repo.build_system == "autotools":
            build_dir.mkdir(parents=True, exist_ok=True)
            configure = src_dir / "configure"
            if not configure.exists():
                raise RuntimeError(f"Missing configure script for {repo.name}: {configure}")
            cflags, cxxflags, ldflags = self._non_cmake_flags(build_type)
            include_dir = install_prefix / "include"
            lib_dir = install_prefix / "lib"
            env = {
                **env,
                "CFLAGS": f"{cflags} -I{include_dir}",
                "CXXFLAGS": f"{cxxflags} -I{include_dir}",
                "LDFLAGS": f"{ldflags} -L{lib_dir}",
                "CPPFLAGS": f"-I{include_dir}",
            }
            cmd = [str(configure), f"--prefix={install_prefix}", "--disable-shared", "--enable-static"]
            cmd.extend(self._autotools_args(repo))
            print_cmd("configure command", cmd)
            banner(f"{repo.name} ({build_type}) - configure")
            run(cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run)

            build_cmd = ["make", f"-j{self._jobs()}"]
            print_cmd("build command", build_cmd)
            banner(f"{repo.name} ({build_type}) - building")
            run(build_cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run)

            install_cmd = ["make", "install"]
            print_cmd("install command", install_cmd)
            banner(f"{repo.name} ({build_type}) - install")
            run(install_cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run)
        elif repo.build_system == "ffmpeg":
            build_dir.mkdir(parents=True, exist_ok=True)
            configure = src_dir / "configure"
            if not configure.exists():
                raise RuntimeError(f"Missing configure script for {repo.name}: {configure}")
            cmd = [str(configure)]
            cmd.extend(self._ffmpeg_configure_args(ctx))
            print_cmd("configure command", cmd)
            banner(f"{repo.name} ({build_type}) - configure")
            run(cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run)

            build_cmd = ["make", f"-j{self._jobs()}"]
            print_cmd("build command", build_cmd)
            banner(f"{repo.name} ({build_type}) - building")
            run(build_cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run)

            install_cmd = ["make", "install"]
            print_cmd("install command", install_cmd)
            banner(f"{repo.name} ({build_type}) - install")
            run(install_cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run)
        elif repo.build_system == "libiconv":
            if self.platform.os != "windows":
                raise RuntimeError("libiconv build system is only supported on Windows")
            build_dir.mkdir(parents=True, exist_ok=True)

            zip_path = self._libiconv_export_zip(env)
            if not zip_path.exists():
                raise RuntimeError(f"Missing libiconv vcpkg export zip: {zip_path}")

            banner(f"{repo.name} ({build_type}) - stage")
            print(f"vcpkg export zip: {zip_path}", flush=True)

            import zipfile

            export_dir = build_dir / "_libiconv_vcpkg_export"
            marker = export_dir / ".zipstamp"
            st = zip_path.stat()
            stamp = f"{zip_path}|{int(st.st_size)}|{int(st.st_mtime)}"

            if self.dry_run:
                print(f"[dry-run] extract -> {export_dir}", flush=True)
                return ("rebuilt" if had_stamp else "built"), ""

            if marker.exists() and marker.read_text(encoding="utf-8").strip() == stamp:
                pass
            else:
                if export_dir.exists():
                    shutil.rmtree(export_dir, ignore_errors=True)
                export_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(zip_path) as zf:
                    export_abs = export_dir.resolve()
                    for info in zf.infolist():
                        name = info.filename
                        if not name or name.endswith("/"):
                            continue
                        dest = export_dir / name
                        dest_abs = dest.resolve()
                        if export_abs not in dest_abs.parents and dest_abs != export_abs:
                            raise RuntimeError(f"Refusing to extract outside destination: {name}")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src_f, open(dest, "wb") as dst_f:
                            shutil.copyfileobj(src_f, dst_f)
                marker.write_text(stamp, encoding="utf-8")

            def _find_export_root(base: Path) -> Path:
                if (base / "installed").is_dir():
                    return base
                for child in base.iterdir():
                    if child.is_dir() and (child / "installed").is_dir():
                        return child
                raise RuntimeError(f"Unexpected vcpkg export layout under {base}")

            export_root = _find_export_root(export_dir)
            installed_dir = export_root / "installed"

            triplet_candidates = [
                p
                for p in installed_dir.iterdir()
                if p.is_dir() and p.name != "vcpkg" and (p / "include" / "iconv.h").exists()
            ]
            if not triplet_candidates:
                raise RuntimeError(f"vcpkg export zip does not contain installed/<triplet>/include/iconv.h: {zip_path}")

            def _triplet_score(path: Path) -> tuple[int, str]:
                name = path.name.lower()
                score = 0
                if "static" in name:
                    score -= 10
                bin_dir = path / "bin"
                if bin_dir.is_dir() and any(bin_dir.glob("*.dll")):
                    score += 5
                return score, name

            triplet_candidates.sort(key=_triplet_score)
            triplet_dir = triplet_candidates[0]

            include_src = triplet_dir / "include"
            lib_src = triplet_dir / "lib"
            debug_lib_src = triplet_dir / "debug" / "lib"
            bin_src = triplet_dir / "bin"

            required = [
                include_src / "iconv.h",
                lib_src / "iconv.lib",
                lib_src / "charset.lib",
                debug_lib_src / "iconv.lib",
                debug_lib_src / "charset.lib",
            ]
            missing = [p for p in required if not p.exists()]
            if missing:
                wanted = "\n".join(f"  - {p}" for p in missing)
                raise RuntimeError(f"libiconv vcpkg export is missing expected files:\n{wanted}")

            debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))

            def _add_debug_postfix(filename: str) -> str:
                p = Path(filename)
                suffixes = p.suffixes
                if not suffixes:
                    return filename + debug_postfix
                base = filename
                for suff in suffixes:
                    if base.endswith(suff):
                        base = base[: -len(suff)]
                if base.endswith(debug_postfix):
                    return filename
                return base + debug_postfix + "".join(suffixes)

            banner(f"{repo.name} ({build_type}) - install")

            inc_dst = install_prefix / "include"
            lib_dst = install_prefix / "lib"
            bin_dst = install_prefix / "bin"
            inc_dst.mkdir(parents=True, exist_ok=True)
            lib_dst.mkdir(parents=True, exist_ok=True)
            if bin_src.is_dir():
                bin_dst.mkdir(parents=True, exist_ok=True)

            for item in include_src.iterdir():
                if item.is_file():
                    shutil.copy2(item, inc_dst / item.name)
            for item in lib_src.iterdir():
                if item.is_file():
                    shutil.copy2(item, lib_dst / item.name)
            for item in debug_lib_src.iterdir():
                if item.is_file():
                    shutil.copy2(item, lib_dst / _add_debug_postfix(item.name))

            if bin_src.is_dir():
                if any(bin_src.glob("*.dll")):
                    print("[note] libiconv export contains DLLs; prefer exporting a *-static triplet for a fully static prefix", flush=True)
                for item in bin_src.iterdir():
                    if item.is_file() and item.suffix.lower() in {".dll", ".pdb"}:
                        shutil.copy2(item, bin_dst / item.name)

            cmake_dir = install_prefix / "lib" / "cmake" / "Iconv"
            cmake_dir.mkdir(parents=True, exist_ok=True)
            (cmake_dir / "IconvConfig.cmake").write_text(
                "\n".join(
                    [
                        "# Generated by oiio-builder (imported from vcpkg export)",
                        "set(Iconv_FOUND TRUE)",
                        "set(Iconv_IS_BUILT_IN FALSE)",
                        "",
                        "get_filename_component(_iconv_prefix \"${CMAKE_CURRENT_LIST_DIR}/../../..\" ABSOLUTE)",
                        "set(_iconv_incdir \"${_iconv_prefix}/include\")",
                        "set(_iconv_libdir \"${_iconv_prefix}/lib\")",
                        f"set(_iconv_debug_postfix \"{debug_postfix}\")",
                        "",
                        "set(_iconv_lib_release \"${_iconv_libdir}/iconv.lib\")",
                        "set(_iconv_lib_debug \"${_iconv_libdir}/iconv${_iconv_debug_postfix}.lib\")",
                        "set(_charset_lib_release \"${_iconv_libdir}/charset.lib\")",
                        "set(_charset_lib_debug \"${_iconv_libdir}/charset${_iconv_debug_postfix}.lib\")",
                        "",
                        "if(NOT TARGET Iconv::Charset)",
                        "  add_library(Iconv::Charset UNKNOWN IMPORTED)",
                        "  set_property(TARGET Iconv::Charset PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG\")",
                        "  set_target_properties(Iconv::Charset PROPERTIES",
                        "    INTERFACE_INCLUDE_DIRECTORIES \"${_iconv_incdir}\"",
                        "    IMPORTED_LOCATION \"${_charset_lib_release}\"",
                        "    IMPORTED_LOCATION_RELEASE \"${_charset_lib_release}\"",
                        "    IMPORTED_LOCATION_DEBUG \"${_charset_lib_debug}\"",
                        "    MAP_IMPORTED_CONFIG_MINSIZEREL Release",
                        "    MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
                        "    MAP_IMPORTED_CONFIG_ASAN Release",
                        "  )",
                        "endif()",
                        "",
                        "if(NOT TARGET Iconv::Iconv)",
                        "  add_library(Iconv::Iconv UNKNOWN IMPORTED)",
                        "  set_property(TARGET Iconv::Iconv PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG\")",
                        "  set_target_properties(Iconv::Iconv PROPERTIES",
                        "    INTERFACE_INCLUDE_DIRECTORIES \"${_iconv_incdir}\"",
                        "    IMPORTED_LOCATION \"${_iconv_lib_release}\"",
                        "    IMPORTED_LOCATION_RELEASE \"${_iconv_lib_release}\"",
                        "    IMPORTED_LOCATION_DEBUG \"${_iconv_lib_debug}\"",
                        "    INTERFACE_LINK_LIBRARIES \"Iconv::Charset\"",
                        "    MAP_IMPORTED_CONFIG_MINSIZEREL Release",
                        "    MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
                        "    MAP_IMPORTED_CONFIG_ASAN Release",
                        "  )",
                        "endif()",
                        "",
                        "set(Iconv_INCLUDE_DIR \"${_iconv_incdir}\")",
                        "set(Iconv_INCLUDE_DIRS \"${_iconv_incdir}\")",
                        "set(Iconv_LIBRARY \"${_iconv_lib_release}\")",
                        "set(Iconv_LIBRARIES \"${_iconv_lib_release};${_charset_lib_release}\")",
                        "",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        elif repo.build_system == "giflib":
            build_dir.mkdir(parents=True, exist_ok=True)
            if self.platform.os == "windows":
                cmake_src_dir = build_dir / "_giflib_cmake"
                cmake_src_dir.mkdir(parents=True, exist_ok=True)
                cmake_lists = cmake_src_dir / "CMakeLists.txt"
                cmake_lists.write_text(
                    "\n".join(
                        [
                            "cmake_minimum_required(VERSION 3.20)",
                            "project(giflib C)",
                            "",
                            "if (NOT DEFINED GIFLIB_SRC_DIR)",
                            "  message(FATAL_ERROR \"GIFLIB_SRC_DIR is not set\")",
                            "endif()",
                            "",
                            "file(GLOB _giflib_sources",
                            "  \"${GIFLIB_SRC_DIR}/*.c\"",
                            "  \"${GIFLIB_SRC_DIR}/lib/*.c\"",
                            ")",
                            "",
                            "# Exclude tool sources (we only need the library for consumers like libjxl/OIIO).",
                            "set(_giflib_tool_sources",
                            "  \"${GIFLIB_SRC_DIR}/gif2rgb.c\"",
                            "  \"${GIFLIB_SRC_DIR}/gifbuild.c\"",
                            "  \"${GIFLIB_SRC_DIR}/giffix.c\"",
                            "  \"${GIFLIB_SRC_DIR}/giftext.c\"",
                            "  \"${GIFLIB_SRC_DIR}/giftool.c\"",
                            "  \"${GIFLIB_SRC_DIR}/gifclrmp.c\"",
                            ")",
                            "list(REMOVE_ITEM _giflib_sources ${_giflib_tool_sources})",
                            "",
                            "add_library(gif STATIC ${_giflib_sources})",
                            "target_include_directories(gif PUBLIC \"${GIFLIB_SRC_DIR}\")",
                            "set_target_properties(gif PROPERTIES OUTPUT_NAME gif DEBUG_POSTFIX \"${CMAKE_DEBUG_POSTFIX}\")",
                            "",
                            "install(TARGETS gif ARCHIVE DESTINATION lib)",
                            "install(FILES \"${GIFLIB_SRC_DIR}/gif_lib.h\" DESTINATION include)",
                            "",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )

                cmd = ["cmake", "-S", str(cmake_src_dir), "-B", str(build_dir)]
                cmd.extend(self._cmake_generator_args())
                cmd.append(f"-DGIFLIB_SRC_DIR={src_dir.as_posix()}")
                cmd.extend(self._cmake_common_args(repo, ctx))
                print_cmd("Full cmake config command", cmd)
                banner(f"{repo.name} ({build_type}) - configure")
                run(cmd, env=env, dry_run=self.dry_run)

                build_cmd = ["cmake", "--build", str(build_dir), "--config", build_type, "--parallel", str(self._jobs())]
                print_cmd("build command", build_cmd)
                banner(f"{repo.name} ({build_type}) - building")
                run(build_cmd, env=env, dry_run=self.dry_run)

                install_cmd = ["cmake", "--install", str(build_dir), "--config", build_type]
                print_cmd("install command", install_cmd)
                banner(f"{repo.name} ({build_type}) - install")
                run(install_cmd, env=env, dry_run=self.dry_run)
            else:
                make_env = env.copy()
                make_env["CC"] = self.toolchain.get("cc", make_env.get("CC", "cc"))
                cflags, _cxxflags, _ldflags = self._non_cmake_flags(build_type)
                make_env["CFLAGS"] = f"{cflags} -std=gnu99 -Wall -Wno-format-truncation"
                getversion = src_dir / "getversion"
                if getversion.exists() and not os.access(getversion, os.X_OK):
                    if not self.dry_run:
                        getversion.chmod(getversion.stat().st_mode | 0o111)
                try:
                    clean_cmd = ["make", "clean"]
                    print_cmd("clean command", clean_cmd)
                    banner(f"{repo.name} ({build_type}) - clean")
                    run(["make", "clean"], cwd=str(src_dir), env=make_env, dry_run=self.dry_run)
                except subprocess.CalledProcessError:
                    pass
                build_cmd = [
                    "make",
                    f"-j{self._jobs()}",
                    "libgif.a",
                    "libutil.a",
                    "gif2rgb",
                    "gifbuild",
                    "giffix",
                    "giftext",
                    "giftool",
                    "gifclrmp",
                ]
                print_cmd("build command", build_cmd)
                banner(f"{repo.name} ({build_type}) - building")
                run(
                    build_cmd,
                    cwd=str(src_dir),
                    env={
                        **make_env,
                        "PREFIX": str(install_prefix),
                        "BINDIR": str(install_prefix / "bin"),
                        "INCDIR": str(install_prefix / "include"),
                        "LIBDIR": str(install_prefix / "lib"),
                        "MANDIR": str(install_prefix / "share" / "man"),
                    },
                    dry_run=self.dry_run,
                )
                if not self.dry_run:
                    (install_prefix / "bin").mkdir(parents=True, exist_ok=True)
                    (install_prefix / "include").mkdir(parents=True, exist_ok=True)
                    (install_prefix / "lib").mkdir(parents=True, exist_ok=True)
                    banner(f"{repo.name} ({build_type}) - install")
                    print_cmd(
                        "install command",
                        [
                            "install",
                            "gif2rgb",
                            "gifbuild",
                            "giffix",
                            "giftext",
                            "giftool",
                            "gifclrmp",
                            str(install_prefix / "bin"),
                        ],
                    )
                    run(
                        ["install", "gif2rgb", "gifbuild", "giffix", "giftext", "giftool", "gifclrmp", str(install_prefix / "bin")],
                        cwd=str(src_dir),
                    )
                    print_cmd(
                        "install command",
                        [
                            "install",
                            "-m",
                            "644",
                            "gif_lib.h",
                            str(install_prefix / "include" / "gif_lib.h"),
                        ],
                    )
                    run(["install", "-m", "644", "gif_lib.h", str(install_prefix / "include" / "gif_lib.h")], cwd=str(src_dir))
                    print_cmd("install command", ["install", "-m", "644", "libgif.a", str(install_prefix / "lib" / "libgif.a")])
                    run(["install", "-m", "644", "libgif.a", str(install_prefix / "lib" / "libgif.a")], cwd=str(src_dir))
                    print_cmd("install command", ["install", "-m", "644", "libutil.a", str(install_prefix / "lib" / "libutil.a")])
                    run(["install", "-m", "644", "libutil.a", str(install_prefix / "lib" / "libutil.a")], cwd=str(src_dir))
        else:
            raise RuntimeError(f"Unsupported build_system: {repo.build_system}")

        if repo.name == "openexr":
            self._make_openexr_pc_override(install_prefix, build_type)
        if repo.name == "openjph" and build_type == "Debug":
            self._ensure_openjph_alias(install_prefix)

        if not self.dry_run:
            self._write_stamp(repo, ctx, deps_heads, cflags, cxxflags)

        return ("rebuilt" if had_stamp else "built"), ""

    def _jobs(self) -> int:
        cfg = self.config.global_cfg
        return cfg.jobs if cfg.jobs > 0 else os.cpu_count() or 4

    def run(self) -> int:
        deps_map = {repo.name: repo.deps for repo in self.repos}
        order = topo_sort([r.name for r in self.repos], deps_map)
        repos_by_name = {repo.name: repo for repo in self.repos}
        build_types = self._build_type_order()
        report = BuildReport(build_types, order, self.prefixes)

        # Resolve paths and clone/update repos.
        for repo_name in order:
            repo = repos_by_name[repo_name]
            repo_dir = self._resolve_repo_dir(repo)
            self.repo_paths[repo.name] = repo_dir
            if self._maybe_skip_missing(repo, repo_dir):
                continue
            if repo.name == "libiconv" and self.platform.os == "windows":
                continue
            ensure_repo(repo_dir, repo.url, repo.ref, repo.ref_type, update=not self.no_update, dry_run=self.dry_run)

        for build_type in build_types:
            for repo_name in order:
                repo = repos_by_name[repo_name]
                src_dir = self.repo_paths.get(repo.name, self._resolve_repo_dir(repo))
                if self._maybe_skip_missing(repo, src_dir):
                    report.record(build_type, repo.name, "missing", "not-found")
                    continue
                deps_heads = {
                    dep: git_head(self.repo_paths[dep])
                    for dep in repo.deps
                    if dep in repos_by_name and dep in self.repo_paths
                }
                # Decide build system for xz/lcms2 based on config and source layout.
                if repo.name == "xz":
                    cmake_lists = src_dir / "CMakeLists.txt"
                    repo.build_system = (
                        "autotools" if (self.config.global_cfg.xz_use_autotools or not cmake_lists.exists()) else "cmake"
                    )
                if repo.name == "lcms2":
                    cmake_lists = src_dir / "CMakeLists.txt"
                    repo.build_system = (
                        "autotools" if (self.config.global_cfg.lcms2_use_autotools or not cmake_lists.exists()) else "cmake"
                    )
                try:
                    status, detail = self._build_repo(repo, build_type, deps_heads)
                    report.record(build_type, repo.name, status, detail)
                except Exception as exc:
                    message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                    report.record(build_type, repo.name, "failed", message)
                    report.print()
                    raise
        report.print()
        return 0
