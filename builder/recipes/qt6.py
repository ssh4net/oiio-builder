from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess

from .. import backends as build_backends
from .policy import ffmpeg_enabled, qt6_enabled
from ..runner import banner, print_cmd, run


def enabled(builder, _repo) -> bool:
    return qt6_enabled(builder)


def _submodules(builder) -> list[str]:
    configured = list(builder.config.global_cfg.qt6_modules)
    if not configured:
        configured = ["qtbase"]
    if "qtbase" not in configured:
        configured.insert(0, "qtbase")
    if builder.platform.os != "linux":
        configured = [name for name in configured if name != "qtwayland"]
    return configured


def _submodule_initialized(src_dir: Path, name: str) -> bool:
    path = src_dir / name
    if not path.is_dir():
        return False
    # A non-initialized git submodule usually exists as an empty directory.
    if (path / ".git").exists():
        return True
    if (path / "CMakeLists.txt").exists():
        return True
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    return True


def _prepare_sources(builder, src_dir: Path) -> None:
    qt_submodules = _submodules(builder)
    missing_submodules = [name for name in qt_submodules if not _submodule_initialized(src_dir, name)]
    if not missing_submodules:
        return

    init_repo = src_dir / ("init-repository.bat" if builder.platform.os == "windows" else "init-repository")
    if not init_repo.exists():
        return

    banner("Qt6 - init submodules")
    if builder.platform.os == "windows":
        init_cmd = [
            "cmd",
            "/c",
            str(init_repo),
            f"--module-subset={','.join(qt_submodules)}",
            "--no-optional-deps",
        ]
    else:
        init_cmd = [
            "sh",
            str(init_repo),
            f"--module-subset={','.join(qt_submodules)}",
            "--no-optional-deps",
        ]
    if builder.no_update:
        # We still need to fetch at least once to clone missing Qt submodules.
        # `init-repository --no-fetch` would prevent bringing in new submodules.
        print(
            "[note] Qt6: missing submodules require fetching; ignoring no_update for init-repository.",
            flush=True,
        )
    print_cmd("init-repository command", init_cmd)
    run(
        init_cmd,
        cwd=str(src_dir),
        env=builder._source_prep_env(),
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path("Qt6", "_shared", "init-submodules")),
    )


def patch_source(builder, src_dir) -> None:
    _prepare_sources(builder, src_dir)


def build(builder, ctx, env: dict[str, str]) -> None:
    self = builder
    repo = ctx.repo
    build_type = ctx.build_type
    build_dir = ctx.build_dir
    install_prefix = ctx.install_prefix
    src_dir = ctx.src_dir

    configure = src_dir / ("configure.bat" if self.platform.os == "windows" else "configure")
    if not configure.exists():
        raise RuntimeError(f"Missing Qt configure script for {repo.name}: {configure}")

    self._ensure_unofficial_brotli_package(install_prefix, build_type)
    self._ensure_freetype_harfbuzz_compat(install_prefix, build_type)
    self._ensure_jasper_package(install_prefix, build_type)

    if not self.dry_run:
        if build_dir.exists():
            shutil.rmtree(build_dir, ignore_errors=True)
        build_dir.mkdir(parents=True, exist_ok=True)
    else:
        build_dir.mkdir(parents=True, exist_ok=True)

    qt_env = dict(env)
    if self.platform.os == "windows":
        sanitized_vars: list[str] = []
        for var in ("CL", "_CL_", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"):
            if qt_env.get(var) or os.environ.get(var):
                qt_env[var] = ""
                sanitized_vars.append(var)
        if sanitized_vars:
            print(
                f"[note] Qt6: cleared inherited compiler env vars: {', '.join(sanitized_vars)}",
                flush=True,
            )

    qt_submodules = _submodules(self)
    qt_submodule_set = set(qt_submodules)

    if self.platform.os == "linux" and "qtwayland" in qt_submodule_set:
        if not shutil.which("wayland-scanner"):
            raise RuntimeError(
                "Qt6: wayland-scanner not found. Install Wayland development tools (wayland-scanner) to build qtwayland."
            )

    pulse_ok = False
    alsa_ok = False
    if self.platform.os == "linux" and "qtmultimedia" in qt_submodule_set:
        pulse_ok = subprocess.run(["pkg-config", "--exists", "libpulse"], env=env, check=False).returncode == 0
        alsa_ok = subprocess.run(["pkg-config", "--exists", "alsa"], env=env, check=False).returncode == 0
        if ffmpeg_enabled(self) and not pulse_ok and not alsa_ok:
            print(
                "[note] Qt6: neither libpulse nor alsa dev packages were found via pkg-config. "
                "QtMultimedia audio backends may be limited.",
                flush=True,
            )

    qt_args: list[str] = [
        "-prefix",
        self._cmake_path_arg(install_prefix),
        "-extprefix",
        self._cmake_path_arg(install_prefix),
        "-opensource",
        "-confirm-license",
        "-static",
        "-nomake",
        "tests",
        "-nomake",
        "examples",
        "-cmake-generator",
        "Ninja",
        "-submodules",
        ",".join(qt_submodules),
        "-system-pcre",
        "-system-zlib",
        "-system-freetype",
        "-system-harfbuzz",
        "-system-libpng",
        "-system-libjpeg",
    ]
    if "qtimageformats" in qt_submodule_set:
        qt_args.extend(["-system-tiff", "-system-webp"])
    if "qtmultimedia" in qt_submodule_set:
        qt_args.extend(["-no-feature-gstreamer", "-no-feature-pipewire"])

    if build_type == "Debug":
        qt_args.append("-debug")
    else:
        qt_args.append("-release")

    if self.platform.os in {"linux", "macos"}:
        qt_args.extend(["-opengl", "desktop"])

    if self.platform.os == "linux":
        linux_qpa = "xcb;wayland" if "qtwayland" in qt_submodule_set else "xcb"
        qt_args.extend(["-qpa", linux_qpa, "-default-qpa", "xcb"])
        qt_args.extend(["-no-gtk", "-no-dbus", "-no-glib"])

    if self.platform.os in {"linux", "windows"}:
        qt_args.append("-openssl-linked")
    if self.platform.os == "windows":
        qt_args.extend(["-static-runtime", "-no-schannel"])

    if "qtmultimedia" in qt_submodule_set and ffmpeg_enabled(self):
        if self.platform.os == "linux":
            if pulse_ok:
                qt_args.append("-feature-ffmpeg")
            else:
                print(
                    "[note] Qt6: libpulse dev package not found via pkg-config; "
                    "QtMultimedia FFmpeg backend cannot be enabled on Linux. "
                    "Install libpulse development files to enable FFmpeg, or disable FFmpeg for QtMultimedia.",
                    flush=True,
                )
        elif self.platform.os != "windows":
            qt_args.append("-feature-ffmpeg")

    cmake_args: list[str] = [
        f"-DCMAKE_BUILD_TYPE={build_type}",
        "-DCMAKE_FIND_PACKAGE_TARGETS_GLOBAL=TRUE",
        f"-DCMAKE_PREFIX_PATH={self._cmake_path_arg(install_prefix)}",
        f"-DCMAKE_INCLUDE_PATH={self._cmake_path_arg(install_prefix / 'include')}",
        f"-DCMAKE_LIBRARY_PATH={self._cmake_path_arg(install_prefix / 'lib')}",
        "-DPKG_CONFIG_USE_STATIC_LIBS=ON",
    ]
    freetype_dir = install_prefix / "lib" / "cmake" / "freetype"
    if freetype_dir.exists():
        cmake_args.append(f"-DFreetype_DIR={self._cmake_path_arg(freetype_dir)}")
    if self.platform.os == "macos":
        cmake_args.append("-DQT_INTERNAL_XCODE_VERSION=15.0")
    if self.config.global_cfg.pic:
        cmake_args.append("-DCMAKE_POSITION_INDEPENDENT_CODE=ON")

    cflags = self._base_flags(build_type)
    cxxflags = self._base_flags(build_type)
    if self.platform.os == "windows":
        cmake_args.append("-DCMAKE_POLICY_DEFAULT_CMP0091=NEW")
        cmake_args.extend(build_backends.cmake_make_program_args(self, force_windows_ninja=True))
        runtime_mode = self._windows_runtime_mode()
        if runtime_mode == "static":
            runtime = "MultiThreadedDebug" if build_type == "Debug" else "MultiThreaded"
        elif runtime_mode == "dynamic":
            runtime = "MultiThreadedDebugDLL" if build_type == "Debug" else "MultiThreadedDLL"
        else:
            runtime = str(self.config.global_cfg.windows.get("msvc_runtime"))
        cmake_args.append(f"-DCMAKE_MSVC_RUNTIME_LIBRARY={runtime}")
        cxxflags += " /bigobj"
        cmake_args.append(f"-DOPENSSL_ROOT_DIR={install_prefix}")

        if build_type != "Debug":
            cmake_args.append("-DCMAKE_C_FLAGS_DEBUG=/Od /Zi")
            cmake_args.append("-DCMAKE_CXX_FLAGS_DEBUG=/Od /Zi /bigobj")

        lib_dir = install_prefix / "lib"
        include_dir = install_prefix / "include"

        def _pick_windows_png(debug: bool) -> Path | None:
            debug_candidates = [
                lib_dir / "libpng18_staticd.lib",
                lib_dir / "libpng16_staticd.lib",
                lib_dir / "libpngd.lib",
                lib_dir / "pngd.lib",
                lib_dir / "libpng18d.lib",
                lib_dir / "libpng16d.lib",
            ]
            release_candidates = [
                lib_dir / "libpng18_static.lib",
                lib_dir / "libpng16_static.lib",
                lib_dir / "libpng.lib",
                lib_dir / "png.lib",
                lib_dir / "libpng18.lib",
                lib_dir / "libpng16.lib",
            ]
            candidates = debug_candidates if debug else release_candidates
            for candidate in candidates:
                if candidate.exists():
                    return candidate
            matches: list[Path] = []
            if debug:
                for pattern in ("libpng*d*.lib", "png*d*.lib"):
                    matches.extend(sorted(lib_dir.glob(pattern)))
            else:
                for pattern in ("libpng*.lib", "png*.lib"):
                    matches.extend(sorted(lib_dir.glob(pattern)))
            return matches[0] if matches else None

        png_debug = _pick_windows_png(debug=True)
        png_release = _pick_windows_png(debug=False)
        if png_debug is None:
            png_debug = png_release
        if png_release is None:
            png_release = png_debug
        if png_debug is not None:
            cmake_args.append(f"-DPNG_LIBRARY_DEBUG={png_debug}")
        if png_release is not None:
            cmake_args.append(f"-DPNG_LIBRARY_RELEASE={png_release}")
        if (include_dir / "png.h").exists():
            cmake_args.append(f"-DPNG_PNG_INCLUDE_DIR={include_dir}")
    if self.platform.os in {"macos", "linux"} and self.config.global_cfg.use_libcxx:
        cxxflags += " -stdlib=libc++"
    if self.platform.os == "windows":
        cmake_args.append(f"-DCMAKE_C_FLAGS_INIT={cflags}")
        cmake_args.append(f"-DCMAKE_CXX_FLAGS_INIT={cxxflags}")
    else:
        if cflags:
            qt_env["CFLAGS"] = cflags
        if cxxflags:
            qt_env["CXXFLAGS"] = cxxflags

    linker_flags = self._linker_flags_init()
    if linker_flags:
        cmake_args += [
            f"-DCMAKE_EXE_LINKER_FLAGS_INIT={linker_flags}",
            f"-DCMAKE_SHARED_LINKER_FLAGS_INIT={linker_flags}",
            f"-DCMAKE_MODULE_LINKER_FLAGS_INIT={linker_flags}",
        ]

    if self.toolchain and (self.platform.os != "windows" or self._windows_should_pin_cmake_compiler()):
        if "cc" in self.toolchain:
            cmake_args.append(f"-DCMAKE_C_COMPILER={self._cmake_path_arg(self.toolchain['cc'])}")
        if "cxx" in self.toolchain:
            cmake_args.append(f"-DCMAKE_CXX_COMPILER={self._cmake_path_arg(self.toolchain['cxx'])}")
        if "ld" in self.toolchain:
            cmake_args.append(f"-DCMAKE_LINKER={self._cmake_path_arg(self.toolchain['ld'])}")
        if "ar" in self.toolchain:
            cmake_args.append(f"-DCMAKE_AR={self._cmake_path_arg(self.toolchain['ar'])}")
        if "ranlib" in self.toolchain:
            cmake_args.append(f"-DCMAKE_RANLIB={self._cmake_path_arg(self.toolchain['ranlib'])}")

    if self.platform.os != "windows" and "qtmultimedia" in qt_submodule_set and ffmpeg_enabled(self):
        cmake_args.append(f"-DFFMPEG_DIR={install_prefix}")
    if self.platform.os == "linux" and self.config.global_cfg.use_libcxx:
        cmake_args.append("-DFEATURE_icu=OFF")
    if "qttools" in qt_submodule_set:
        cmake_args.extend(
            [
                "-DFEATURE_clang=OFF",
                "-DFEATURE_clangcpp=OFF",
                "-DFEATURE_qdoc=OFF",
            ]
        )

    full_cmd: list[str] = ["cmd", "/c", str(configure)] if self.platform.os == "windows" else [str(configure)]
    full_cmd.extend(qt_args)
    full_cmd.append("--")
    full_cmd.extend(cmake_args)

    print_cmd("configure command", full_cmd)
    banner(f"{repo.name} ({build_type}) - configure")
    run(
        full_cmd,
        cwd=str(build_dir),
        env=qt_env,
        dry_run=self.dry_run,
        log_path=str(self._repo_log_path(repo.name, build_type, "configure")),
    )
    if not self.dry_run:
        cache = build_dir / "CMakeCache.txt"
        if not cache.exists():
            raise RuntimeError(
                "Qt6: configure finished without generating CMakeCache.txt. "
                "This commonly means required git submodules were not initialized; "
                "re-run and allow -init-submodules to populate qtbase/qtdeclarative/etc."
            )
        generator = "Ninja"
        try:
            for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("CMAKE_GENERATOR:"):
                    generator = line.split("=", 1)[1].strip() or generator
                    break
        except OSError:
            pass

        generator_lower = generator.lower()
        if "ninja" in generator_lower:
            expected = build_dir / "build.ninja"
            if not expected.exists():
                raise RuntimeError(
                    "Qt6: configure did not generate build.ninja. "
                    "Configuration likely failed even if configure.bat returned success. "
                    f"Check: {self._repo_log_path(repo.name, build_type, 'configure')}"
                )
        elif "visual studio" in generator_lower:
            expected = build_dir / "Qt6.sln"
            if not expected.exists():
                if not any(build_dir.glob("*.sln")):
                    raise RuntimeError(
                        "Qt6: configure did not generate a Visual Studio solution. "
                        "Configuration likely failed even if configure.bat returned success. "
                        f"Check: {self._repo_log_path(repo.name, build_type, 'configure')}"
                    )

    build_cmd = ["cmake", "--build", str(build_dir), "--config", build_type, "--parallel", str(self._jobs())]
    print_cmd("build command", build_cmd)
    banner(f"{repo.name} ({build_type}) - building")
    run(
        build_cmd,
        cwd=str(build_dir),
        env=qt_env,
        dry_run=self.dry_run,
        log_path=str(self._repo_log_path(repo.name, build_type, "build")),
    )

    install_cmd = ["cmake", "--install", str(build_dir), "--config", build_type, "--prefix", str(install_prefix)]
    print_cmd("install command", install_cmd)
    banner(f"{repo.name} ({build_type}) - install")
    run(
        install_cmd,
        cwd=str(build_dir),
        env=qt_env,
        dry_run=self.dry_run,
        log_path=str(self._repo_log_path(repo.name, build_type, "install")),
    )


def stamp_payload(builder, _repo, ctx, payload: dict) -> None:
    qt_submodules = _submodules(builder)
    qt_submodule_set = set(qt_submodules)
    system_libs = {
        "pcre": "system",
        "zlib": "system",
        "freetype": "system",
        "harfbuzz": "system",
        "libpng": "system",
        "libjpeg": "system",
    }
    if "qtimageformats" in qt_submodule_set:
        system_libs["tiff"] = "system"
        system_libs["webp"] = "system"
    disabled_features = ["gstreamer", "pipewire"] if "qtmultimedia" in qt_submodule_set else []
    if builder.platform.os == "linux":
        disabled_features.extend(["dbus", "glib"])
    payload["qt6"] = {
        "submodules": qt_submodules,
        "mode": "debug" if ctx.build_type == "Debug" else "release",
        "opengl": "desktop" if builder.platform.os in {"linux", "macos"} else "default",
        "qpa": (
            "xcb;wayland"
            if builder.platform.os == "linux" and "qtwayland" in qt_submodule_set
            else ("xcb" if builder.platform.os == "linux" else "default")
        ),
        "qpa_default": ("xcb" if builder.platform.os == "linux" else "default"),
        "ssl": ("openssl-linked" if builder.platform.os in {"linux", "windows"} else "default"),
        "static_runtime": (builder.platform.os == "windows"),
        "system_libs": system_libs,
        "disabled_features": sorted(disabled_features),
        "feature_ffmpeg": (
            builder.platform.os != "windows" and "qtmultimedia" in qt_submodule_set and ffmpeg_enabled(builder)
        ),
        "pkg_config_use_static_libs": True,
    }
