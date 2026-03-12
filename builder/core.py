from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import threading

from .config import Config, RepoConfig
from .git_ops import ensure_repo, git_head
from .platform import PlatformInfo
from .recipes import registry as recipe_registry
from .repo_options import CMakeOptions, load_repo_defaults, load_user_overrides, render_cmake_options
from .runner import banner, print_cmd, run, set_output_lock
from .stamps import compute_stamp, read_stamp, write_stamp
from .topo import topo_sort


def _normalize_override(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"\"", "'"}:
        trimmed = trimmed[1:-1]
    return trimmed or None


def _resolve_executable_candidate(candidate: str | None, *, search_path: str | None = None) -> str | None:
    normalized = _normalize_override(candidate)
    if not normalized:
        return None

    if any(sep in normalized for sep in ("/", "\\")):
        expanded = os.path.expandvars(os.path.expanduser(normalized))
        path = Path(expanded)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None

    return shutil.which(normalized, path=search_path)


def _windows_nasm_probe_candidates(env: Mapping[str, str] | None = None) -> list[str]:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        normalized = _normalize_override(value)
        if not normalized:
            return
        key = os.path.normcase(os.path.normpath(normalized.strip("\"'")))
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    for env_name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = merged_env.get(env_name)
        if not base:
            continue
        add(str(Path(base) / "NASM" / "nasm.exe"))

    add(r"C:\Program Files\NASM\nasm.exe")
    add(r"C:\Program Files (x86)\NASM\nasm.exe")
    return candidates


def resolve_nasm_executable(env: Mapping[str, str] | None = None, *, platform_os: str | None = None) -> str | None:
    search_path = None
    if env is not None:
        search_path = env.get("PATH") or os.environ.get("PATH", "")

    override_names = [
        "NASM_EXECUTABLE",
        "CMAKE_ASM_NASM_COMPILER",
        "NASM",
        "YASM_EXECUTABLE",
        "YASM",
    ]
    for name in override_names:
        value = env.get(name) if env is not None else os.environ.get(name)
        resolved = _resolve_executable_candidate(value, search_path=search_path)
        if resolved:
            return resolved

    for candidate in ("nasm", "yasm"):
        resolved = _resolve_executable_candidate(candidate, search_path=search_path)
        if resolved:
            return resolved

    if platform_os == "windows":
        for candidate in _windows_nasm_probe_candidates(env):
            resolved = _resolve_executable_candidate(candidate, search_path=search_path)
            if resolved:
                return resolved

    return None


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
        self._lock = threading.Lock()

    def record(self, build_type: str, repo: str, status: str, detail: str = "") -> None:
        with self._lock:
            self.entries[(build_type, repo)] = (status, detail)

    def render(self) -> str:
        with self._lock:
            entries = dict(self.entries)
        lines = ["", "=== Build Report ==="]
        for build_type in self.build_types:
            lines.append(f"{build_type}:")
            for repo in self.order:
                entry = entries.get((build_type, repo))
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
    def __init__(
        self,
        config: Config,
        platform: PlatformInfo,
        dry_run: bool,
        no_update: bool,
        force: bool,
        force_all: bool = False,
        reinstall: bool = False,
        reinstall_all: bool = False,
        parallel_build_types: bool = False,
    ) -> None:
        self.config = config
        self.platform = platform
        self.dry_run = dry_run
        self.no_update = no_update
        self.force = force
        self.force_all = force_all or (force and not bool(config.only))
        self.reinstall = reinstall
        self.reinstall_all = reinstall_all or (reinstall and not bool(config.only))
        self.force_targets: set[str] = set()
        self.reinstall_targets: set[str] = set()
        self.toolchain = self._resolve_toolchain()
        self._ccache_path = self._resolve_ccache()
        self.repos = self._filter_repos()
        self._apply_dynamic_repo_overrides()
        if force and bool(self.config.only) and not self.force_all:
            self.force_targets = set(self.config.only)
        if reinstall and bool(self.config.only) and not self.reinstall_all:
            self.reinstall_targets = set(self.config.only)
        self.prefixes = self._compute_prefixes()
        self.repo_paths: dict[str, Path] = {}
        self.pkg_override_root = self.config.global_cfg.build_root / "pkgconfig_override"
        self._ocio_python_note_printed = False
        self._openexr_python_note_printed = False
        self._windows_python_wrappers_forced_on_note_printed = False
        self._repo_defaults_dir = Path(__file__).resolve().parent / "recipes" / "defaults"
        self._repo_cmake_defaults = load_repo_defaults(self._repo_defaults_dir)
        self._user_overrides_path = self.config.global_cfg.repo_root / "build.user.toml"
        self._repo_cmake_user_overrides = load_user_overrides(self._user_overrides_path)
        self._validate_user_overrides()

        self.parallel_build_types = parallel_build_types
        if self.parallel_build_types and self.platform.os == "windows":
            raise SystemExit("--parallel-build-types is supported only on macOS/Linux.")
        self._parallel_build_type_count = 1
        self._output_lock = threading.Lock() if self.parallel_build_types else None
        set_output_lock(self._output_lock)
        self._repo_source_prepared: set[str] = set()
        self._repo_source_prepare_locks: dict[str, threading.Lock] = {repo.name: threading.Lock() for repo in self.repos}
        self._repo_exclusive_build_locks: dict[str, threading.Lock] = {repo.name: threading.Lock() for repo in self.repos}

    def _repo_log_path(self, repo_name: str, build_type: str, step: str) -> Path:
        safe_step = re.sub(r"[^A-Za-z0-9._-]+", "_", step).strip("._")
        if not safe_step:
            safe_step = "command"
        return self.config.global_cfg.build_root / ".logs" / repo_name / build_type / f"{safe_step}.log"

    def _validate_user_overrides(self) -> None:
        if not self._repo_cmake_user_overrides:
            return
        known = {repo.name for repo in self.config.repos}
        unknown = sorted(name for name in self._repo_cmake_user_overrides.keys() if name not in known)
        if unknown:
            names_str = ", ".join(unknown)
            raise SystemExit(f"Unknown repo name(s) in {self._user_overrides_path.name}: {names_str}")

    def _repo_cmake_defaults_args(self, repo_name: str) -> list[str]:
        defaults = self._repo_cmake_defaults.get(repo_name)
        if defaults is None:
            return []
        return render_cmake_options(defaults.resolve(self.platform.os))

    def _repo_cmake_user_override_args(self, repo_name: str) -> list[str]:
        overrides = self._repo_cmake_user_overrides.get(repo_name)
        if overrides is None:
            return []
        return render_cmake_options(overrides.resolve(self.platform.os))

    def _repo_cmake_effective_toml_options(self, repo_name: str) -> CMakeOptions:
        options = CMakeOptions()
        defaults = self._repo_cmake_defaults.get(repo_name)
        if defaults is not None:
            options = options.merged(defaults.resolve(self.platform.os))
        overrides = self._repo_cmake_user_overrides.get(repo_name)
        if overrides is not None:
            options = options.merged(overrides.resolve(self.platform.os))
        return options

    def _reinstall_requested(self, repo_name: str) -> bool:
        if not (self.reinstall or self.reinstall_all):
            return False
        if self.reinstall_all:
            return True
        if self.reinstall_targets:
            return repo_name in self.reinstall_targets
        return False

    def _install_marker_path(self, install_prefix: Path, repo_name: str, build_type: str) -> Path:
        return install_prefix / ".oiio-builder" / "install-stamps" / repo_name / f"{build_type}.json"

    def _read_install_marker(self, path: Path) -> dict | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _install_marker_matches(self, repo: RepoConfig, ctx: BuildContext, build_stamp: str) -> bool:
        path = self._install_marker_path(ctx.install_prefix, repo.name, ctx.build_type)
        marker = self._read_install_marker(path)
        if not marker:
            return False
        if marker.get("build_stamp") != build_stamp:
            return False
        marker_prefix = marker.get("install_prefix")
        if isinstance(marker_prefix, str) and marker_prefix.strip():
            marker_norm = os.path.normcase(os.path.normpath(marker_prefix))
            desired_norm = os.path.normcase(os.path.normpath(str(ctx.install_prefix)))
            return marker_norm == desired_norm
        return False

    def _write_install_marker(self, repo: RepoConfig, ctx: BuildContext, build_stamp: str) -> None:
        path = self._install_marker_path(ctx.install_prefix, repo.name, ctx.build_type)
        payload = {
            "repo": repo.name,
            "build_type": ctx.build_type,
            "build_stamp": build_stamp,
            "build_system": repo.build_system,
            "install_prefix": str(ctx.install_prefix),
            "build_dir": str(ctx.build_dir),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _filter_repos(self) -> list[RepoConfig]:
        configured_repos = [r for r in self.config.repos if r.enabled]
        by_name_configured = {repo.name: repo for repo in configured_repos}
        by_lower_configured: dict[str, list[str]] = {}
        for repo in configured_repos:
            by_lower_configured.setdefault(repo.name.lower(), []).append(repo.name)

        def resolve_user_repo_names(names: set[str], opt: str) -> set[str]:
            resolved: set[str] = set()
            unknown: list[str] = []
            ambiguous: list[tuple[str, list[str]]] = []

            for name in names:
                if name in by_name_configured:
                    resolved.add(name)
                    continue
                matches = by_lower_configured.get(name.lower(), [])
                if len(matches) == 1:
                    resolved.add(matches[0])
                elif len(matches) > 1:
                    ambiguous.append((name, matches))
                else:
                    unknown.append(name)

            if ambiguous:
                lines = [
                    f"Ambiguous repo name '{name}' in {opt}: matches {', '.join(matches)}"
                    for name, matches in ambiguous
                ]
                lines.append("Use exact names as shown by: uv run build.py --list-repos")
                raise SystemExit("\n".join(lines))
            if unknown:
                names_str = ", ".join(sorted(unknown))
                raise SystemExit(f"Unknown repo name(s) in {opt}: {names_str}\nUse: uv run build.py --list-repos")
            return resolved

        repos = list(configured_repos)

        def enabled(repo: RepoConfig) -> bool:
            decision = recipe_registry.enabled(repo.name, self, repo)
            if decision is not None:
                return decision
            return True

        repos = [r for r in repos if enabled(r)]

        if self.config.only:
            explicit = resolve_user_repo_names(set(self.config.only), "--only")
            self.config.only = set(explicit)
            selected = set(explicit)
            by_name = by_name_configured
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

            enabled_names = {repo.name for repo in repos}
            disabled_explicit = sorted(name for name in explicit if name not in enabled_names)
            if disabled_explicit:
                names_str = ", ".join(disabled_explicit)
                raise SystemExit(f"Repo(s) requested by --only are disabled by config/toggles: {names_str}")
            repos = [r for r in repos if r.name in selected]
        if self.config.skip:
            skip = resolve_user_repo_names(set(self.config.skip), "--skip")
            self.config.skip = set(skip)
            repos = [r for r in repos if r.name not in skip]
        return repos

    def _apply_dynamic_repo_overrides(self) -> None:
        cpython_ref, cpython_ref_type = self._cpython_ref_override()
        if not cpython_ref:
            return
        for repo in self.repos:
            if repo.name != "cpython":
                continue
            repo.ref = cpython_ref
            repo.ref_type = cpython_ref_type

    def _cpython_ref_override(self) -> tuple[str | None, str]:
        cfg = self.config.global_cfg
        ref = getattr(cfg, "cpython_ref", None)
        if isinstance(ref, str):
            ref = ref.strip() or None
        else:
            ref = None
        ref_type = str(getattr(cfg, "cpython_ref_type", "branch")).strip().lower() or "branch"
        if ref_type not in {"branch", "tag", "commit"}:
            ref_type = "branch"
        return ref, ref_type

    def _cpython_enabled_for_run(self) -> bool:
        return any(repo.name == "cpython" for repo in self.repos)

    def _prefix_python_executable(self, prefix: Path, build_type: str) -> Path | None:
        if self.platform.os == "windows":
            debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
            if build_type == "Debug":
                candidates = [
                    prefix / f"python_{debug_postfix}.exe",
                    prefix / f"python{debug_postfix}.exe",
                    prefix / "python.exe",
                    prefix / "bin" / f"python_{debug_postfix}.exe",
                    prefix / "bin" / f"python{debug_postfix}.exe",
                    prefix / "bin" / "python.exe",
                ]
            else:
                candidates = [
                    prefix / "python.exe",
                    prefix / "bin" / "python.exe",
                    prefix / f"python_{debug_postfix}.exe",
                    prefix / f"python{debug_postfix}.exe",
                    prefix / "bin" / f"python_{debug_postfix}.exe",
                    prefix / "bin" / f"python{debug_postfix}.exe",
                ]
        else:
            candidates = [prefix / "bin" / "python3", prefix / "bin" / "python"]
        return next((candidate for candidate in candidates if candidate.exists()), None)

    def _prefix_windows_python_libraries(self, prefix: Path) -> tuple[Path | None, Path | None]:
        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        lib_dirs = [prefix / "libs", prefix / "lib"]
        release_candidates: list[Path] = []
        debug_candidates: list[Path] = []

        for lib_dir in lib_dirs:
            if not lib_dir.exists():
                continue
            for candidate in sorted(lib_dir.glob("python*.lib")):
                name = candidate.name.lower()
                # Keep python3.lib as a low-priority compatibility fallback.
                if name == "python3.lib":
                    release_candidates.append(candidate)
                    continue
                if name.endswith(f"{debug_postfix}.lib") or name.endswith(f"_{debug_postfix}.lib"):
                    debug_candidates.append(candidate)
                else:
                    release_candidates.append(candidate)

        def _priority(path: Path) -> tuple[int, str]:
            stem = path.stem.lower()
            # Prefer versioned libs (python312.lib / python312_d.lib) over
            # generic import libs (python3.lib / python3_d.lib).
            if re.fullmatch(r"python\d{2,}(_[a-z])?", stem):
                return 0, path.name.lower()
            if stem.startswith("python3"):
                return 2, path.name.lower()
            return 1, path.name.lower()

        release_candidates.sort(key=_priority)
        debug_candidates.sort(key=_priority)

        release_lib = release_candidates[0] if release_candidates else None
        debug_lib = debug_candidates[0] if debug_candidates else None
        if debug_lib is None:
            debug_lib = release_lib
        if release_lib is None:
            release_lib = debug_lib
        return release_lib, debug_lib

    def _compute_prefixes(self) -> dict[str, Path]:
        cfg = self.config.global_cfg
        prefixes: dict[str, Path] = {}

        def _resolve_prefix(raw: str) -> Path:
            expanded = os.path.expanduser(os.path.expandvars(str(raw)))
            path = Path(expanded)
            if not path.is_absolute():
                path = (cfg.repo_root / path).resolve()
            return path

        if self.platform.os == "windows":
            layout = str(getattr(cfg, "prefix_layout", "suffix")).strip().lower()
            if layout == "by-build-type":
                base_raw = cfg.install_prefix or cfg.prefix_base
                if not base_raw:
                    base_raw = str(cfg.repo_root / "developer" / "install")
                base_path = _resolve_prefix(str(base_raw))
                prefixes["Release"] = base_path
                prefixes["Debug"] = base_path
                if "ASAN" in cfg.build_types:
                    asan_raw = cfg.asan_prefix
                    if asan_raw:
                        asan_path = _resolve_prefix(str(asan_raw))
                    else:
                        if base_path.name.lower() == "install":
                            asan_path = base_path.parent / "asan"
                        else:
                            asan_path = Path(f"{base_path}_ASAN")
                    prefixes["ASAN"] = asan_path
                return prefixes

            base = cfg.install_prefix or cfg.prefix_base
            if not base:
                base = str(cfg.repo_root / "_install" / "WIN")
            base_path = _resolve_prefix(str(base))
            prefixes["Release"] = base_path
            prefixes["Debug"] = base_path
            if "ASAN" in cfg.build_types:
                asan_base = cfg.asan_prefix
                if not asan_base:
                    asan_base = f"{base_path}_ASAN"
                asan_path = _resolve_prefix(str(asan_base))
                prefixes["ASAN"] = asan_path
            return prefixes

        layout = str(getattr(cfg, "prefix_layout", "suffix")).strip().lower()
        if layout == "by-build-type":
            root = cfg.install_prefix or cfg.prefix_base or str(cfg.repo_root / "developer")
            root_path = _resolve_prefix(str(root))
            prefixes["Release"] = root_path / "Release"
            prefixes["Debug"] = root_path / "Debug"
            if cfg.asan_prefix:
                prefixes["ASAN"] = _resolve_prefix(str(cfg.asan_prefix))
            else:
                prefixes["ASAN"] = root_path / "ASAN"
            return prefixes

        base = cfg.install_prefix or cfg.prefix_base
        if not base:
            base = str(cfg.repo_root / "_install" / "UBS")
        base_path = _resolve_prefix(str(base))
        prefixes["Release"] = base_path
        prefixes["Debug"] = Path(f"{base_path}{cfg.debug_suffix}")
        if cfg.asan_prefix:
            prefixes["ASAN"] = _resolve_prefix(str(cfg.asan_prefix))
        else:
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

    @staticmethod
    def _which_in_env(name: str, env: dict[str, str]) -> str | None:
        search_path = env.get("PATH") or os.environ.get("PATH", "")
        return shutil.which(name, path=search_path)

    def _resolve_ccache(self) -> str | None:
        cfg = self.config.global_cfg
        if self.platform.os == "windows":
            return None
        if not cfg.use_ccache:
            return None
        disabled = cfg.env.get("CCACHE_DISABLE") or os.environ.get("CCACHE_DISABLE")
        if disabled and str(disabled).strip().lower() in {"1", "true", "yes", "on"}:
            return None
        return shutil.which("ccache")

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

    def _windows_msys2_detected(self) -> bool:
        if self.platform.os != "windows":
            return False
        cfg = self.config.global_cfg
        candidates = [
            cfg.windows_env.get("MSYSTEM"),
            cfg.env.get("MSYSTEM"),
            os.environ.get("MSYSTEM"),
            cfg.windows_env.get("MSYSTEM_PREFIX"),
            cfg.env.get("MSYSTEM_PREFIX"),
            os.environ.get("MSYSTEM_PREFIX"),
            cfg.windows_env.get("MINGW_PREFIX"),
            cfg.env.get("MINGW_PREFIX"),
            os.environ.get("MINGW_PREFIX"),
        ]
        for value in candidates:
            if isinstance(value, str) and value.strip():
                return True

        ostype = str(cfg.windows_env.get("OSTYPE") or cfg.env.get("OSTYPE") or os.environ.get("OSTYPE") or "").strip().lower()
        return "msys" in ostype or "mingw" in ostype

    def _windows_ffmpeg_native_build_enabled(self) -> bool:
        return self.platform.os == "windows" and self._ffmpeg_enabled() and self._windows_msys2_detected()

    def _ensure_ffmpeg_posix_line_endings(self, src_dir: Path) -> None:
        if self.platform.os == "windows" and not self._windows_ffmpeg_native_build_enabled():
            return

        probe_paths = [
            src_dir / "configure",
            src_dir / "libavcodec" / "bitstream_filters.c",
            src_dir / "libavcodec" / "allcodecs.c",
            src_dir / "libavcodec" / "Makefile",
        ]
        files_with_cr: list[Path] = []
        checked = 0
        for probe in probe_paths:
            if not probe.exists():
                continue
            checked += 1
            if b"\r" in probe.read_bytes():
                files_with_cr.append(probe)

        if checked == 0 or not files_with_cr:
            return

        rel_paths = [str(path.relative_to(src_dir)) for path in files_with_cr]
        preview = ", ".join(rel_paths[:3])
        if len(rel_paths) > 3:
            preview += f", +{len(rel_paths) - 3} more"

        fix_cmds = [
            f"git -C {src_dir} config core.autocrlf false",
            f"git -C {src_dir} config core.eol lf",
            f"git -C {src_dir} reset --hard HEAD",
        ]
        fix_block = "\n".join(f"  {cmd}" for cmd in fix_cmds)
        raise RuntimeError(
            "FFmpeg checkout uses CRLF line endings on a POSIX host. "
            "This breaks configure (symptom: eval: ...\\r=yes: not found).\n"
            f"Detected in: {preview}\n"
            "Normalize line endings and retry:\n"
            f"{fix_block}"
        )

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

        if self._ccache_path:
            fallback_cache_dir = self.config.global_cfg.build_root / ".ccache"
            fallback_tmp_dir = self.config.global_cfg.build_root / ".ccache-tmp"

            def _ensure_writable(path: Path) -> bool:
                try:
                    path.mkdir(parents=True, exist_ok=True)
                    probe = path / ".oiio_builder_probe"
                    probe.write_text("ok", encoding="utf-8")
                    probe.unlink()
                    return True
                except OSError:
                    return False

            def _normalize_env_path(value: str) -> Path:
                expanded = os.path.expanduser(os.path.expandvars(value.strip()))
                path = Path(expanded)
                if not path.is_absolute():
                    path = (self.config.global_cfg.repo_root / path).resolve()
                return path

            ccache_tmp_raw = env.get("CCACHE_TEMPDIR") or os.environ.get("CCACHE_TEMPDIR")
            if ccache_tmp_raw:
                ccache_tmp = _normalize_env_path(str(ccache_tmp_raw))
                if _ensure_writable(ccache_tmp):
                    env["CCACHE_TEMPDIR"] = str(ccache_tmp)
                elif _ensure_writable(fallback_tmp_dir):
                    env["CCACHE_TEMPDIR"] = str(fallback_tmp_dir)
                else:
                    env["CCACHE_DISABLE"] = "1"

            ccache_dir_raw = env.get("CCACHE_DIR") or os.environ.get("CCACHE_DIR")
            if ccache_dir_raw:
                ccache_dir = _normalize_env_path(str(ccache_dir_raw))
                if _ensure_writable(ccache_dir):
                    env["CCACHE_DIR"] = str(ccache_dir)
                elif _ensure_writable(fallback_cache_dir):
                    env["CCACHE_DIR"] = str(fallback_cache_dir)
                else:
                    env["CCACHE_DISABLE"] = "1"

        if self.platform.os == "windows":
            effective_env = dict(os.environ)
            effective_env.update(env)
            nasm = resolve_nasm_executable(effective_env, platform_os="windows")
            if nasm:
                env.setdefault("PATH", os.environ.get("PATH", ""))
                self._prepend_windows_env_paths(env, "PATH", [Path(nasm).parent])

        return env

    def _env_for_repo_build(self, repo: RepoConfig, build_type: str, prefix: Path) -> dict[str, str]:
        env = self._env_for_build(build_type, prefix)

        # nativefiledialog-extended (GTK/dbus) should use the system pkg-config
        # graph. Mixing it with the builder prefix can break resolution when
        # the system GTK stack pins exact harfbuzz submodule versions.
        if self.platform.os == "linux" and repo.name == "nativefiledialog-extended":
            override_dir = self.pkg_override_root / build_type
            remove_norm = {
                os.path.normcase(os.path.normpath(str(override_dir))),
                os.path.normcase(os.path.normpath(str(prefix / "lib" / "pkgconfig"))),
                os.path.normcase(os.path.normpath(str(prefix / "share" / "pkgconfig"))),
            }
            current = env.get("PKG_CONFIG_PATH", "")
            if current:
                items = current.split(os.pathsep)
            else:
                items = []
            kept: list[str] = []
            seen: set[str] = set()
            for item in items:
                if not item:
                    continue
                normalized = os.path.normcase(os.path.normpath(item))
                if normalized in remove_norm:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                kept.append(item)
            if kept:
                env["PKG_CONFIG_PATH"] = os.pathsep.join(kept)
            else:
                env.pop("PKG_CONFIG_PATH", None)

        return env

    def _prepend_windows_env_paths(self, env: dict[str, str], key: str, paths: list[Path | str]) -> None:
        if self.platform.os != "windows":
            return

        sep = ";"
        merged: list[str] = []
        for path_item in paths:
            item = str(path_item).strip()
            if item:
                merged.append(item)
        if key in env:
            existing = env.get(key) or ""
        else:
            existing = ""
        if existing:
            merged.extend(existing.split(sep))

        deduped: list[str] = []
        seen: set[str] = set()
        for raw in merged:
            value = raw.strip()
            if not value:
                continue
            norm = os.path.normcase(os.path.normpath(value.strip("\"'")))
            if norm in seen:
                continue
            seen.add(norm)
            deduped.append(value)
        if deduped:
            env[key] = sep.join(deduped)

    def _windows_runtime_mode(self) -> str:
        mode = str(self.config.global_cfg.windows.get("msvc_runtime", "static")).strip().lower()
        if mode in {"", "static", "mt", "multithreaded"}:
            return "static"
        if mode in {"dynamic", "md", "multithreadeddll"}:
            return "dynamic"
        return mode

    def _ffmpeg_enabled(self) -> bool:
        cfg = self.config.global_cfg
        enabled = bool(cfg.build_ffmpeg)
        if self.platform.os == "windows":
            override = cfg.windows.get("build_ffmpeg")
            if override is None:
                return enabled
            if isinstance(override, str):
                value = override.strip().lower()
                if value in {"0", "false", "off", "no"}:
                    return False
                if value in {"1", "true", "on", "yes"}:
                    return True
            return bool(override)
        return enabled

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

    def _windows_cpython_fetch_externals(self) -> bool:
        if self.platform.os != "windows":
            return False
        raw = self.config.global_cfg.windows.get("cpython_fetch_externals")
        if raw is None:
            return True
        if isinstance(raw, bool):
            return raw
        value = str(raw).strip().lower()
        if value in {"1", "true", "on", "yes"}:
            return True
        if value in {"0", "false", "off", "no"}:
            return False
        return True

    def _base_flags(self, build_type: str) -> str:
        cfg = self.config.global_cfg
        if self.platform.os == "windows":
            generator = str(cfg.windows.get("generator", "ninja-msvc")).strip().lower()
            # clang-cl needs explicit -m* target features for some x86 intrinsics
            # (e.g. SSSE3/SSE4.1) even though it defines _MSC_VER.
            clangcl_extra_flags = ""
            if self.platform.arch == "x86_64" and generator in {"msvc-clang-cl", "ninja-clang-cl"}:
                raw_override = cfg.windows.get("clangcl_extra_flags")
                raw_append = cfg.windows.get("clangcl_extra_flags_append")

                if raw_override is None:
                    raw_override = "-msse4.1"
                if isinstance(raw_override, bool):
                    raw_override = "-msse4.1" if raw_override else ""
                override_str = str(raw_override).strip()

                if isinstance(raw_append, bool):
                    raw_append = ""
                append_str = str(raw_append).strip() if raw_append is not None else ""

                combined = " ".join(s for s in (override_str, append_str) if s)
                if combined:
                    clangcl_extra_flags = f" {combined}"

            runtime_mode = self._windows_runtime_mode()
            runtime_flag = ""
            if runtime_mode == "static":
                runtime_flag = "/MTd" if build_type == "Debug" else "/MT"
            elif runtime_mode == "dynamic":
                runtime_flag = "/MDd" if build_type == "Debug" else "/MD"
            utf8_flag = "/utf-8"
            if build_type == "Debug":
                return f"/Od /Zi {runtime_flag} {utf8_flag}{clangcl_extra_flags}".strip()
            if build_type == "ASAN":
                # MSVC ASAN warns (C5072) when no debug info is emitted. This repo
                # treats warnings as errors for some dependencies (e.g. zlib-ng),
                # so include `/Zi` even for optimized ASAN builds.
                return f"/O2 /DNDEBUG {runtime_flag} {utf8_flag} /Zi{clangcl_extra_flags}".strip()
            return f"/O2 /DNDEBUG {runtime_flag} {utf8_flag}{clangcl_extra_flags}".strip()
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
        args.extend(self._repo_cmake_defaults_args(name))

        recipe_args = recipe_registry.cmake_args(name, self, ctx)
        recipe_applied = recipe_args is not None
        if recipe_applied:
            args.extend(recipe_args)

        if name == "nativefiledialog-extended" and self.platform.os == "linux":
            # Keep system GTK/dbus pkg-config resolution isolated from the builder prefix.
            args.append("-DPKG_CONFIG_USE_CMAKE_PREFIX_PATH=FALSE")

        if name == "libxml2":
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
        elif name == "freetype":
            bz_include = (ctx.install_prefix / "include").resolve()
            lib_dir = (ctx.install_prefix / "lib").resolve()
            bzip2_release: Path | None = None
            bzip2_debug: Path | None = None
            if self.platform.os == "windows":
                debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
                self._ensure_bzip2_alias(ctx.install_prefix, ctx.build_type)
                bzip2_release_candidates = [
                    lib_dir / "bz2_static.lib",
                    lib_dir / "bz2.lib",
                    lib_dir / "libbz2_static.lib",
                    lib_dir / "libbz2.lib",
                ]
                bzip2_debug_candidates = [
                    lib_dir / f"bz2_static{debug_postfix}.lib",
                    lib_dir / f"bz2{debug_postfix}.lib",
                    lib_dir / f"libbz2_static{debug_postfix}.lib",
                    lib_dir / f"libbz2{debug_postfix}.lib",
                    lib_dir / "bz2_static.lib",
                    lib_dir / "bz2.lib",
                ]
                bzip2_release = next((candidate for candidate in bzip2_release_candidates if candidate.exists()), None)
                bzip2_debug = next((candidate for candidate in bzip2_debug_candidates if candidate.exists()), None)
                if bzip2_release is None:
                    matches = sorted(lib_dir.glob("*bz2*.lib"))
                    if matches:
                        bzip2_release = matches[0]
                if bzip2_debug is None:
                    matches = sorted(lib_dir.glob(f"*bz2*{debug_postfix}*.lib"))
                    if matches:
                        bzip2_debug = matches[0]
                    elif bzip2_release is not None:
                        bzip2_debug = bzip2_release
            else:
                bzip2_release_candidates = [
                    lib_dir / "libbz2_static.a",
                    lib_dir / "libbz2.a",
                    lib_dir / "libbz2.so",
                    lib_dir / "libbz2.dylib",
                ]
                bzip2_debug_candidates = [
                    lib_dir / "libbz2_staticd.a",
                    lib_dir / "libbz2d.a",
                    lib_dir / "libbz2_static.a",
                    lib_dir / "libbz2.a",
                ]
                bzip2_release = next((candidate for candidate in bzip2_release_candidates if candidate.exists()), None)
                bzip2_debug = next((candidate for candidate in bzip2_debug_candidates if candidate.exists()), None)
                if bzip2_debug is None and bzip2_release is not None:
                    bzip2_debug = bzip2_release

            if (bz_include / "bzlib.h").exists():
                args.append(f"-DBZIP2_INCLUDE_DIR={bz_include.as_posix()}")
            if bzip2_release is not None:
                args.append(f"-DBZIP2_LIBRARY_RELEASE={bzip2_release.as_posix()}")
            if bzip2_debug is not None:
                args.append(f"-DBZIP2_LIBRARY_DEBUG={bzip2_debug.as_posix()}")
            bzip2_default = bzip2_debug if ctx.build_type == "Debug" else bzip2_release
            if bzip2_default is not None:
                args.append(f"-DBZIP2_LIBRARY={bzip2_default.as_posix()}")
                args.append(f"-DBZIP2_LIBRARIES={bzip2_default.as_posix()}")
            if self.platform.os == "windows":
                debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
                hb_include_candidates = [
                    (ctx.install_prefix / "include" / "harfbuzz").resolve(),
                    (ctx.install_prefix / "include").resolve(),
                ]
                hb_include_dir = next((candidate for candidate in hb_include_candidates if (candidate / "hb.h").exists()), None)

                lib_dir = (ctx.install_prefix / "lib").resolve()
                if ctx.build_type == "Debug":
                    hb_lib_candidates = [
                        lib_dir / f"harfbuzz{debug_postfix}.lib",
                        lib_dir / f"libharfbuzz{debug_postfix}.lib",
                        lib_dir / "harfbuzz.lib",
                        lib_dir / "libharfbuzz.lib",
                    ]
                else:
                    hb_lib_candidates = [
                        lib_dir / "harfbuzz.lib",
                        lib_dir / "libharfbuzz.lib",
                        lib_dir / f"harfbuzz{debug_postfix}.lib",
                        lib_dir / f"libharfbuzz{debug_postfix}.lib",
                    ]
                hb_library = next((candidate for candidate in hb_lib_candidates if candidate.exists()), None)
                if hb_library is None:
                    matches = sorted(lib_dir.glob("*harfbuzz*.lib"))
                    if matches:
                        hb_library = matches[0]

                if hb_include_dir is not None:
                    args.append(f"-DHarfBuzz_INCLUDE_DIR={hb_include_dir.as_posix()}")
                if hb_library is not None:
                    args.append(f"-DHarfBuzz_LIBRARY={hb_library.as_posix()}")
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
                include_path = ctx.build_dir / "oiio_builder_libtiff_defines.cmake"
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
        elif name == "libraw":
            libraw_path = str(self.config.global_cfg.src_root / "LibRaw")
            args += [
                f"-DLIBRAW_PATH={libraw_path}",
                f"-DENABLE_EXAMPLES={cfg.libraw_enable_examples}",
                "-DENABLE_RAWSPEED=OFF",
                f"-DENABLE_OPENMP={cfg.libraw_enable_openmp}",
                "-DENABLE_LCMS=ON",
                "-DENABLE_JASPER=ON",
                f"-DENABLE_DCRAW_DEBUG={'ON' if ctx.build_type == 'Debug' else 'OFF'}",
                "-DENABLE_X3FTOOLS=ON",
                "-DENABLE_6BY9RPI=ON",
            ]
            if self.platform.os == "windows":
                debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
                lib_dir = (ctx.install_prefix / "lib").resolve()
                include_dir = (ctx.install_prefix / "include").resolve()
                if ctx.build_type == "Debug":
                    lcms_names = [
                        f"lcms2_static{debug_postfix}.lib",
                        f"lcms2{debug_postfix}.lib",
                        f"liblcms2{debug_postfix}.lib",
                        f"lcms-2{debug_postfix}.lib",
                        f"liblcms-2{debug_postfix}.lib",
                        "lcms2_static.lib",
                        "lcms2.lib",
                        "liblcms2.lib",
                        "lcms-2.lib",
                        "liblcms-2.lib",
                    ]
                else:
                    lcms_names = [
                        "lcms2_static.lib",
                        "lcms2.lib",
                        "liblcms2.lib",
                        "lcms-2.lib",
                        "liblcms-2.lib",
                        f"lcms2_static{debug_postfix}.lib",
                        f"lcms2{debug_postfix}.lib",
                        f"liblcms2{debug_postfix}.lib",
                        f"lcms-2{debug_postfix}.lib",
                        f"liblcms-2{debug_postfix}.lib",
                    ]
                lcms_lib = next((lib_dir / name for name in lcms_names if (lib_dir / name).exists()), None)
                if lcms_lib is None:
                    patterns: list[str]
                    if ctx.build_type == "Debug":
                        patterns = [
                            f"lcms2*{debug_postfix}.lib",
                            f"liblcms2*{debug_postfix}.lib",
                            "lcms2*.lib",
                            "liblcms2*.lib",
                        ]
                    else:
                        patterns = [
                            "lcms2*.lib",
                            "liblcms2*.lib",
                            f"lcms2*{debug_postfix}.lib",
                            f"liblcms2*{debug_postfix}.lib",
                        ]
                    for pattern in patterns:
                        matches = sorted(lib_dir.glob(pattern))
                        if matches:
                            lcms_lib = matches[0]
                            break
                if lcms_lib is not None and (include_dir / "lcms2.h").exists():
                    # LibRaw ships its own FindLCMS2.cmake which doesn't look for
                    # `lcms2_static`, so force the static library explicitly.
                    args += [
                        f"-DLCMS2_INCLUDE_DIR={include_dir}",
                        f"-DLCMS2_LIBRARIES={lcms_lib}",
                        f"-DLCMS2_LIBRARY={lcms_lib}",
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
        elif name == "lcms2" and not recipe_applied:
            args += [
                "-DBUILD_TESTING=OFF",
                "-DBUILD_TESTS=OFF",
                "-DLCMS2_WITH_TIFF=OFF",
                "-DLCMS2_BUILD_TIFICC=OFF",
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
                "-DCMAKE_SKIP_RPATH=ON",
                "-DCMAKE_SKIP_INSTALL_RPATH=ON",
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
        elif name == "expat":
            if self.platform.os == "windows":
                runtime_mode = str(cfg.windows.get("msvc_runtime", "static")).strip().lower()
                if runtime_mode in {"", "static", "mt", "multithreaded"}:
                    args.append("-DEXPAT_MSVC_STATIC_CRT=ON")
                elif runtime_mode in {"dynamic", "md", "multithreadeddll"}:
                    args.append("-DEXPAT_MSVC_STATIC_CRT=OFF")
        elif name == "OpenColorIO":
            self._ensure_ppmd_package(ctx.install_prefix, ctx.build_type)
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

                # OpenColorIO's Findminizip-ng.cmake may fall back to a library-name search
                # that misses Debug-suffixed names on Windows (e.g. minizip-ngd.lib).
                # Provide explicit hints to keep detection deterministic.
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
        elif name == "OpenImageIO":
            args.extend(self._oiio_cache_args(ctx))

        return args

    def _oiio_cache_args(self, ctx: BuildContext) -> list[str]:
        cfg = self.config.global_cfg
        ffmpeg_enabled = self._ffmpeg_enabled()
        args: list[str] = []
        self._ensure_bzip2_alias(ctx.install_prefix, ctx.build_type)
        self._ensure_ppmd_package(ctx.install_prefix, ctx.build_type)
        self._ensure_freetype_harfbuzz_compat(ctx.install_prefix, ctx.build_type)
        cache_path = cfg.src_root / "OpenImageIO" / "build" / "CMakeCache.txt"
        allow = {
            "BUILD_SHARED_LIBS",
            "EMBEDPLUGINS",
            "OIIO_BUILD_TOOLS",
            "OIIO_BUILD_TESTS",
            "OIIO_IV_EXTRA_IV_LIBRARIES",
            "USE_PYTHON",
            "USE_JXL",
            "USE_FREETYPE",
            "USE_LIBUHDR",
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

        if ffmpeg_enabled:
            values.setdefault("USE_FFMPEG", "ON")

        # Enable ffmpeg plugins whenever the feature is enabled in config.
        values["USE_FFMPEG"] = "ON" if ffmpeg_enabled else "OFF"

        # Python is mandatory for OIIO in this setup.
        values["USE_PYTHON"] = "ON"

        if self.platform.os == "linux" and cfg.build_qt6 and not values.get("OIIO_IV_EXTRA_IV_LIBRARIES"):
            # Qt6 static DBus linkage on Linux may require systemd symbols
            # via libdbus-1.a (_dbus_listen_systemd_sockets).
            values["OIIO_IV_EXTRA_IV_LIBRARIES"] = "dbus-1;systemd"

        # Always embed plugins for consistent single-binary plugin loading across platforms.
        values["EMBEDPLUGINS"] = "ON"

        # Pugixml: use external only when it's part of the planned build and present in the prefix.
        # Otherwise, let OIIO fall back to its internal copy.
        pugixml_planned = any(repo.name == "pugixml" for repo in self.repos)
        pugixml_config_dir = ctx.install_prefix / "lib" / "cmake" / "pugixml"
        pugixml_config_found = any(
            (pugixml_config_dir / name).exists() for name in ("pugixml-config.cmake", "pugixmlConfig.cmake")
        )
        pugixml_header_found = (ctx.install_prefix / "include" / "pugixml.hpp").exists()
        if self.platform.os == "windows":
            pugixml_lib_found = bool(list((ctx.install_prefix / "lib").glob("pugixml*.lib")))
        else:
            pugixml_lib_found = bool(list((ctx.install_prefix / "lib").glob("libpugixml.*")))
        pugixml_found = pugixml_config_found or (pugixml_header_found and pugixml_lib_found)
        values["USE_EXTERNAL_PUGIXML"] = "ON" if (pugixml_planned and pugixml_found) else "OFF"
        required = ["GIF", "JXL", "LibRaw", "libuhdr", "Freetype"]
        if cfg.build_qt6:
            required.insert(0, "Qt6")
            required.insert(1, "OpenGL")
        if ffmpeg_enabled:
            required.insert(0, "FFmpeg")
        args.append(f"-DOpenImageIO_REQUIRED_DEPS={';'.join(required)}")

        # Keep dependency discovery deterministic by hinting the shared prefix.
        root_vars = (
            "ZLIB",
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
            if self.platform.os == "windows":
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

            ordered = []
            for stem in stems:
                ordered.extend(
                    [
                        lib_dir / f"lib{stem}.a",
                        lib_dir / f"lib{stem}.so",
                        lib_dir / f"lib{stem}.dylib",
                        lib_dir / f"{stem}.a",
                    ]
                )
            found = next((candidate for candidate in ordered if candidate.exists()), None)
            if found is not None:
                return found
            matches = []
            for stem in stems:
                matches.extend(sorted(lib_dir.glob(f"lib{stem}.*")))
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
            source_robinmap_include = self.config.global_cfg.src_root / "robin-map" / "include"
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

        if self.platform.os == "windows":
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
            if self.platform.os == "windows":
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
            if self.platform.os == "windows":
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
        if self.platform.os == "windows" and cfg.build_dng_sdk:
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
        if self.platform.os == "windows":
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

        if ffmpeg_enabled:
            def _normalize_ffmpeg_override(value: str | None) -> str | None:
                if value is None:
                    return None
                trimmed = value.strip()
                if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"\"", "'"}:
                    trimmed = trimmed[1:-1].strip()
                return trimmed or None

            def _ffmpeg_override(name: str) -> str | None:
                if self.platform.os == "windows":
                    return _normalize_ffmpeg_override(cfg.windows_env.get(name) or cfg.env.get(name) or os.environ.get(name))
                return _normalize_ffmpeg_override(cfg.env.get(name) or os.environ.get(name))

            def _expand_override_path(value: str) -> Path:
                expanded = Path(os.path.expandvars(value)).expanduser()
                if not expanded.is_absolute():
                    expanded = (cfg.repo_root / expanded).resolve()
                return expanded

            prefix_root = ctx.install_prefix.resolve()
            prefix_norm = os.path.normcase(os.path.normpath(str(prefix_root)))

            def _is_within_prefix(candidate: Path) -> bool:
                if self.platform.os != "windows":
                    return True
                cand_str = str(candidate)
                try:
                    cand_str = str(candidate.resolve())
                except OSError:
                    pass
                cand_norm = os.path.normcase(os.path.normpath(cand_str))
                try:
                    common = os.path.commonpath([cand_norm, prefix_norm])
                except ValueError:
                    return False
                return common == prefix_norm

            ffmpeg_roots: list[Path] = []
            ffmpeg_root_overrides: list[Path] = []
            for key in ("FFmpeg_ROOT", "FFMPEG_ROOT"):
                value = _ffmpeg_override(key)
                if not value:
                    continue
                expanded = _expand_override_path(value)
                ffmpeg_root_overrides.append(expanded)
                ffmpeg_roots.append(expanded)

            if self.platform.os == "windows":
                # Enforce using the build prefix only. Prebuilt FFmpeg must be installed into the same
                # prefix as other deps (CMAKE_INSTALL_PREFIX/CMAKE_PREFIX_PATH).
                if ffmpeg_root_overrides:
                    for root in ffmpeg_root_overrides:
                        if os.path.normcase(os.path.normpath(str(root))) != prefix_norm:
                            print(f"[note] ignoring FFmpeg_ROOT outside install prefix: {root}", flush=True)
                ffmpeg_roots = [prefix_root]
            elif not ffmpeg_root_overrides:
                repo_ffmpeg_root = self.repo_paths.get("ffmpeg")
                if repo_ffmpeg_root is None:
                    ffmpeg_repo = next((repo for repo in self.config.repos if repo.name == "ffmpeg"), None)
                    if ffmpeg_repo is not None:
                        repo_ffmpeg_root = self._resolve_repo_dir(ffmpeg_repo)
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
                if self.platform.os == "windows":
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
                    candidate_names = [f"lib{stem}.a", f"lib{stem}.so", f"lib{stem}.dylib"]

                for directory in ffmpeg_lib_dirs:
                    for name in candidate_names:
                        candidate = directory / name
                        if candidate.exists():
                            return candidate

                for directory in ffmpeg_lib_dirs:
                    if self.platform.os == "windows":
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
            if self.platform.os == "windows":
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
            if self.platform.os == "windows":
                missing_libs: list[str] = []
                if ffmpeg_codec is None:
                    missing_libs.append("avcodec")
                if ffmpeg_format is None:
                    missing_libs.append("avformat")
                if ffmpeg_util is None:
                    missing_libs.append("avutil")
                if ffmpeg_swscale is None:
                    missing_libs.append("swscale")
                if missing_libs:
                    searched = ", ".join(str(d) for d in ffmpeg_lib_dirs) if ffmpeg_lib_dirs else "<none>"
                    print(
                        "[note] FFmpeg .lib files missing for "
                        + ", ".join(missing_libs)
                        + f"; searched: {searched}. Install MSVC-built static FFmpeg into the build prefix "
                        f"({ctx.install_prefix}), or define FFMPEG_LIBAV* overrides that point inside it.",
                        flush=True,
                    )

        # Ensure static dependency linking is propagated for static builds.
        args.append(f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={self._oiio_linkstatic_include(ctx)}")

        for key in sorted(values):
            args.append(f"-D{key}={values[key]}")
        return args

    def _oiio_linkstatic_include(self, ctx: BuildContext) -> str:
        include_path = ctx.build_dir / "oiio_linkstatic.cmake"
        extra_libs = self._oiio_extra_static_libs(ctx.install_prefix, ctx.build_type)
        def _cmake_quote(value: str) -> str:
            # CMake treats backslashes as escapes inside strings, so always normalize
            # Windows paths to forward slashes before embedding.
            if self.platform.os == "windows":
                value = value.replace("\\", "/")
            value = value.replace('"', '\\"')
            return f"\"{value}\""

        extra_list = "\n  ".join(_cmake_quote(entry) for entry in extra_libs) if extra_libs else ""
        static_defs = self._oiio_static_preprocessor_definitions(ctx.install_prefix)
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
        return include_path.as_posix() if self.platform.os == "windows" else str(include_path)

    def _oiio_extra_static_libs(self, prefix: Path, build_type: str) -> list[str]:
        prefix = prefix.resolve()
        libs: list[str] = []
        libdir = prefix / "lib"
        seen: set[str] = set()

        libraw_openmp_value = str(self.config.global_cfg.libraw_enable_openmp).strip().lower()
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

        if self.platform.os == "windows":
            debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
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

            # minizip-ng deps (PPMD)
            add_windows_library(["ppmd"])

            # Freetype deps (HarfBuzz)
            add_windows_library(["harfbuzz"])

            # System libs needed by static deps (minizip-ng, FFmpeg, etc.)
            for syslib in ("bcrypt.lib", "ncrypt.lib", "crypt32.lib", "ws2_32.lib", "secur32.lib"):
                add_entry(syslib)
            # `ucrt(d).lib` are the import libraries for the UCRT DLL and should
            # not be forced for `/MT` builds (it causes CRT mixing).
            if self._windows_runtime_mode() == "dynamic":
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
            if self.platform.os == "linux" and self._ffmpeg_enabled():
                # FFmpeg static libs may reference system hwaccel/display libs
                # (e.g. vdpau/x11/drm) via transitive symbols.
                for syslib in ("vdpau", "X11", "drm", "xcb", "Xau", "Xdmcp", "pthread", "atomic"):
                    add_entry(syslib)

            # minizip-ng deps (PPMD)
            add_lib("libppmd.a")

            # Freetype deps (HarfBuzz)
            add_lib("libharfbuzz.a")

        # OpenMP (libomp)
        omp_root = self.config.global_cfg.env.get("OpenMP_ROOT")
        if self.platform.os == "windows":
            omp_root = self.config.global_cfg.windows_env.get("OpenMP_ROOT") or os.environ.get("OpenMP_ROOT") or omp_root
        else:
            omp_root = os.environ.get("OpenMP_ROOT") or omp_root
        omp_added = False
        if libraw_openmp_enabled:
            if omp_root:
                candidates = ["libomp.dylib", "libomp.a", "libomp.so", "libiomp5.so", "libgomp.so"]
                if self.platform.os == "windows":
                    candidates = ["libomp.lib", "libompd.lib", "libiomp5md.lib", "libiomp5mdd.lib"] + candidates
                for candidate in candidates:
                    path = Path(omp_root) / "lib" / candidate
                    if path.exists():
                        add_entry(str(path))
                        omp_added = True
                        break

            if self.platform.os == "windows" and not omp_added:
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

        if self.platform.os == "linux" and not omp_added:
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

        if self.platform.os == "linux" and libraw_openmp_enabled and not omp_added:
            # Last-resort fallback when LibRaw was compiled with OpenMP but no
            # absolute runtime path was discovered.
            add_entry("omp")

        # iconv (system)
        if (libdir / "libiconv.a").exists():
            add_entry(str(libdir / "libiconv.a"))
        else:
            if self.platform.os == "macos":
                add_entry("iconv")

        # macOS Security framework (minizip-ng uses SecRandomCopyBytes)
        if self.platform.os == "macos":
            add_entry("-Wl,-framework,Security")
            if self._ffmpeg_enabled():
                for framework_flag in (
                    "-Wl,-framework,AudioToolbox",
                    "-Wl,-framework,VideoToolbox",
                    "-Wl,-framework,CoreMedia",
                    "-Wl,-framework,CoreVideo",
                    "-Wl,-framework,CoreFoundation",
                ):
                    add_entry(framework_flag)

        return libs

    def _oiio_static_preprocessor_definitions(self, prefix: Path) -> list[str]:
        prefix = prefix.resolve()
        include_dir = prefix / "include"
        lib_dir = prefix / "lib"

        def _has_any_library(stems: list[str]) -> bool:
            for stem in stems:
                if self.platform.os == "windows":
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

    def _autotools_args(self, repo: RepoConfig) -> list[str]:
        if repo.name == "xz":
            return ["--disable-nls", "--disable-xz", "--disable-xzdec", "--disable-lzmadec", "--disable-lzmainfo"]
        if repo.name == "lcms2":
            return ["--without-fastfloat", "--without-threaded"]
        return []

    def _ffmpeg_configure_args(self, ctx: BuildContext) -> list[str]:
        cfg = self.config.global_cfg
        windows_native_ffmpeg = self.platform.os == "windows" and self._windows_ffmpeg_native_build_enabled()
        args = [
            f"--prefix={ctx.install_prefix}",
            "--disable-shared",
            "--enable-static",
            "--enable-pic",
            "--disable-doc",
            "--pkg-config-flags=--static",
        ]
        if windows_native_ffmpeg:
            # FFmpeg on Windows is built through MSYS2 makefiles while targeting
            # the same MSVC/clang-cl toolchain family as the CMake repos.
            target_os = "win64" if self.platform.arch in {"x86_64", "arm64"} else "win32"
            ffmpeg_arch = "aarch64" if self.platform.arch == "arm64" else ("x86_64" if self.platform.arch == "x86_64" else self.platform.arch)
            args.extend(
                [
                    f"--target-os={target_os}",
                    f"--arch={ffmpeg_arch}",
                    "--toolchain=msvc",
                    "--ar=lib",
                    "--ranlib=:",
                ]
            )
            generator = str(cfg.windows.get("generator", "ninja-msvc")).strip().lower()
            if generator in {"msvc-clang-cl", "ninja-clang-cl"}:
                args.append("--cc=clang-cl")
                args.append("--cxx=clang-cl")
            else:
                args.append("--cc=cl")
                args.append("--cxx=cl")
        if ctx.build_type == "Release":
            if not windows_native_ffmpeg:
                args.append("--disable-debug")
        else:
            if windows_native_ffmpeg:
                debug_postfix = str(cfg.windows.get("debug_postfix", "d")).strip()
                args.append("--enable-debug")
                if debug_postfix:
                    args.append(f"--build-suffix={debug_postfix}")
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

    def _ffmpeg_configure_command(self, configure: Path, args: list[str], env: dict[str, str]) -> list[str]:
        if self.platform.os != "windows":
            return [str(configure), *args]
        if not self._windows_ffmpeg_native_build_enabled():
            return [str(configure), *args]

        bash = self._which_in_env("bash", env) or self._which_in_env("bash.exe", env)
        if not bash:
            raise RuntimeError(
                "FFmpeg native build on Windows requires MSYS2 bash in PATH. "
                "Run from an MSYS2 shell (MSYSTEM set) or disable windows.build_ffmpeg."
            )
        return [bash, configure.as_posix(), *args]

    def _ffmpeg_make_command(self, make_args: list[str], env: dict[str, str]) -> list[str]:
        if self.platform.os != "windows":
            return ["make", *make_args]
        if not self._windows_ffmpeg_native_build_enabled():
            return ["make", *make_args]

        make = self._which_in_env("make", env) or self._which_in_env("mingw32-make", env)
        if not make:
            raise RuntimeError(
                "FFmpeg native build on Windows requires MSYS2 make in PATH. "
                "Run from an MSYS2 shell (MSYSTEM set) or disable windows.build_ffmpeg."
            )
        return [make, *make_args]

    def _autotools_windows_msys2_active(self) -> bool:
        return self.platform.os == "windows" and self._windows_msys2_detected()

    def _autotools_configure_command(self, configure: Path, args: list[str], env: dict[str, str]) -> list[str]:
        if not self._autotools_windows_msys2_active():
            return [str(configure), *args]

        bash = self._which_in_env("bash", env) or self._which_in_env("bash.exe", env)
        if not bash:
            raise RuntimeError(
                "Windows autotools builds require MSYS2 bash in PATH. "
                "Run from an MSYS2 shell (MSYSTEM set)."
            )
        return [bash, configure.as_posix(), *args]

    def _autotools_make_command(self, make_args: list[str], env: dict[str, str]) -> list[str]:
        if not self._autotools_windows_msys2_active():
            return ["make", *make_args]

        make = self._which_in_env("make", env) or self._which_in_env("mingw32-make", env)
        if not make:
            raise RuntimeError(
                "Windows autotools builds require MSYS2 make in PATH. "
                "Run from an MSYS2 shell (MSYSTEM set)."
            )
        return [make, *make_args]

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
        if self._ccache_path:
            args.append(f"-DCMAKE_C_COMPILER_LAUNCHER={self._ccache_path}")
            args.append(f"-DCMAKE_CXX_COMPILER_LAUNCHER={self._ccache_path}")

        if self.platform.os == "windows":
            # MSBuild + VS generators sometimes hit file timestamp races in the generated
            # "check build system" custom steps (generate.stamp). The builder always
            # re-configures from scratch when it rebuilds a repo, so regeneration is
            # unnecessary here.
            generator = str(cfg.windows.get("generator", "ninja-msvc")).strip().lower()
            if generator in {"msvc", "msvc-clang-cl"}:
                args.append("-DCMAKE_SUPPRESS_REGENERATION=ON")

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

        effective_env = dict(os.environ)
        effective_env.update(cfg.env)
        if self.platform.os == "windows":
            effective_env.update(cfg.windows_env)
        nasm = resolve_nasm_executable(effective_env, platform_os=self.platform.os)
        if nasm:
            args.append(f"-DCMAKE_ASM_NASM_COMPILER={nasm}")

        python_exec = _normalize_override(
            cfg.env.get("Python3_EXECUTABLE")
            or cfg.env.get("PYTHON3_EXECUTABLE")
            or cfg.env.get("Python_EXECUTABLE")
            or cfg.env.get("PYTHON_EXECUTABLE")
        )
        if self.platform.os == "windows":
            python_exec = _normalize_override(
                cfg.windows_env.get("Python3_EXECUTABLE")
                or cfg.windows_env.get("PYTHON3_EXECUTABLE")
                or cfg.windows_env.get("Python_EXECUTABLE")
                or cfg.windows_env.get("PYTHON_EXECUTABLE")
                or os.environ.get("Python3_EXECUTABLE")
                or os.environ.get("PYTHON3_EXECUTABLE")
                or os.environ.get("Python_EXECUTABLE")
                or os.environ.get("PYTHON_EXECUTABLE")
            ) or python_exec
        else:
            python_exec = _normalize_override(
                os.environ.get("Python3_EXECUTABLE")
                or os.environ.get("PYTHON3_EXECUTABLE")
                or os.environ.get("Python_EXECUTABLE")
                or os.environ.get("PYTHON_EXECUTABLE")
            ) or python_exec

        cpython_enabled = self._cpython_enabled_for_run()
        if cpython_enabled:
            prefix_posix = ctx.install_prefix.as_posix()
            args.append(f"-DPython3_ROOT_DIR={prefix_posix}")
            args.append(f"-DPython_ROOT_DIR={prefix_posix}")
            args.append("-DPython3_FIND_STRATEGY=LOCATION")
            args.append("-DPython_FIND_STRATEGY=LOCATION")
            if not python_exec:
                prefix_python = self._prefix_python_executable(ctx.install_prefix, ctx.build_type)
                if prefix_python is not None:
                    python_exec = prefix_python.as_posix()

        # Keep Python resolution portable by default:
        # - do not hardcode an absolute interpreter path unless user-provided;
        # - prefer PATH/venv over Windows registry-provided interpreters.
        if self.platform.os == "windows":
            args.append("-DPython3_FIND_REGISTRY=NEVER")
            args.append("-DPython_FIND_REGISTRY=NEVER")

        in_virtual_env = (
            bool(os.environ.get("VIRTUAL_ENV"))
            or bool(os.environ.get("CONDA_PREFIX"))
            or bool(getattr(sys, "real_prefix", ""))
            or (getattr(sys, "base_prefix", sys.prefix) != sys.prefix)
        )
        if in_virtual_env:
            args.append("-DPython3_FIND_VIRTUALENV=ONLY")
            args.append("-DPython_FIND_VIRTUALENV=ONLY")

        if python_exec:
            args.append(f"-DPython3_EXECUTABLE={python_exec}")
            args.append(f"-DPython_EXECUTABLE={python_exec}")

        if self.platform.os == "windows" and cpython_enabled:
            python_release_lib, python_debug_lib = self._prefix_windows_python_libraries(ctx.install_prefix)
            if python_release_lib is not None:
                release_posix = python_release_lib.as_posix()
                args.append(f"-DPython3_LIBRARY_RELEASE={release_posix}")
                args.append(f"-DPython_LIBRARY_RELEASE={release_posix}")
            if python_debug_lib is not None:
                debug_posix = python_debug_lib.as_posix()
                args.append(f"-DPython3_LIBRARY_DEBUG={debug_posix}")
                args.append(f"-DPython_LIBRARY_DEBUG={debug_posix}")
            if python_release_lib is not None or python_debug_lib is not None:
                if ctx.build_type == "Debug":
                    chosen = python_debug_lib or python_release_lib
                else:
                    chosen = python_release_lib or python_debug_lib
                if chosen is not None:
                    chosen_posix = chosen.as_posix()
                    args.append(f"-DPython3_LIBRARY={chosen_posix}")
                    args.append(f"-DPython_LIBRARY={chosen_posix}")

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

        def _windows_vs_generator() -> str:
            # Allow overriding the Visual Studio generator name to support
            # multiple VS versions (e.g. "Visual Studio 18 2026" in CMake 4.2+).
            raw = cfg.windows.get("vs_generator")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            return "Visual Studio 17 2022"

        generator = str(cfg.windows.get("generator", "ninja-msvc")).strip().lower()
        if generator == "msvc":
            return ["-G", _windows_vs_generator()]
        if generator == "msvc-clang-cl":
            return ["-G", _windows_vs_generator(), "-T", "ClangCL"]
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

    def _openssl_export_zip(self, env: dict[str, str] | None = None) -> Path:
        cfg = self.config.global_cfg
        default = cfg.repo_root / "external" / "vcpkg-export-openssl.zip"
        override = None
        if env:
            override = env.get("OPENSSL_VCPKG_EXPORT_ZIP") or env.get("VCPKG_OPENSSL_EXPORT_ZIP")
        if not override and self.platform.os == "windows":
            override = (
                cfg.windows_env.get("OPENSSL_VCPKG_EXPORT_ZIP")
                or cfg.windows_env.get("VCPKG_OPENSSL_EXPORT_ZIP")
                or cfg.env.get("OPENSSL_VCPKG_EXPORT_ZIP")
                or cfg.env.get("VCPKG_OPENSSL_EXPORT_ZIP")
                or os.environ.get("OPENSSL_VCPKG_EXPORT_ZIP")
                or os.environ.get("VCPKG_OPENSSL_EXPORT_ZIP")
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
            matches = sorted(external_dir.glob("vcpkg-export-openssl*.zip"))
            if matches:
                return matches[0]
        return default

    def _sqlite_export_zip(self, env: dict[str, str] | None = None) -> Path:
        cfg = self.config.global_cfg
        default = cfg.repo_root / "external" / "vcpkg-export-sqlite.zip"
        override = None
        if env:
            override = (
                env.get("SQLITE_VCPKG_EXPORT_ZIP")
                or env.get("VCPKG_SQLITE_EXPORT_ZIP")
                or env.get("SQLITE3_VCPKG_EXPORT_ZIP")
                or env.get("VCPKG_SQLITE3_EXPORT_ZIP")
            )
        if not override and self.platform.os == "windows":
            override = (
                cfg.windows_env.get("SQLITE_VCPKG_EXPORT_ZIP")
                or cfg.windows_env.get("VCPKG_SQLITE_EXPORT_ZIP")
                or cfg.windows_env.get("SQLITE3_VCPKG_EXPORT_ZIP")
                or cfg.windows_env.get("VCPKG_SQLITE3_EXPORT_ZIP")
                or cfg.env.get("SQLITE_VCPKG_EXPORT_ZIP")
                or cfg.env.get("VCPKG_SQLITE_EXPORT_ZIP")
                or cfg.env.get("SQLITE3_VCPKG_EXPORT_ZIP")
                or cfg.env.get("VCPKG_SQLITE3_EXPORT_ZIP")
                or os.environ.get("SQLITE_VCPKG_EXPORT_ZIP")
                or os.environ.get("VCPKG_SQLITE_EXPORT_ZIP")
                or os.environ.get("SQLITE3_VCPKG_EXPORT_ZIP")
                or os.environ.get("VCPKG_SQLITE3_EXPORT_ZIP")
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
            matches = sorted(external_dir.glob("vcpkg-export-sqlite*.zip"))
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
        if repo.name == "openssl" and self.platform.os == "windows":
            zip_path = self._openssl_export_zip()
            if zip_path.exists():
                return False
            if repo.optional:
                print(f"[skip] {repo.name}: missing vcpkg export zip at {zip_path}")
                return True
            return False
        if repo.name == "sqlite" and self.platform.os == "windows":
            zip_path = self._sqlite_export_zip()
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

    def _prepare_repo_source(self, repo: RepoConfig, src_dir: Path) -> None:
        lock = self._repo_source_prepare_locks.get(repo.name)
        if lock is None:
            return
        with lock:
            if repo.name in self._repo_source_prepared:
                return
            if repo.name == "glew":
                self._patch_glew_macos(src_dir)
            recipe_registry.patch_source(repo.name, self, src_dir)
            self._repo_source_prepared.add(repo.name)

    def _source_prep_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.config.global_cfg.env)
        if self.platform.os == "windows":
            env.update(self.config.global_cfg.windows_env)
        return env

    def _qt6_submodules(self) -> list[str]:
        submodules = [
            "qtbase",
            "qtdeclarative",
            "qtshadertools",
            "qtmultimedia",
            "qtimageformats",
            "qtsvg",
            "qttools",
        ]
        if self.platform.os == "linux":
            submodules.append("qtwayland")
        return submodules

    def _qt6_submodule_initialized(self, src_dir: Path, name: str) -> bool:
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

    def _prepare_qt6_sources(self, src_dir: Path) -> None:
        qt_submodules = self._qt6_submodules()
        missing_submodules = [name for name in qt_submodules if not self._qt6_submodule_initialized(src_dir, name)]
        if not missing_submodules:
            return

        init_repo = src_dir / ("init-repository.bat" if self.platform.os == "windows" else "init-repository")
        if not init_repo.exists():
            return

        banner("Qt6 - init submodules")
        if self.platform.os == "windows":
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
        if self.no_update:
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
            env=self._source_prep_env(),
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path("Qt6", "_shared", "init-submodules")),
        )

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
        override_dir = self.pkg_override_root / build_type
        override_dir.mkdir(parents=True, exist_ok=True)
        dst = override_dir / "OpenEXR.pc"

        def _pick_windows_lib(libdir: Path, names: list[str], globs: list[str]) -> Path | None:
            for name in names:
                candidate = libdir / name
                if candidate.exists():
                    return candidate
            for pattern in globs:
                matches = sorted(libdir.glob(pattern))
                if matches:
                    return matches[0]
            return None

        extra_flags = ""
        if self.platform.os == "windows":
            debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
            libdir = prefix / "lib"
            if build_type == "Debug":
                deflate_names = [f"deflatestatic{debug_postfix}.lib", "deflatestatic.lib", f"deflate{debug_postfix}.lib", "deflate.lib"]
                deflate_globs = [f"deflate*{debug_postfix}.lib", "deflate*.lib"]
                openjph_names = [f"openjph{debug_postfix}.lib", "openjph.lib"]
                openjph_globs = [f"openjph*{debug_postfix}.lib", "openjph*.lib"]
                imath_names = [f"Imath-3_2{debug_postfix}.lib", "Imath-3_2d.lib", "Imath-3_2.lib"]
                imath_globs = [f"Imath-*{debug_postfix}.lib", "Imath-*d.lib", "Imath-*.lib"]
            else:
                deflate_names = ["deflatestatic.lib", "deflate.lib", f"deflatestatic{debug_postfix}.lib", f"deflate{debug_postfix}.lib"]
                deflate_globs = ["deflate*.lib", f"deflate*{debug_postfix}.lib"]
                openjph_names = ["openjph.lib", f"openjph{debug_postfix}.lib"]
                openjph_globs = ["openjph*.lib", f"openjph*{debug_postfix}.lib"]
                imath_names = ["Imath-3_2.lib", f"Imath-3_2{debug_postfix}.lib", "Imath-3_2d.lib"]
                imath_globs = ["Imath-*.lib", f"Imath-*{debug_postfix}.lib", "Imath-*d.lib"]
            deflate_lib = _pick_windows_lib(libdir, deflate_names, deflate_globs)
            openjph_lib = _pick_windows_lib(libdir, openjph_names, openjph_globs)
            imath_lib = _pick_windows_lib(libdir, imath_names, imath_globs)
            windows_libs: list[str] = []
            if deflate_lib:
                windows_libs.append(deflate_lib.as_posix())
            if openjph_lib:
                windows_libs.append(openjph_lib.as_posix())
            if imath_lib:
                windows_libs.append(imath_lib.as_posix())
            if windows_libs:
                extra_flags = " " + " ".join(windows_libs)
        else:
            openjph_lib = "openjph"
            if build_type == "Debug" and (prefix / "lib" / "libopenjph_d.a").exists():
                openjph_lib = "openjph_d"
            extra_flags = f" -ldeflate -l{openjph_lib}"

        lines = []
        for line in src.read_text(encoding="utf-8").splitlines():
            if line.startswith("Libs:"):
                cleaned = re.sub(r"\s+-l(?:deflate|openjph[^\s]*)", "", line)
                cleaned = re.sub(r"\s+-lImath[^\s]*", "", cleaned)
                cleaned = re.sub(r"\s+[^\s]*deflate[^\s]*\.lib", "", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\s+[^\s]*openjph[^\s]*\.lib", "", cleaned, flags=re.IGNORECASE)
                cleaned = re.sub(r"\s+[^\s]*Imath[^\s]*\.lib", "", cleaned, flags=re.IGNORECASE)
                lines.append((cleaned.rstrip() + extra_flags).rstrip())
                continue
            if self.platform.os == "windows" and line.startswith("Cflags:"):
                cleaned = re.sub(r"\s+-I\$\{includedir\}/Imath\b", "", line)
                cleaned = re.sub(r"\s+-I[^\s]*[/\\\\]Imath\b", "", cleaned)
                lines.append((cleaned.rstrip() + " -I${includedir}/Imath").rstrip())
                continue
            if self.platform.os == "windows" and line.startswith("Requires:"):
                cleaned = line
                cleaned = re.sub(r"\bImath\b(?:\s*[<>=]+\s*[\w\.\-]+)?", "", cleaned)
                cleaned = re.sub(r"\s+", " ", cleaned).rstrip()
                if cleaned.endswith(":"):
                    lines.append("Requires:")
                else:
                    lines.append(cleaned)
                continue
            if self.platform.os == "windows" and line.startswith("Requires.private:"):
                cleaned = line
                cleaned = re.sub(r"\blibdeflate\b(?:\s*[<>=]+\s*[\w\.\-]+)?", "", cleaned)
                cleaned = re.sub(r"\bopenjph\b(?:\s*[<>=]+\s*[\w\.\-]+)?", "", cleaned)
                cleaned = re.sub(r"\s+", " ", cleaned).rstrip()
                if cleaned.endswith(":"):
                    lines.append("Requires.private:")
                else:
                    lines.append(cleaned)
                continue
            lines.append(line)
        dst.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ensure_harfbuzz_package(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        include_dir = prefix / "include" / "harfbuzz"
        if not (include_dir / "hb.h").exists():
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        if self.platform.os == "windows":
            release_candidates = [libdir / "harfbuzz.lib", libdir / "libharfbuzz.lib", libdir / f"harfbuzz{debug_postfix}.lib"]
            debug_candidates = [libdir / f"harfbuzz{debug_postfix}.lib", libdir / "harfbuzz.lib", libdir / "libharfbuzz.lib"]
            fallback_pattern = "*harfbuzz*.lib"
        else:
            release_candidates = [libdir / "libharfbuzz.a", libdir / "libharfbuzz.so", libdir / "libharfbuzz.dylib"]
            debug_candidates = [libdir / "libharfbuzz.a", libdir / "libharfbuzz.so", libdir / "libharfbuzz.dylib"]
            fallback_pattern = "libharfbuzz.*"

        release_lib = next((candidate for candidate in release_candidates if candidate.exists()), None)
        debug_lib = next((candidate for candidate in debug_candidates if candidate.exists()), None)
        if release_lib is None and debug_lib is None:
            matches = sorted(libdir.glob(fallback_pattern))
            if matches:
                release_lib = matches[0]
                debug_lib = matches[0]
            else:
                return

        default_lib = release_lib or debug_lib
        if build_type == "Debug" and debug_lib is not None:
            default_lib = debug_lib
        if default_lib is None:
            return

        cmake_dir = libdir / "cmake" / "HarfBuzz"
        try:
            cmake_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        include_path = include_dir.as_posix()
        default_path = default_lib.as_posix()
        release_path = release_lib.as_posix() if release_lib is not None else ""
        debug_path = debug_lib.as_posix() if debug_lib is not None else ""
        extra_link = ""
        if self.platform.os == "macos":
            extra_link = '  set_property(TARGET HarfBuzz::HarfBuzz APPEND PROPERTY INTERFACE_LINK_LIBRARIES "-framework CoreText")\n'
        config_text = f"""\
set(HarfBuzz_FOUND TRUE)
set(HarfBuzz_INCLUDE_DIR "{include_path}")
set(HarfBuzz_INCLUDE_DIRS "{include_path}")

if(NOT TARGET HarfBuzz::HarfBuzz)
  add_library(HarfBuzz::HarfBuzz UNKNOWN IMPORTED)
  set_target_properties(HarfBuzz::HarfBuzz PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"
    IMPORTED_LOCATION "{default_path}"
  )
  if(EXISTS "{release_path}")
    set_property(TARGET HarfBuzz::HarfBuzz PROPERTY IMPORTED_LOCATION_RELEASE "{release_path}")
  endif()
  if(EXISTS "{debug_path}")
    set_property(TARGET HarfBuzz::HarfBuzz PROPERTY IMPORTED_LOCATION_DEBUG "{debug_path}")
  endif()
{extra_link}
endif()

if(NOT TARGET harfbuzz::harfbuzz)
  add_library(harfbuzz::harfbuzz INTERFACE IMPORTED)
  set_property(TARGET harfbuzz::harfbuzz PROPERTY INTERFACE_LINK_LIBRARIES HarfBuzz::HarfBuzz)
endif()

set(HarfBuzz_LIBRARY HarfBuzz::HarfBuzz)
set(HarfBuzz_LIBRARIES HarfBuzz::HarfBuzz)
"""
        hb_version = ""
        pc_candidates = [
            prefix / "lib" / "pkgconfig" / "harfbuzz.pc",
            prefix / "share" / "pkgconfig" / "harfbuzz.pc",
        ]
        for candidate in pc_candidates:
            if not candidate.exists():
                continue
            try:
                for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.startswith("Version:"):
                        continue
                    hb_version = line.partition(":")[2].strip()
                    break
            except OSError:
                hb_version = ""
            if hb_version:
                break
        if not hb_version:
            header = prefix / "include" / "harfbuzz" / "hb-version.h"
            if header.exists():
                try:
                    m = re.search(
                        r'^\\s*#\\s*define\\s+HB_VERSION_STRING\\s+\\"([^\\"]+)\\"',
                        header.read_text(encoding="utf-8", errors="replace"),
                        flags=re.MULTILINE,
                    )
                    if m:
                        hb_version = m.group(1)
                except OSError:
                    hb_version = ""
        if not hb_version:
            hb_version = "1.0.0"

        version_text = f"""\
set(PACKAGE_VERSION "{hb_version}")
if(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
  if(PACKAGE_FIND_VERSION STREQUAL PACKAGE_VERSION)
    set(PACKAGE_VERSION_EXACT TRUE)
  endif()
endif()
"""
        for name in ("HarfBuzzConfig.cmake", "harfbuzz-config.cmake"):
            try:
                (cmake_dir / name).write_text(config_text, encoding="utf-8")
            except OSError:
                return
        for name in ("HarfBuzzConfigVersion.cmake", "harfbuzz-config-version.cmake"):
            try:
                (cmake_dir / name).write_text(version_text, encoding="utf-8")
            except OSError:
                return

    def _ensure_jasper_package(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        include_dir = prefix / "include" / "jasper"
        if not (include_dir / "jas_config.h").exists():
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        if self.platform.os == "windows":
            debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
            if build_type == "Debug":
                candidates = [libdir / f"jasper{debug_postfix}.lib", libdir / "jasper.lib", libdir / f"libjasper{debug_postfix}.lib"]
            else:
                candidates = [libdir / "jasper.lib", libdir / f"jasper{debug_postfix}.lib", libdir / "libjasper.lib"]
            lib = next((c for c in candidates if c.exists()), None)
            if lib is None:
                matches = sorted(libdir.glob("*jasper*.lib"))
                if matches:
                    lib = matches[0]
        else:
            candidates = [libdir / "libjasper.a", libdir / "libjasper.dylib", libdir / "libjasper.so"]
            lib = next((c for c in candidates if c.exists()), None)
            if lib is None:
                matches = sorted(libdir.glob("libjasper.*"))
                if matches:
                    lib = matches[0]
        if lib is None:
            return

        cmake_dir = libdir / "cmake" / "Jasper"
        try:
            cmake_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        include_path = include_dir.as_posix()
        lib_path = lib.as_posix()

        config_text = f"""\
set(Jasper_FOUND TRUE)
set(JASPER_FOUND TRUE)
set(JASPER_INCLUDE_DIR "{include_path}")
set(JASPER_INCLUDE_DIRS "{include_path}")
set(JASPER_LIBRARY "{lib_path}")
set(JASPER_LIBRARIES "{lib_path}")

if(NOT TARGET Jasper::Jasper)
  add_library(Jasper::Jasper UNKNOWN IMPORTED)
  set_target_properties(Jasper::Jasper PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"
    IMPORTED_LOCATION "{lib_path}"
  )
endif()
"""

        version_text = """\
set(PACKAGE_VERSION "1.0.0")
if(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
  if(PACKAGE_FIND_VERSION STREQUAL PACKAGE_VERSION)
    set(PACKAGE_VERSION_EXACT TRUE)
  endif()
endif()
"""

        for name in ("JasperConfig.cmake", "jasper-config.cmake"):
            try:
                (cmake_dir / name).write_text(config_text, encoding="utf-8")
            except OSError:
                return
        for name in ("JasperConfigVersion.cmake", "jasper-config-version.cmake"):
            try:
                (cmake_dir / name).write_text(version_text, encoding="utf-8")
            except OSError:
                return

    def _ensure_bzip2_package(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        include_dir = prefix / "include"
        if not (include_dir / "bzlib.h").exists():
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        if self.platform.os == "windows":
            release_candidates = [
                libdir / "bz2_static.lib",
                libdir / "bz2.lib",
                libdir / "libbz2_static.lib",
                libdir / "libbz2.lib",
            ]
            debug_candidates = [
                libdir / f"bz2_static{debug_postfix}.lib",
                libdir / f"bz2{debug_postfix}.lib",
                libdir / f"libbz2_static{debug_postfix}.lib",
                libdir / f"libbz2{debug_postfix}.lib",
            ]
            fallback_pattern = "*bz2*.lib"
        else:
            release_candidates = [libdir / "libbz2_static.a", libdir / "libbz2.a", libdir / "libbz2.so", libdir / "libbz2.dylib"]
            debug_candidates = [
                libdir / "libbz2_staticd.a",
                libdir / "libbz2d.a",
                libdir / "libbz2_static.a",
                libdir / "libbz2.a",
            ]
            fallback_pattern = "lib*bz2*.*"

        release_lib = next((candidate for candidate in release_candidates if candidate.exists()), None)
        debug_lib = next((candidate for candidate in debug_candidates if candidate.exists()), None)
        if release_lib is None and debug_lib is None:
            matches = sorted(libdir.glob(fallback_pattern))
            if matches:
                release_lib = matches[0]
                debug_lib = matches[0]
            else:
                return

        default_lib = release_lib or debug_lib
        if build_type == "Debug" and debug_lib is not None:
            default_lib = debug_lib
        if default_lib is None:
            return

        cmake_dir = libdir / "cmake" / "BZip2"
        try:
            cmake_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        include_path = include_dir.as_posix()
        default_path = default_lib.as_posix()
        release_path = release_lib.as_posix() if release_lib is not None else ""
        debug_path = debug_lib.as_posix() if debug_lib is not None else ""
        config_text = f"""\
set(BZip2_FOUND TRUE)
set(BZIP2_FOUND TRUE)
set(BZip2_INCLUDE_DIR "{include_path}")
set(BZIP2_INCLUDE_DIR "{include_path}")
set(BZIP2_INCLUDE_DIRS "{include_path}")

if(NOT TARGET BZip2::BZip2)
  add_library(BZip2::BZip2 UNKNOWN IMPORTED)
  set_target_properties(BZip2::BZip2 PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"
    IMPORTED_LOCATION "{default_path}"
  )
  if(EXISTS "{release_path}")
    set_property(TARGET BZip2::BZip2 PROPERTY IMPORTED_LOCATION_RELEASE "{release_path}")
  endif()
  if(EXISTS "{debug_path}")
    set_property(TARGET BZip2::BZip2 PROPERTY IMPORTED_LOCATION_DEBUG "{debug_path}")
  endif()
endif()

set(BZIP2_LIBRARY BZip2::BZip2)
set(BZIP2_LIBRARIES BZip2::BZip2)
"""
        version_text = """\
set(PACKAGE_VERSION "1.0.0")
if(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
  if(PACKAGE_FIND_VERSION STREQUAL PACKAGE_VERSION)
    set(PACKAGE_VERSION_EXACT TRUE)
  endif()
endif()
"""
        for name in ("BZip2Config.cmake", "bzip2-config.cmake"):
            try:
                (cmake_dir / name).write_text(config_text, encoding="utf-8")
            except OSError:
                return
        for name in ("BZip2ConfigVersion.cmake", "bzip2-config-version.cmake"):
            try:
                (cmake_dir / name).write_text(version_text, encoding="utf-8")
            except OSError:
                return

    def _ensure_unofficial_brotli_package(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        include_dir = prefix / "include"
        if not (include_dir / "brotli" / "decode.h").exists():
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))

        def _pick_lib_pair(stem: str) -> tuple[Path | None, Path | None]:
            release_lib: Path | None = None
            debug_lib: Path | None = None

            if self.platform.os == "windows":
                release_candidates = [
                    libdir / f"{stem}.lib",
                    libdir / f"lib{stem}.lib",
                    libdir / f"{stem}-static.lib",
                    libdir / f"lib{stem}-static.lib",
                    libdir / f"{stem}static.lib",
                    libdir / f"lib{stem}static.lib",
                ]
                debug_candidates = [
                    libdir / f"{stem}{debug_postfix}.lib",
                    libdir / f"lib{stem}{debug_postfix}.lib",
                    libdir / f"{stem}-static{debug_postfix}.lib",
                    libdir / f"lib{stem}-static{debug_postfix}.lib",
                    libdir / f"{stem}static{debug_postfix}.lib",
                    libdir / f"lib{stem}static{debug_postfix}.lib",
                ]
                release_lib = next((candidate for candidate in release_candidates if candidate.exists()), None)
                debug_lib = next((candidate for candidate in debug_candidates if candidate.exists()), None)

                if release_lib is None or debug_lib is None:
                    matches = sorted(libdir.glob(f"*{stem}*.lib"))
                    if matches:
                        if release_lib is None:
                            release_lib = next(
                                (m for m in matches if not m.stem.lower().endswith(debug_postfix.lower())),
                                None,
                            ) or matches[0]
                        if debug_lib is None:
                            debug_lib = next(
                                (m for m in matches if m.stem.lower().endswith(debug_postfix.lower())),
                                None,
                            ) or release_lib
            else:
                release_candidates = [
                    libdir / f"lib{stem}.a",
                    libdir / f"lib{stem}-static.a",
                    libdir / f"lib{stem}_static.a",
                    libdir / f"lib{stem}.so",
                    libdir / f"lib{stem}.dylib",
                ]
                debug_candidates = [
                    libdir / f"lib{stem}d.a",
                    libdir / f"lib{stem}_d.a",
                    libdir / f"lib{stem}.a",
                ]
                release_lib = next((candidate for candidate in release_candidates if candidate.exists()), None)
                debug_lib = next((candidate for candidate in debug_candidates if candidate.exists()), None)
                if release_lib is None and debug_lib is None:
                    matches = sorted(libdir.glob(f"lib{stem}*"))
                    preferred = [m for m in matches if m.suffix in {".a", ".so", ".dylib"}]
                    if preferred:
                        release_lib = preferred[0]
                        debug_lib = preferred[0]

            return release_lib, debug_lib

        common_release, common_debug = _pick_lib_pair("brotlicommon")
        dec_release, dec_debug = _pick_lib_pair("brotlidec")
        enc_release, enc_debug = _pick_lib_pair("brotlienc")
        if not common_release and not common_debug:
            return
        if not dec_release and not dec_debug:
            return
        if not enc_release and not enc_debug:
            return

        def _default_for_pair(release: Path | None, debug: Path | None) -> Path | None:
            chosen = release or debug
            if build_type == "Debug" and debug is not None:
                chosen = debug
            return chosen

        common_default = _default_for_pair(common_release, common_debug)
        dec_default = _default_for_pair(dec_release, dec_debug)
        enc_default = _default_for_pair(enc_release, enc_debug)
        if common_default is None or dec_default is None or enc_default is None:
            return

        cmake_dir = libdir / "cmake" / "unofficial-brotli"
        try:
            cmake_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        version = ""
        pc_candidates = [
            prefix / "lib" / "pkgconfig" / "libbrotlidec.pc",
            prefix / "share" / "pkgconfig" / "libbrotlidec.pc",
        ]
        for candidate in pc_candidates:
            if not candidate.exists():
                continue
            try:
                for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.startswith("Version:"):
                        continue
                    version = line.partition(":")[2].strip()
                    break
            except OSError:
                version = ""
            if version:
                break
        if not version:
            version = "1.0.0"

        include_path = include_dir.as_posix()

        def _posix(path: Path | None) -> str:
            return path.as_posix() if path is not None else ""

        config_text = f"""\
set(unofficial-brotli_FOUND TRUE)
set(unofficial-brotli_VERSION "{version}")

set(_unofficial_brotli_include_dir "{include_path}")

if(NOT TARGET unofficial::brotli::brotlicommon)
  add_library(unofficial::brotli::brotlicommon UNKNOWN IMPORTED)
  set_target_properties(unofficial::brotli::brotlicommon PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"
    IMPORTED_LOCATION "{common_default.as_posix()}"
  )
  if(EXISTS "{_posix(common_release)}")
    set_property(TARGET unofficial::brotli::brotlicommon PROPERTY IMPORTED_LOCATION_RELEASE "{_posix(common_release)}")
  endif()
  if(EXISTS "{_posix(common_debug)}")
    set_property(TARGET unofficial::brotli::brotlicommon PROPERTY IMPORTED_LOCATION_DEBUG "{_posix(common_debug)}")
  endif()
  if(NOT WIN32)
    set_property(TARGET unofficial::brotli::brotlicommon APPEND PROPERTY INTERFACE_LINK_LIBRARIES m)
  endif()
endif()

if(NOT TARGET unofficial::brotli::brotlidec)
  add_library(unofficial::brotli::brotlidec UNKNOWN IMPORTED)
  set_target_properties(unofficial::brotli::brotlidec PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"
    IMPORTED_LOCATION "{dec_default.as_posix()}"
  )
  if(EXISTS "{_posix(dec_release)}")
    set_property(TARGET unofficial::brotli::brotlidec PROPERTY IMPORTED_LOCATION_RELEASE "{_posix(dec_release)}")
  endif()
  if(EXISTS "{_posix(dec_debug)}")
    set_property(TARGET unofficial::brotli::brotlidec PROPERTY IMPORTED_LOCATION_DEBUG "{_posix(dec_debug)}")
  endif()
  set_property(TARGET unofficial::brotli::brotlidec APPEND PROPERTY INTERFACE_LINK_LIBRARIES unofficial::brotli::brotlicommon)
endif()

if(NOT TARGET unofficial::brotli::brotlienc)
  add_library(unofficial::brotli::brotlienc UNKNOWN IMPORTED)
  set_target_properties(unofficial::brotli::brotlienc PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"
    IMPORTED_LOCATION "{enc_default.as_posix()}"
  )
  if(EXISTS "{_posix(enc_release)}")
    set_property(TARGET unofficial::brotli::brotlienc PROPERTY IMPORTED_LOCATION_RELEASE "{_posix(enc_release)}")
  endif()
  if(EXISTS "{_posix(enc_debug)}")
    set_property(TARGET unofficial::brotli::brotlienc PROPERTY IMPORTED_LOCATION_DEBUG "{_posix(enc_debug)}")
  endif()
  set_property(TARGET unofficial::brotli::brotlienc APPEND PROPERTY INTERFACE_LINK_LIBRARIES unofficial::brotli::brotlicommon)
endif()
"""
        version_text = f"""\
set(PACKAGE_VERSION "{version}")
if(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
  if(PACKAGE_FIND_VERSION STREQUAL PACKAGE_VERSION)
    set(PACKAGE_VERSION_EXACT TRUE)
  endif()
endif()
"""
        for name in ("unofficial-brotliConfig.cmake", "unofficial-brotli-config.cmake"):
            try:
                (cmake_dir / name).write_text(config_text, encoding="utf-8")
            except OSError:
                return
        for name in ("unofficial-brotliConfigVersion.cmake", "unofficial-brotli-config-version.cmake"):
            try:
                (cmake_dir / name).write_text(version_text, encoding="utf-8")
            except OSError:
                return

    def _ensure_ppmd_package(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        include_candidates = [
            (prefix / "include" / "minizip-ng").resolve(),
            (prefix / "include").resolve(),
        ]
        include_dir = next((candidate for candidate in include_candidates if candidate.exists()), None)

        release_lib: Path | None = None
        debug_lib: Path | None = None
        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))

        if self.platform.os == "windows":
            release_candidates = [
                libdir / "ppmd.lib",
                libdir / "libppmd.lib",
            ]
            debug_candidates = [
                libdir / f"ppmd{debug_postfix}.lib",
                libdir / f"libppmd{debug_postfix}.lib",
                libdir / "ppmdd.lib",
                libdir / "libppmdd.lib",
            ]
            release_lib = next((candidate for candidate in release_candidates if candidate.exists()), None)
            debug_lib = next((candidate for candidate in debug_candidates if candidate.exists()), None)

            if release_lib is None or debug_lib is None:
                matches = sorted(libdir.glob("*ppmd*.lib"))
                if matches:
                    if release_lib is None:
                        release_lib = next(
                            (m for m in matches if not m.stem.lower().endswith(debug_postfix.lower())),
                            None,
                        ) or matches[0]
                    if debug_lib is None:
                        debug_lib = next(
                            (m for m in matches if m.stem.lower().endswith(debug_postfix.lower()) or m.stem.lower().endswith("d")),
                            None,
                        ) or release_lib
        else:
            release_candidates = [
                libdir / "libppmd.a",
                libdir / "libppmd.so",
                libdir / "libppmd.dylib",
            ]
            debug_candidates = [
                libdir / "libppmdd.a",
                libdir / "libppmd_d.a",
                libdir / "libppmd.a",
            ]
            release_lib = next((candidate for candidate in release_candidates if candidate.exists()), None)
            debug_lib = next((candidate for candidate in debug_candidates if candidate.exists()), None)
            if release_lib is None and debug_lib is None:
                matches = sorted(libdir.glob("libppmd*"))
                if matches:
                    release_lib = matches[0]
                    debug_lib = matches[0]

        default_lib = debug_lib if build_type == "Debug" and debug_lib is not None else release_lib
        if default_lib is None:
            default_lib = release_lib or debug_lib
        if default_lib is None:
            return

        cmake_dir = libdir / "cmake" / "PPMD"
        try:
            cmake_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        include_path = include_dir.as_posix() if include_dir is not None else ""
        default_path = default_lib.as_posix()
        release_path = release_lib.as_posix() if release_lib is not None else ""
        debug_path = debug_lib.as_posix() if debug_lib is not None else ""

        include_prop = f'    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"\n' if include_path else ""
        config_text = f"""\
set(PPMD_FOUND TRUE)

if(NOT TARGET PPMD::PPMD)
  add_library(PPMD::PPMD UNKNOWN IMPORTED)
  set_target_properties(PPMD::PPMD PROPERTIES
{include_prop}    IMPORTED_LOCATION "{default_path}"
  )
  if(EXISTS "{release_path}")
    set_property(TARGET PPMD::PPMD PROPERTY IMPORTED_LOCATION_RELEASE "{release_path}")
  endif()
  if(EXISTS "{debug_path}")
    set_property(TARGET PPMD::PPMD PROPERTY IMPORTED_LOCATION_DEBUG "{debug_path}")
  endif()
endif()

set(PPMD_LIBRARY PPMD::PPMD)
set(PPMD_LIBRARIES PPMD::PPMD)
"""
        version_text = """\
set(PACKAGE_VERSION "1.0.0")
if(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
  if(PACKAGE_FIND_VERSION STREQUAL PACKAGE_VERSION)
    set(PACKAGE_VERSION_EXACT TRUE)
  endif()
endif()
"""

        for name in ("PPMDConfig.cmake", "ppmd-config.cmake"):
            try:
                (cmake_dir / name).write_text(config_text, encoding="utf-8")
            except OSError:
                return
        for name in ("PPMDConfigVersion.cmake", "ppmd-config-version.cmake"):
            try:
                (cmake_dir / name).write_text(version_text, encoding="utf-8")
            except OSError:
                return

    def _ensure_dng_sdk_lcms2_compat(self, prefix: Path, _build_type: str) -> None:
        if self.dry_run:
            return

        config_path = prefix / "lib" / "cmake" / "dng_sdk" / "dng_sdk-config.cmake"
        if not config_path.exists():
            return

        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError:
            return

        marker_begin = "# OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_BEGIN"
        marker_end = "# OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_END"
        if marker_begin in text and marker_end in text:
            return

        lines = text.splitlines()
        insert_at = next(
            (
                idx
                for idx, line in enumerate(lines)
                if "if((_dng_lcms2_release OR _dng_lcms2_debug) AND NOT TARGET dng_sdk::lcms2)" in line
            ),
            None,
        )
        if insert_at is None:
            return

        block = [
            "        # OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_BEGIN",
            "        # Some installs expose only one configuration for lcms2::lcms2.",
            "        # Mirror the available location so imported targets are valid",
            "        # across single- and multi-config generators.",
            "        if(NOT _dng_lcms2_release AND _dng_lcms2_debug)",
            "            set(_dng_lcms2_release \"${_dng_lcms2_debug}\")",
            "        endif()",
            "        if(NOT _dng_lcms2_debug AND _dng_lcms2_release)",
            "            set(_dng_lcms2_debug \"${_dng_lcms2_release}\")",
            "        endif()",
            "        # OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_END",
            "",
        ]
        lines[insert_at:insert_at] = block

        try:
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            return

    def _ensure_freetype_harfbuzz_compat(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        self._ensure_bzip2_package(prefix, build_type)
        self._ensure_harfbuzz_package(prefix, build_type)

        freetype_cfg = prefix / "lib" / "cmake" / "freetype" / "freetype-config.cmake"
        if not freetype_cfg.exists():
            return

        try:
            text = freetype_cfg.read_text(encoding="utf-8")
        except OSError:
            return

        compat_marker = "# oiio-builder: freetype mixed-target compatibility"
        fatal_block = """\
if(NOT _cmake_targets_defined STREQUAL "")
  string(REPLACE ";" ", " _cmake_targets_defined_text "${_cmake_targets_defined}")
  string(REPLACE ";" ", " _cmake_targets_not_defined_text "${_cmake_targets_not_defined}")
  message(FATAL_ERROR "Some (but not all) targets in this export set were already defined.\\nTargets Defined: ${_cmake_targets_defined_text}\\nTargets not yet defined: ${_cmake_targets_not_defined_text}\\n")
endif()
"""
        compat_block = f"""\
if(NOT _cmake_targets_defined STREQUAL "")
  {compat_marker}
  if(TARGET Freetype::Freetype AND NOT TARGET freetype)
    add_library(freetype INTERFACE IMPORTED)
    set_property(TARGET freetype PROPERTY INTERFACE_LINK_LIBRARIES Freetype::Freetype)
    unset(_cmake_targets_defined)
    unset(_cmake_targets_not_defined)
    unset(_cmake_expected_targets)
    unset(CMAKE_IMPORT_FILE_VERSION)
    cmake_policy(POP)
    return()
  elseif(TARGET freetype AND NOT TARGET Freetype::Freetype)
    add_library(Freetype::Freetype INTERFACE IMPORTED)
    set_property(TARGET Freetype::Freetype PROPERTY INTERFACE_LINK_LIBRARIES freetype)
    unset(_cmake_targets_defined)
    unset(_cmake_targets_not_defined)
    unset(_cmake_expected_targets)
    unset(CMAKE_IMPORT_FILE_VERSION)
    cmake_policy(POP)
    return()
  endif()
  string(REPLACE ";" ", " _cmake_targets_defined_text "${{_cmake_targets_defined}}")
  string(REPLACE ";" ", " _cmake_targets_not_defined_text "${{_cmake_targets_not_defined}}")
  message(FATAL_ERROR "Some (but not all) targets in this export set were already defined.\\nTargets Defined: ${{_cmake_targets_defined_text}}\\nTargets not yet defined: ${{_cmake_targets_not_defined_text}}\\n")
endif()
"""
        if compat_marker not in text and fatal_block in text:
            text = text.replace(fatal_block, compat_block, 1)

        marker = "# oiio-builder: freetype harfbuzz compatibility"
        needle = "# Compute the installation prefix relative to this file."
        if needle not in text:
            return

        shim = """\
# oiio-builder: freetype harfbuzz compatibility
include(CMakeFindDependencyMacro)
find_dependency(ZLIB QUIET)
find_dependency(BZip2 QUIET)
if(NOT TARGET HarfBuzz::HarfBuzz AND NOT TARGET harfbuzz::harfbuzz)
  find_dependency(HarfBuzz CONFIG QUIET)
endif()
if(TARGET HarfBuzz::HarfBuzz AND NOT TARGET harfbuzz::harfbuzz)
  add_library(harfbuzz::harfbuzz INTERFACE IMPORTED)
  set_property(TARGET harfbuzz::harfbuzz PROPERTY INTERFACE_LINK_LIBRARIES HarfBuzz::HarfBuzz)
endif()
if(TARGET harfbuzz::harfbuzz AND NOT TARGET HarfBuzz::HarfBuzz)
  add_library(HarfBuzz::HarfBuzz INTERFACE IMPORTED)
  set_property(TARGET HarfBuzz::HarfBuzz PROPERTY INTERFACE_LINK_LIBRARIES harfbuzz::harfbuzz)
endif()
if(APPLE)
  if(TARGET HarfBuzz::HarfBuzz)
    set_property(TARGET HarfBuzz::HarfBuzz APPEND PROPERTY INTERFACE_LINK_LIBRARIES "-framework CoreText")
  endif()
  if(TARGET harfbuzz::harfbuzz)
    set_property(TARGET harfbuzz::harfbuzz APPEND PROPERTY INTERFACE_LINK_LIBRARIES "-framework CoreText")
  endif()
endif()
"""

        if marker in text:
            start = text.find(marker)
            end = text.find(needle, start)
            if end == -1:
                return
            updated = text[:start] + shim + "\n" + text[end:]
        else:
            updated = text.replace(needle, shim + "\n" + needle, 1)

        # Some FreeType exports record only libbrotlidec, but static Brotli decode
        # also needs libbrotlicommon. Normalize the exported link list so both
        # libraries are present, and prefer config-specific libs on Windows.
        libdir = prefix / "lib"
        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))

        def _first_existing(candidates: list[Path]) -> Path | None:
            for candidate in candidates:
                if candidate.exists():
                    return candidate
            return None

        if self.platform.os == "windows":
            dec_release = _first_existing([libdir / "brotlidec.lib", libdir / "libbrotlidec.lib"])
            dec_debug = _first_existing(
                [
                    libdir / f"brotlidec{debug_postfix}.lib",
                    libdir / f"libbrotlidec{debug_postfix}.lib",
                    libdir / "brotlidecd.lib",
                    libdir / "libbrotlidecd.lib",
                ]
            )
            common_release = _first_existing([libdir / "brotlicommon.lib", libdir / "libbrotlicommon.lib"])
            common_debug = _first_existing(
                [
                    libdir / f"brotlicommon{debug_postfix}.lib",
                    libdir / f"libbrotlicommon{debug_postfix}.lib",
                    libdir / "brotlicommond.lib",
                    libdir / "libbrotlicommond.lib",
                ]
            )
        else:
            dec_release = _first_existing(
                [
                    libdir / "libbrotlidec.a",
                    libdir / "libbrotlidec-static.a",
                    libdir / "libbrotlidec.so",
                    libdir / "libbrotlidec.dylib",
                ]
            )
            dec_debug = dec_release
            common_release = _first_existing(
                [
                    libdir / "libbrotlicommon.a",
                    libdir / "libbrotlicommon-static.a",
                    libdir / "libbrotlicommon.so",
                    libdir / "libbrotlicommon.dylib",
                ]
            )
            common_debug = common_release

        def _cmake_path(path: Path | None) -> str | None:
            return path.resolve().as_posix() if path is not None else None

        def _cmake_config_expr(release: Path | None, debug: Path | None) -> str | None:
            release_path = _cmake_path(release)
            debug_path = _cmake_path(debug)
            if release_path and debug_path and release_path != debug_path:
                return f"\\$<$<CONFIG:Debug>:{debug_path}>;\\$<$<NOT:$<CONFIG:Debug>>:{release_path}>"
            if debug_path:
                return debug_path
            if release_path:
                return release_path
            return None

        dec_expr = _cmake_config_expr(dec_release, dec_debug)
        common_expr = _cmake_config_expr(common_release, common_debug)
        if dec_expr is not None or common_expr is not None:
            target_pattern = r'(set_target_properties\(freetype PROPERTIES[\s\S]*?INTERFACE_LINK_LIBRARIES ")([^"]*)(")'
            match = re.search(target_pattern, updated)
            if match:
                libs_value = match.group(2)
                parts = [part for part in libs_value.split(";") if part]
                rewritten: list[str] = []
                changed = False
                dec_present = False
                common_present = False

                for part in parts:
                    lower = part.lower()
                    if "brotlidec" in lower:
                        replacement = dec_expr if dec_expr is not None else part
                        rewritten.append(replacement)
                        if replacement != part:
                            changed = True
                        dec_present = True
                        continue
                    if "brotlicommon" in lower:
                        replacement = common_expr if common_expr is not None else part
                        rewritten.append(replacement)
                        if replacement != part:
                            changed = True
                        common_present = True
                        continue
                    rewritten.append(part)

                # Upstream FreeType currently links Brotli privately, so
                # exported targets may omit it entirely. Ensure static consumers
                # (Qt qsb, libjxl tools, etc.) get the required transitive libs.
                if not dec_present and dec_expr is not None:
                    rewritten.append(dec_expr)
                    dec_present = True
                    changed = True
                if dec_present and not common_present and common_expr is not None:
                    rewritten.append(common_expr)
                    changed = True

                libs_new = ";".join(rewritten)
                if changed and libs_new != libs_value:
                    updated = updated[: match.start(2)] + libs_new + updated[match.end(2) :]

        try:
            freetype_cfg.write_text(updated, encoding="utf-8")
        except OSError:
            return

    def _ensure_pystring_package(self, prefix: Path, build_type: str) -> None:
        include_dir = prefix / "include" / "pystring"
        if not include_dir.exists():
            include_dir = prefix / "include"
        if not include_dir.exists():
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        if self.platform.os == "windows":
            release_candidates = [libdir / "pystring.lib", libdir / f"pystring{debug_postfix}.lib"]
            debug_candidates = [libdir / f"pystring{debug_postfix}.lib", libdir / "pystring.lib"]
            fallback_pattern = "pystring*.lib"
        else:
            release_candidates = [libdir / "libpystring.a", libdir / "libpystringd.a", libdir / "libpystring_d.a"]
            debug_candidates = [libdir / "libpystringd.a", libdir / "libpystring_d.a", libdir / "libpystring.a"]
            fallback_pattern = "libpystring*.a"

        release_lib = next((candidate for candidate in release_candidates if candidate.exists()), None)
        debug_lib = next((candidate for candidate in debug_candidates if candidate.exists()), None)
        if release_lib is None and debug_lib is None:
            matches = sorted(libdir.glob(fallback_pattern))
            if matches:
                release_lib = matches[0]
                debug_lib = matches[0]
            else:
                return

        default_lib = release_lib or debug_lib
        if build_type == "Debug" and debug_lib is not None:
            default_lib = debug_lib
        if default_lib is None:
            return

        cmake_dir = libdir / "cmake" / "pystring"
        cmake_dir.mkdir(parents=True, exist_ok=True)

        include_path = include_dir.as_posix()
        default_path = default_lib.as_posix()
        release_path = release_lib.as_posix() if release_lib is not None else ""
        debug_path = debug_lib.as_posix() if debug_lib is not None else ""
        config_text = f"""\
set(pystring_FOUND TRUE)
set(pystring_INCLUDE_DIR "{include_path}")
set(pystring_INCLUDE_DIRS "{include_path}")

if(NOT TARGET pystring::pystring)
  add_library(pystring::pystring UNKNOWN IMPORTED)
  set_target_properties(pystring::pystring PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"
    IMPORTED_LOCATION "{default_path}"
  )
  if(EXISTS "{release_path}")
    set_property(TARGET pystring::pystring PROPERTY IMPORTED_LOCATION_RELEASE "{release_path}")
  endif()
  if(EXISTS "{debug_path}")
    set_property(TARGET pystring::pystring PROPERTY IMPORTED_LOCATION_DEBUG "{debug_path}")
  endif()
endif()

set(pystring_LIBRARY pystring::pystring)
set(pystring_LIBRARIES pystring::pystring)
"""
        version_text = """\
set(PACKAGE_VERSION "1.0.0")
if(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
  if(PACKAGE_FIND_VERSION STREQUAL PACKAGE_VERSION)
    set(PACKAGE_VERSION_EXACT TRUE)
  endif()
endif()
"""
        for name in ("pystring-config.cmake", "pystringConfig.cmake"):
            (cmake_dir / name).write_text(config_text, encoding="utf-8")
        for name in ("pystring-config-version.cmake", "pystringConfigVersion.cmake"):
            (cmake_dir / name).write_text(version_text, encoding="utf-8")

    def _ensure_aom_package(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        include_dir = prefix / "include"
        if not (include_dir / "aom" / "aom_decoder.h").exists():
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        release_lib: Path | None = None
        debug_lib: Path | None = None

        if self.platform.os == "windows":
            release_candidates = [libdir / "aom.lib", libdir / "libaom.lib"]
            debug_candidates = [libdir / f"aom{debug_postfix}.lib", libdir / "aomd.lib"]
            release_lib = next((p for p in release_candidates if p.exists()), None)
            debug_lib = next((p for p in debug_candidates if p.exists()), None)
            if release_lib is None or debug_lib is None:
                matches = sorted(libdir.glob("aom*.lib"))
                if release_lib is None:
                    release_lib = next(
                        (m for m in matches if not m.name.lower().endswith(f"{debug_postfix}.lib")),
                        None,
                    ) or (matches[0] if matches else None)
                if debug_lib is None:
                    debug_lib = next(
                        (m for m in matches if m.name.lower().endswith(f"{debug_postfix}.lib")),
                        None,
                    ) or release_lib
        else:
            static = libdir / "libaom.a"
            if static.exists():
                release_lib = static
                debug_lib = static
            else:
                matches = sorted(libdir.glob("libaom.*"))
                if matches:
                    release_lib = matches[0]
                    debug_lib = matches[0]

        if release_lib is None and debug_lib is None:
            return

        default_lib = release_lib or debug_lib
        if build_type == "Debug" and debug_lib is not None:
            default_lib = debug_lib
        if default_lib is None:
            return

        cmake_dir = libdir / "cmake" / "AOM"
        try:
            cmake_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return

        include_path = include_dir.as_posix()
        default_path = default_lib.as_posix()
        release_path = release_lib.as_posix() if release_lib is not None else ""
        debug_path = debug_lib.as_posix() if debug_lib is not None else ""
        config_text = f"""\
# Auto-generated by oiio-builder.
set(AOM_FOUND TRUE)
set(AOM_INCLUDE_DIR "{include_path}")
set(AOM_LIBRARY "{default_path}")

if(NOT TARGET AOM::aom)
  add_library(AOM::aom UNKNOWN IMPORTED)
  set_target_properties(AOM::aom PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "{include_path}"
    IMPORTED_LOCATION "{default_path}"
  )
  if(EXISTS "{release_path}")
    set_property(TARGET AOM::aom PROPERTY IMPORTED_LOCATION_RELEASE "{release_path}")
  endif()
  if(EXISTS "{debug_path}")
    set_property(TARGET AOM::aom PROPERTY IMPORTED_LOCATION_DEBUG "{debug_path}")
  endif()
endif()
"""
        for name in ("AOMConfig.cmake", "aom-config.cmake"):
            (cmake_dir / name).write_text(config_text, encoding="utf-8")

    def _ensure_libheif_aom_dependency(self, prefix: Path) -> None:
        if self.dry_run:
            return

        cmake_dir = prefix / "lib" / "cmake" / "libheif"
        if not cmake_dir.exists():
            return

        cfg_paths = [cmake_dir / "libheif-config.cmake", cmake_dir / "libheifConfig.cmake"]
        cfg_paths = [p for p in cfg_paths if p.exists()]
        if not cfg_paths:
            return

        marker = "# oiio-builder: libheif requires AOM"
        patch_lines = [
            "",
            marker,
            "include(CMakeFindDependencyMacro)",
            "find_dependency(AOM CONFIG)",
            "if(NOT TARGET AOM::aom AND TARGET aom)",
            "  add_library(AOM::aom ALIAS aom)",
            "endif()",
            "",
        ]

        for cfg_path in cfg_paths:
            try:
                text = cfg_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if marker in text:
                continue
            if "AOM::aom" not in text:
                continue

            lines = text.splitlines()
            insert_at: int | None = None
            for idx, line in enumerate(lines):
                if "Compute the installation prefix relative to this file." in line:
                    insert_at = idx
                    break
            if insert_at is None:
                for idx, line in enumerate(lines):
                    if "libheif-targets.cmake" in line or "libheifTargets.cmake" in line:
                        insert_at = idx
                        break
            if insert_at is None:
                for idx, line in enumerate(lines):
                    if line.lstrip().startswith("add_library("):
                        insert_at = idx
                        break
            if insert_at is None:
                insert_at = len(lines)

            new_text = "\n".join(lines[:insert_at] + patch_lines + lines[insert_at:]) + "\n"
            try:
                cfg_path.write_text(new_text, encoding="utf-8")
            except OSError:
                continue

    def _ensure_libheif_consumer_definitions(self, prefix: Path) -> None:
        if self.dry_run:
            return

        cmake_dir = prefix / "lib" / "cmake" / "libheif"
        if not cmake_dir.exists():
            return

        removed_names = {"LIBHEIF_EXPORTS", "HAVE_VISIBILITY"}

        def filter_defs(raw_defs: str) -> tuple[str, bool]:
            defs = [d for d in raw_defs.split(";") if d]
            filtered: list[str] = []
            for d in defs:
                name = d.split("=", 1)[0]
                if name in removed_names:
                    continue
                filtered.append(d)
            if filtered == defs:
                return raw_defs, False
            return ";".join(filtered), True

        def patch_property_line(line: str) -> tuple[str, bool]:
            needle = 'INTERFACE_COMPILE_DEFINITIONS "'
            start = line.find(needle)
            if start < 0:
                return line, False
            start_defs = start + len(needle)
            end_defs = line.find('"', start_defs)
            if end_defs < 0:
                return line, False
            raw_defs = line[start_defs:end_defs]
            new_defs, changed = filter_defs(raw_defs)
            if not changed:
                return line, False
            return line[:start_defs] + new_defs + line[end_defs:], True

        def patch_standalone_quoted_line(line: str) -> tuple[str, bool]:
            stripped = line.lstrip()
            if not stripped.startswith('"'):
                return line, False
            start = line.find('"')
            end = line.find('"', start + 1)
            if start < 0 or end < 0:
                return line, False
            raw_defs = line[start + 1 : end]
            new_defs, changed = filter_defs(raw_defs)
            if not changed:
                return line, False
            return line[: start + 1] + new_defs + line[end:], True

        for path in sorted(cmake_dir.glob("*.cmake")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if "INTERFACE_COMPILE_DEFINITIONS" not in text:
                continue
            if not any(name in text for name in removed_names):
                continue

            changed = False
            out_lines: list[str] = []
            pending_defs_line = False
            for line in text.splitlines():
                if pending_defs_line:
                    pending_defs_line = False
                    line, line_changed = patch_standalone_quoted_line(line)
                    if line_changed:
                        changed = True
                    out_lines.append(line)
                    continue

                line, line_changed = patch_property_line(line)
                if line_changed:
                    changed = True
                    out_lines.append(line)
                    continue

                if "INTERFACE_COMPILE_DEFINITIONS" in line:
                    pending_defs_line = True
                out_lines.append(line)

            if not changed:
                continue
            try:
                path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
            except OSError:
                continue

    def _ensure_libheif_windows_multiconfig_compat(self, prefix: Path) -> None:
        if self.dry_run or self.platform.os != "windows":
            return

        cmake_dir = prefix / "lib" / "cmake" / "libheif"
        cfg_path = cmake_dir / "libheif-config.cmake"
        if not cfg_path.exists():
            return

        marker_begin = "# OIIO_BUILDER_LIBHEIF_MULTICONFIG_BEGIN"
        marker_end = "# OIIO_BUILDER_LIBHEIF_MULTICONFIG_END"
        try:
            text = cfg_path.read_text(encoding="utf-8")
        except OSError:
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        lib_dir = prefix / "lib"

        def _has_lib(name: str) -> bool:
            return (lib_dir / name).exists()

        def _config_expr(stem: str) -> str | None:
            release_name = f"{stem}.lib"
            debug_name = f"{stem}{debug_postfix}.lib"
            has_release = _has_lib(release_name)
            has_debug = _has_lib(debug_name)
            if has_release and has_debug:
                return (
                    f"$<$<CONFIG:Debug>:${{_IMPORT_PREFIX}}/lib/{debug_name}>;"
                    f"$<$<NOT:$<CONFIG:Debug>>:${{_IMPORT_PREFIX}}/lib/{release_name}>"
                )
            if has_debug:
                return f"${{_IMPORT_PREFIX}}/lib/{debug_name}"
            if has_release:
                return f"${{_IMPORT_PREFIX}}/lib/{release_name}"
            return None

        link_parts: list[str] = []
        for stem in ("x265-static", "libde265", "libkvazaar", "libsharpyuv"):
            expr = _config_expr(stem)
            if expr is not None:
                link_parts.append(expr)
        if not link_parts:
            return

        heif_release_name = "heif.lib"
        heif_debug_name = f"heif{debug_postfix}.lib"
        has_heif_release = _has_lib(heif_release_name)
        has_heif_debug = _has_lib(heif_debug_name)
        if not has_heif_release and not has_heif_debug:
            return

        patch_lines = [
            "",
            f"  {marker_begin}",
            "  if (TARGET heif)",
            f"    if (EXISTS \"${{_IMPORT_PREFIX}}/lib/{heif_debug_name}\")",
            "      set_property(TARGET heif APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)",
            f"      set_target_properties(heif PROPERTIES IMPORTED_LOCATION_DEBUG \"${{_IMPORT_PREFIX}}/lib/{heif_debug_name}\")",
            f"    elseif (EXISTS \"${{_IMPORT_PREFIX}}/lib/{heif_release_name}\")",
            "      set_property(TARGET heif APPEND PROPERTY IMPORTED_CONFIGURATIONS DEBUG)",
            f"      set_target_properties(heif PROPERTIES IMPORTED_LOCATION_DEBUG \"${{_IMPORT_PREFIX}}/lib/{heif_release_name}\")",
            "    endif()",
            "    set_target_properties(heif PROPERTIES",
            f"      INTERFACE_LINK_LIBRARIES \"{';'.join(link_parts)}\"",
            "    )",
            "  endif()",
            f"  {marker_end}",
            "",
        ]
        patch_block = "\n".join(patch_lines)

        cleanup_anchor = "# Cleanup temporary variables."
        if marker_begin in text and marker_end in text:
            lines = text.splitlines()
            begin: int | None = None
            end: int | None = None
            for i, line in enumerate(lines):
                if marker_begin in line:
                    begin = i
                    break
            if begin is None:
                return
            for j in range(begin + 1, len(lines)):
                if marker_end in lines[j]:
                    end = j
                    break
            if end is None:
                return
            replacement = patch_block.rstrip("\n").splitlines()
            if lines[begin - 1 : end + 2] != replacement:
                # Keep one leading/trailing blank line around the marker block.
                start = begin - 1 if begin > 0 and lines[begin - 1].strip() == "" else begin
                stop = end + 2 if end + 1 < len(lines) and lines[end + 1].strip() == "" else end + 1
                lines[start:stop] = replacement
                try:
                    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                except OSError:
                    pass
            return

        anchor_index = text.find(cleanup_anchor)
        if anchor_index < 0:
            new_text = text.rstrip() + patch_block + "\n"
        else:
            new_text = text[:anchor_index] + patch_block + text[anchor_index:]
        try:
            cfg_path.write_text(new_text, encoding="utf-8")
        except OSError:
            return

    def _ensure_openjph_alias(self, prefix: Path) -> None:
        libdir = prefix / "lib"
        debug_lib = libdir / "libopenjph_d.a"
        release_lib = libdir / "libopenjph.a"
        if debug_lib.exists() and not release_lib.exists():
            try:
                release_lib.symlink_to(debug_lib.name)
            except OSError:
                release_lib.write_bytes(debug_lib.read_bytes())

    def _prune_lcms2_shared_artifacts(self, prefix: Path) -> None:
        if self.dry_run or self.platform.os != "windows":
            return

        libdir = prefix / "lib"
        bindir = prefix / "bin"
        if not libdir.is_dir() or not bindir.is_dir():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        static_candidates = [
            libdir / "lcms2_static.lib",
            libdir / f"lcms2_static{debug_postfix}.lib",
        ]
        if not any(p.exists() for p in static_candidates):
            return

        dll_candidates = [bindir / "lcms2.dll", bindir / f"lcms2{debug_postfix}.dll"]
        if not any(p.exists() for p in dll_candidates):
            return

        # A static prefix should not ship shared LCMS2 artifacts. Leaving stale
        # DLL/import-lib pairs in the shared Windows prefix can cause accidental
        # mixing of static and shared LCMS2 in downstream links (LNK2005/LNK1169).
        for path in dll_candidates + [libdir / "lcms2.lib", libdir / f"lcms2{debug_postfix}.lib"]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _ensure_libdeflate_alias(self, prefix: Path, build_type: str) -> None:
        if self.dry_run or self.platform.os != "windows":
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        release_source = libdir / "deflatestatic.lib"
        debug_source = libdir / f"deflatestatic{debug_postfix}.lib"
        if not release_source.exists() and not debug_source.exists():
            return
        if not debug_source.exists() and release_source.exists():
            debug_source = release_source

        def _materialize_alias(target: Path, source: Path) -> None:
            if target.exists() or not source.exists():
                return
            try:
                target.symlink_to(source.name)
            except OSError:
                shutil.copy2(source, target)

        _materialize_alias(libdir / "deflate.lib", release_source)
        _materialize_alias(libdir / f"deflate{debug_postfix}.lib", debug_source)
        if build_type == "Debug":
            # Some projects request explicit debug naming even in single-config generators.
            _materialize_alias(libdir / "deflated.lib", debug_source)

    def _ensure_zlib_windows_alias(self, prefix: Path, build_type: str) -> None:
        if self.dry_run or self.platform.os != "windows":
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        release_candidates = [
            libdir / "zlibstatic.lib",
            libdir / "zlib.lib",
            libdir / "zlib-ng.lib",
            libdir / "zlibng.lib",
        ]
        debug_candidates = [
            libdir / f"zlibstatic{debug_postfix}.lib",
            libdir / f"zlib{debug_postfix}.lib",
            libdir / "zlib_d.lib",
            libdir / "zlibd.lib",
            *release_candidates,
        ]

        release_source = next((p for p in release_candidates if p.exists()), None)
        debug_source = next((p for p in debug_candidates if p.exists()), None)
        if release_source is None and debug_source is None:
            return
        if release_source is None:
            release_source = debug_source
        if debug_source is None:
            debug_source = release_source

        def _materialize_alias(target: Path, source: Path | None) -> None:
            if source is None or not source.exists():
                return

            if target.exists() or target.is_symlink():
                try:
                    if target.is_symlink():
                        try:
                            if target.resolve() == source.resolve():
                                return
                        except OSError:
                            pass
                    else:
                        try:
                            st_target = target.stat()
                            st_source = source.stat()
                            if st_target.st_size == st_source.st_size and int(st_target.st_mtime) == int(st_source.st_mtime):
                                return
                        except OSError:
                            pass
                    target.unlink()
                except OSError:
                    return

            try:
                target.symlink_to(source.name)
            except OSError:
                shutil.copy2(source, target)

        if release_source is not None:
            _materialize_alias(libdir / "zlib.lib", release_source)
        if debug_source is not None:
            _materialize_alias(libdir / f"zlib{debug_postfix}.lib", debug_source)
            _materialize_alias(libdir / "zlibd.lib", debug_source)
            _materialize_alias(libdir / "zlib_d.lib", debug_source)
            if build_type == "Debug":
                _materialize_alias(libdir / "zlib_debug.lib", debug_source)

    def _ensure_openjph_windows_alias(self, prefix: Path, build_type: str) -> None:
        if self.dry_run or self.platform.os != "windows":
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        matches = sorted(libdir.glob("openjph*.lib"))
        if not matches:
            return

        # Prefer the "real" versioned libraries as sources, not our aliases,
        # to avoid self-referential or stale alias chains.
        alias_names = {"openjph.lib", "openjphd.lib", f"openjph{debug_postfix}.lib"}
        alias_names_lower = {name.lower() for name in alias_names}
        candidates = [m for m in matches if m.name.lower() not in alias_names_lower]
        if not candidates:
            candidates = matches

        release_candidates = [m for m in candidates if not m.name.lower().endswith(f"{debug_postfix}.lib")]
        debug_candidates = [m for m in candidates if m.name.lower().endswith(f"{debug_postfix}.lib")]
        release_source = release_candidates[0] if release_candidates else None
        debug_source = debug_candidates[0] if debug_candidates else None

        def _materialize_alias(target: Path, source: Path) -> None:
            if source is None or not source.exists():
                return

            # If a previous run created a wrong alias (common when building only
            # Release first), fix it rather than keeping a stale link.
            if target.exists() or target.is_symlink():
                try:
                    if target.is_symlink():
                        try:
                            if target.resolve() == source.resolve():
                                return
                        except OSError:
                            pass
                    else:
                        try:
                            st_target = target.stat()
                            st_source = source.stat()
                            if st_target.st_size == st_source.st_size and int(st_target.st_mtime) == int(st_source.st_mtime):
                                return
                        except OSError:
                            pass
                    target.unlink()
                except OSError:
                    return

            try:
                target.symlink_to(source.name)
            except OSError:
                shutil.copy2(source, target)

        # Release alias: never point this to a debug library.
        if release_source is not None:
            _materialize_alias(libdir / "openjph.lib", release_source)

        # Debug aliases: only create when the debug library actually exists.
        if debug_source is not None:
            _materialize_alias(libdir / f"openjph{debug_postfix}.lib", debug_source)
            if build_type == "Debug":
                _materialize_alias(libdir / "openjphd.lib", debug_source)

    def _ensure_bzip2_alias(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        libdir = prefix / "lib"
        if not libdir.exists():
            return

        def _materialize_alias(target: Path, source: Path) -> None:
            if target.exists() or not source.exists():
                return
            try:
                target.symlink_to(source.name)
            except OSError:
                shutil.copy2(source, target)

        if self.platform.os == "windows":
            debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
            release_source_candidates = [
                libdir / "bz2_static.lib",
                libdir / "libbz2_static.lib",
                libdir / "bz2.lib",
                libdir / "libbz2.lib",
            ]
            debug_source_candidates = [
                libdir / f"bz2_static{debug_postfix}.lib",
                libdir / f"libbz2_static{debug_postfix}.lib",
                libdir / f"bz2{debug_postfix}.lib",
                libdir / f"libbz2{debug_postfix}.lib",
            ]
            release_source = next((candidate for candidate in release_source_candidates if candidate.exists()), None)
            debug_source = next((candidate for candidate in debug_source_candidates if candidate.exists()), None)
            if debug_source is None and release_source is not None:
                debug_source = release_source

            if release_source is not None:
                _materialize_alias(libdir / "bz2.lib", release_source)
                _materialize_alias(libdir / "libbz2.lib", release_source)
            if debug_source is not None:
                _materialize_alias(libdir / f"bz2{debug_postfix}.lib", debug_source)
                _materialize_alias(libdir / f"libbz2{debug_postfix}.lib", debug_source)
            return

        release_source_candidates = [libdir / "libbz2_static.a", libdir / "libbz2.a"]
        debug_source_candidates = [libdir / "libbz2_staticd.a", libdir / "libbz2d.a", libdir / "libbz2_static.a", libdir / "libbz2.a"]
        release_source = next((candidate for candidate in release_source_candidates if candidate.exists()), None)
        debug_source = next((candidate for candidate in debug_source_candidates if candidate.exists()), None)
        if build_type == "Debug":
            if debug_source is not None:
                _materialize_alias(libdir / "libbz2.a", debug_source)
                _materialize_alias(libdir / "libbz2d.a", debug_source)
            elif release_source is not None:
                _materialize_alias(libdir / "libbz2.a", release_source)
                _materialize_alias(libdir / "libbz2d.a", release_source)
        elif release_source is not None:
            _materialize_alias(libdir / "libbz2.a", release_source)

    def _stamp_state(
        self, repo: RepoConfig, ctx: BuildContext, deps_heads: dict[str, str | None], cflags: str, cxxflags: str
    ) -> tuple[str, bool, str, str | None]:
        stamp_dir = self.config.global_cfg.build_root / ".stamps" / repo.name
        stamp_path = stamp_dir / f"{ctx.build_type}.json"
        existing = read_stamp(stamp_path)
        had_stamp = bool(existing)
        if self.force_all:
            return "build", had_stamp, "forced-all", None
        if self.force and self.force_targets and repo.name in self.force_targets:
            return "build", had_stamp, "forced", None
        if not existing:
            return "build", False, "no-stamp", None
        payload = self._stamp_payload(repo, ctx, deps_heads, cflags, cxxflags)
        current = compute_stamp(payload)
        if existing.get("stamp") == current:
            return "skip", True, "up-to-date", current
        return "build", True, "stamp-changed", current

    def _write_stamp(self, repo: RepoConfig, ctx: BuildContext, deps_heads: dict[str, str | None], cflags: str, cxxflags: str) -> str:
        stamp_dir = self.config.global_cfg.build_root / ".stamps" / repo.name
        stamp_path = stamp_dir / f"{ctx.build_type}.json"
        payload = self._stamp_payload(repo, ctx, deps_heads, cflags, cxxflags)
        payload["stamp"] = compute_stamp(payload)
        write_stamp(stamp_path, payload)
        return str(payload["stamp"])

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
        if repo.build_system == "cmake":
            effective = self._repo_cmake_effective_toml_options(repo.name)
            if effective.cache or effective.args:
                payload["cmake_cache_toml"] = effective.cache
                payload["cmake_args_toml"] = effective.args
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
        if repo.name == "openssl" and self.platform.os == "windows":
            zip_path = self._openssl_export_zip()
            payload["vcpkg_export_zip"] = str(zip_path)
            if zip_path.exists():
                st = zip_path.stat()
                payload["vcpkg_export_zip_size"] = int(st.st_size)
                payload["vcpkg_export_zip_mtime"] = int(st.st_mtime)
        if repo.name == "sqlite" and self.platform.os == "windows":
            zip_path = self._sqlite_export_zip()
            payload["vcpkg_export_zip"] = str(zip_path)
            if zip_path.exists():
                st = zip_path.stat()
                payload["vcpkg_export_zip_size"] = int(st.st_size)
                payload["vcpkg_export_zip_mtime"] = int(st.st_mtime)
        if repo.build_system == "qt6":
            qt_submodules = [
                "qtbase",
                "qtdeclarative",
                "qtshadertools",
                "qtmultimedia",
                "qtimageformats",
                "qtsvg",
                "qttools",
            ]
            if self.platform.os == "linux":
                qt_submodules.append("qtwayland")
            payload["qt6"] = {
                "submodules": qt_submodules,
                "mode": "debug" if ctx.build_type == "Debug" else "release",
                "opengl": "desktop" if self.platform.os in {"linux", "macos"} else "default",
                "qpa": ("xcb;wayland" if self.platform.os == "linux" else "default"),
                "qpa_default": ("xcb" if self.platform.os == "linux" else "default"),
                "ssl": ("openssl-linked" if self.platform.os in {"linux", "windows"} else "default"),
                "static_runtime": (self.platform.os == "windows"),
                "system_libs": {
                    "pcre": "system",
                    "zlib": "system",
                    "freetype": "system",
                    "harfbuzz": "system",
                    "libpng": "system",
                    "libjpeg": "system",
                    "tiff": "system",
                    "webp": "system",
                },
                "disabled_features": ["gstreamer", "pipewire"],
                "feature_ffmpeg": (self.platform.os != "windows" and self._ffmpeg_enabled()),
                "pkg_config_use_static_libs": True,
            }
        return payload

    def _dep_fingerprint(self, dep: str, build_type: str) -> str | None:
        """Return a stable fingerprint for a dependency suitable for stamps.

        Prefer the dependency's computed stamp (includes toolchain/flags/patch
        revisions) and fall back to its git head when no stamp is available.
        """
        stamp_path = self.config.global_cfg.build_root / ".stamps" / dep / f"{build_type}.json"
        existing = read_stamp(stamp_path)
        if existing:
            stamp_value = existing.get("stamp")
            if isinstance(stamp_value, str) and stamp_value:
                return f"stamp:{stamp_value}"
        dep_dir = self.repo_paths.get(dep)
        if dep_dir:
            return git_head(dep_dir)
        return None

    def _post_install_repo(self, repo: RepoConfig, install_prefix: Path, build_type: str) -> None:
        recipe_registry.post_install(repo.name, self, install_prefix, build_type)

    def _cmake_cache_value(self, cache_path: Path, key: str) -> str | None:
        try:
            text = cache_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        prefix = f"{key}:"
        for line in text.splitlines():
            if not line or line.startswith(("//", "#")):
                continue
            if not line.startswith(prefix):
                continue
            _, _, value = line.partition("=")
            value = value.strip()
            return value or None
        return None

    def _cmake_cache_vars_referencing_prefix(self, cache_path: Path, prefix: str) -> list[str]:
        """Return cache variable names whose values mention `prefix`.

        Used when the install prefix changes and we need to reconfigure in-place
        without reusing stale find_* cache results that still point at the old
        prefix.
        """
        prefix = prefix.strip()
        if not prefix:
            return []

        needle = prefix.replace("\\", "/")
        needle_norm = needle.rstrip("/")
        needles = {needle, needle_norm} if needle_norm else {needle}

        try:
            lines = cache_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

        hits: set[str] = set()
        for line in lines:
            if not line or line.startswith(("//", "#")):
                continue
            colon = line.find(":")
            if colon <= 0:
                continue
            eq = line.find("=", colon + 1)
            if eq <= colon:
                continue
            name = line[:colon].strip()
            if not name:
                continue
            value = line[eq + 1 :].strip()
            if not value:
                continue
            value_norm = value.replace("\\", "/")
            if any(n in value_norm for n in needles):
                hits.add(name)

        return sorted(hits)

    def _cmake_install_only(self, ctx: BuildContext, env: dict[str, str]) -> bool:
        if not (ctx.build_dir / "cmake_install.cmake").exists():
            return False
        cache_path = ctx.build_dir / "CMakeCache.txt"
        if not cache_path.exists():
            return False

        cached_prefix = self._cmake_cache_value(cache_path, "CMAKE_INSTALL_PREFIX")
        desired_prefix = os.path.normcase(os.path.normpath(str(ctx.install_prefix)))
        cached_prefix_norm = os.path.normcase(os.path.normpath(cached_prefix)) if cached_prefix else ""

        if cached_prefix_norm and cached_prefix_norm != desired_prefix:
            cmd = ["cmake", "-S", str(ctx.src_dir), "-B", str(ctx.build_dir)]
            cmd.extend(self._cmake_generator_args())
            stale_cache_vars = self._cmake_cache_vars_referencing_prefix(cache_path, cached_prefix or "")
            if stale_cache_vars:
                print(
                    f"[note] {ctx.repo.name} ({ctx.build_type}) prefix changed; clearing {len(stale_cache_vars)} stale CMake cache entries",
                    flush=True,
                )
                for name in stale_cache_vars:
                    cmd.extend(["-U", name])
            cmake_args = self._cmake_common_args(ctx.repo, ctx)
            cmake_args.extend(self._repo_specific_args(ctx.repo, ctx))
            cmake_args.extend(self._expand_args(ctx.repo.cmake_args, ctx.build_type, ctx.install_prefix))
            cmake_args.extend(self._repo_cmake_user_override_args(ctx.repo.name))
            cmd.extend(cmake_args)
            print_cmd("Full cmake config command", cmd)
            banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
            run(
                cmd,
                env=env,
                dry_run=self.dry_run,
                log_path=str(self._repo_log_path(ctx.repo.name, ctx.build_type, "configure")),
            )

        install_cmd = ["cmake", "--install", str(ctx.build_dir), "--config", ctx.build_type, "--prefix", str(ctx.install_prefix)]
        print_cmd("install command", install_cmd)
        banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
        run(
            install_cmd,
            env=env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
        )
        return True

    def _autotools_install_only(self, repo: RepoConfig, ctx: BuildContext, env: dict[str, str]) -> bool:
        if not (ctx.build_dir / "Makefile").exists():
            return False
        configure = ctx.src_dir / "configure"
        if not configure.exists():
            return False
        use_msys2_autotools = self._autotools_windows_msys2_active()
        if self.platform.os == "windows" and not use_msys2_autotools:
            return False
        cflags, cxxflags, ldflags = self._non_cmake_flags(ctx.build_type)
        include_dir = ctx.install_prefix / "include"
        lib_dir = ctx.install_prefix / "lib"
        include_arg = include_dir.as_posix() if use_msys2_autotools else str(include_dir)
        lib_arg = lib_dir.as_posix() if use_msys2_autotools else str(lib_dir)
        prefix_arg = ctx.install_prefix.as_posix() if use_msys2_autotools else str(ctx.install_prefix)
        install_env = {
            **env,
            "CFLAGS": f"{cflags} -I{include_arg}",
            "CXXFLAGS": f"{cxxflags} -I{include_arg}",
            "LDFLAGS": f"{ldflags} -L{lib_arg}",
            "CPPFLAGS": f"-I{include_arg}",
        }
        configure_args = [f"--prefix={prefix_arg}", "--disable-shared", "--enable-static", *self._autotools_args(repo)]
        cmd = self._autotools_configure_command(configure, configure_args, install_env)
        print_cmd("configure command", cmd)
        banner(f"{repo.name} ({ctx.build_type}) - configure")
        run(
            cmd,
            cwd=str(ctx.build_dir),
            env=install_env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(repo.name, ctx.build_type, "configure")),
        )

        install_cmd = self._autotools_make_command(["install"], install_env)
        print_cmd("install command", install_cmd)
        banner(f"{repo.name} ({ctx.build_type}) - install")
        run(
            install_cmd,
            cwd=str(ctx.build_dir),
            env=install_env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(repo.name, ctx.build_type, "install")),
        )
        return True

    def _ffmpeg_install_only(self, ctx: BuildContext, env: dict[str, str]) -> bool:
        if not (ctx.build_dir / "Makefile").exists():
            return False
        configure = ctx.src_dir / "configure"
        if not configure.exists():
            return False
        self._ensure_ffmpeg_posix_line_endings(ctx.src_dir)
        ffmpeg_args = self._ffmpeg_configure_args(ctx)
        cmd = self._ffmpeg_configure_command(configure, ffmpeg_args, env)
        print_cmd("configure command", cmd)
        banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
        run(
            cmd,
            cwd=str(ctx.build_dir),
            env=env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(ctx.repo.name, ctx.build_type, "configure")),
        )

        install_cmd = self._ffmpeg_make_command(["install"], env)
        print_cmd("install command", install_cmd)
        banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
        run(
            install_cmd,
            cwd=str(ctx.build_dir),
            env=env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
        )
        return True

    def _giflib_install_only(self, repo: RepoConfig, ctx: BuildContext, env: dict[str, str]) -> bool:
        if self.platform.os == "windows":
            if not (ctx.build_dir / "cmake_install.cmake").exists():
                return False
            cmake_src_dir = ctx.build_dir / "_giflib_cmake"
            cmake_src_dir.mkdir(parents=True, exist_ok=True)
            cmake_lists = cmake_src_dir / "CMakeLists.txt"
            if not cmake_lists.exists():
                return False
            cmd = ["cmake", "--install", str(ctx.build_dir), "--config", ctx.build_type, "--prefix", str(ctx.install_prefix)]
            print_cmd("install command", cmd)
            banner(f"{repo.name} ({ctx.build_type}) - install")
            run(
                cmd,
                env=env,
                dry_run=self.dry_run,
                log_path=str(self._repo_log_path(repo.name, ctx.build_type, "install")),
            )
            return True

        # POSIX giflib builds in the source tree (one build at a time), so install-only is unsafe when
        # multiple build types are enabled. Fall back to full rebuild+install.
        return False

    def _build_cpython_posix(self, ctx: BuildContext, env: dict[str, str]) -> None:
        build_dir = ctx.build_dir
        src_dir = ctx.src_dir
        install_prefix = ctx.install_prefix
        configure = src_dir / "configure"
        if not configure.exists():
            raise RuntimeError(f"Missing configure script for cpython: {configure}")

        if not self.dry_run and (build_dir / "Makefile").exists():
            shutil.rmtree(build_dir, ignore_errors=True)
        build_dir.mkdir(parents=True, exist_ok=True)

        cflags, cxxflags, ldflags = self._non_cmake_flags(ctx.build_type)
        py_env = dict(env)
        if "cc" in self.toolchain:
            py_env["CC"] = self.toolchain["cc"]
        if "cxx" in self.toolchain:
            py_env["CXX"] = self.toolchain["cxx"]
        if "ar" in self.toolchain:
            py_env["AR"] = self.toolchain["ar"]
        if "ranlib" in self.toolchain:
            py_env["RANLIB"] = self.toolchain["ranlib"]
        if cflags:
            py_env["CFLAGS"] = cflags
        if cxxflags:
            py_env["CXXFLAGS"] = cxxflags
        if ldflags:
            py_env["LDFLAGS"] = ldflags

        configure_cmd = [
            str(configure),
            f"--prefix={install_prefix}",
            "--enable-shared",
            "--without-ensurepip",
        ]
        print_cmd("configure command", configure_cmd)
        banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
        run(
            configure_cmd,
            cwd=str(build_dir),
            env=py_env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(ctx.repo.name, ctx.build_type, "configure")),
        )

        build_cmd = ["make", f"-j{self._jobs()}"]
        print_cmd("build command", build_cmd)
        banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
        run(
            build_cmd,
            cwd=str(build_dir),
            env=py_env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(ctx.repo.name, ctx.build_type, "build")),
        )

        install_cmd = ["make", "install"]
        print_cmd("install command", install_cmd)
        banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
        run(
            install_cmd,
            cwd=str(build_dir),
            env=py_env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
        )

    def _build_cpython_windows(self, ctx: BuildContext, env: dict[str, str]) -> None:
        src_dir = ctx.src_dir
        install_prefix = ctx.install_prefix
        build_script = src_dir / "PCbuild" / "build.bat"
        if not build_script.exists():
            raise RuntimeError(f"Missing CPython Windows build script: {build_script}")

        if self.platform.arch == "x86_64":
            pcbuild_platform = "x64"
            output_dirs = ["amd64", "x64"]
        elif self.platform.arch == "arm64":
            pcbuild_platform = "arm64"
            output_dirs = ["arm64", "ARM64"]
        else:
            raise RuntimeError(f"Unsupported Windows architecture for cpython: {self.platform.arch}")

        config_name = "Debug" if ctx.build_type == "Debug" else "Release"
        fetch_externals = self._windows_cpython_fetch_externals()

        self._ensure_zlib_windows_alias(install_prefix, ctx.build_type)
        self._ensure_bzip2_alias(install_prefix, ctx.build_type)

        py_env = dict(env)
        # Avoid inheriting host-shell compiler/linker flags (for example CL=/Yu...)
        # that can conflict with CPython's own PCbuild project settings.
        for var in ("CL", "_CL_", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "LINK"):
            py_env[var] = ""
        # Explicitly disable forced PCH use if any /Yu slips in from project/user overrides.
        py_env["_CL_"] = "/Y-"
        # Preserve host toolchain paths and prepend our prefix paths as needed.
        py_env.setdefault("PATH", os.environ.get("PATH", ""))
        py_env.setdefault("INCLUDE", os.environ.get("INCLUDE", ""))
        py_env.setdefault("LIB", os.environ.get("LIB", ""))
        py_env.setdefault("LIBPATH", os.environ.get("LIBPATH", ""))

        include_dir = install_prefix / "include"
        lib_dir = install_prefix / "lib"
        bin_dir = install_prefix / "bin"
        if include_dir.is_dir():
            self._prepend_windows_env_paths(py_env, "INCLUDE", [include_dir])
        if lib_dir.is_dir():
            self._prepend_windows_env_paths(py_env, "LIB", [lib_dir])
            self._prepend_windows_env_paths(py_env, "LIBPATH", [lib_dir])
        if bin_dir.is_dir():
            self._prepend_windows_env_paths(py_env, "PATH", [bin_dir])

        if not self.dry_run:
            pcbuild_root = src_dir / "PCbuild"
            for name in output_dirs:
                candidate = pcbuild_root / name
                if candidate.exists():
                    shutil.rmtree(candidate, ignore_errors=True)
            obj_dir = pcbuild_root / "obj"
            if obj_dir.exists():
                shutil.rmtree(obj_dir, ignore_errors=True)

        build_cmd = [
            "cmd",
            "/c",
            str(build_script),
            "-p",
            pcbuild_platform,
            "-c",
            config_name,
            "--no-tkinter",
        ]
        if fetch_externals:
            build_cmd.append("-e")
        else:
            build_cmd.append("-E")
        print_cmd("build command", build_cmd)
        banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
        run(
            build_cmd,
            cwd=str(src_dir),
            env=py_env,
            dry_run=self.dry_run,
            log_path=str(self._repo_log_path(ctx.repo.name, ctx.build_type, "build")),
        )

        if self.dry_run:
            return

        pcbuild_root = src_dir / "PCbuild"
        output_dir: Path | None = None
        for name in output_dirs:
            candidate = pcbuild_root / name
            if candidate.is_dir() and list(candidate.glob("python*.lib")):
                output_dir = candidate
                break
        if output_dir is None:
            for candidate in sorted(pcbuild_root.iterdir()):
                if candidate.is_dir() and list(candidate.glob("python*.lib")):
                    output_dir = candidate
                    break
        if output_dir is None:
            raise RuntimeError(f"Could not locate CPython build output under: {pcbuild_root}")

        include_dst = install_prefix / "include"
        lib_dst = install_prefix / "lib"
        libs_compat_dst = install_prefix / "libs"
        bin_dst = install_prefix / "bin"
        include_dst.mkdir(parents=True, exist_ok=True)
        lib_dst.mkdir(parents=True, exist_ok=True)
        libs_compat_dst.mkdir(parents=True, exist_ok=True)
        bin_dst.mkdir(parents=True, exist_ok=True)

        include_src = src_dir / "Include"
        if include_src.is_dir():
            shutil.copytree(include_src, include_dst, dirs_exist_ok=True)
        for pyconfig_candidate in (src_dir / "PC" / "pyconfig.h", src_dir / "PCbuild" / "pyconfig.h"):
            if pyconfig_candidate.exists():
                shutil.copy2(pyconfig_candidate, include_dst / "pyconfig.h")
                break

        for lib_file in sorted(output_dir.glob("python*.lib")):
            shutil.copy2(lib_file, lib_dst / lib_file.name)
            shutil.copy2(lib_file, libs_compat_dst / lib_file.name)

        for pattern in ("python*.dll", "python*.exe", "python*.pdb"):
            for file_path in sorted(output_dir.glob(pattern)):
                shutil.copy2(file_path, install_prefix / file_path.name)
                shutil.copy2(file_path, bin_dst / file_path.name)

        stdlib_src = src_dir / "Lib"
        if stdlib_src.is_dir():
            shutil.copytree(stdlib_src, install_prefix / "Lib", dirs_exist_ok=True)

        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        debug_libs = list(lib_dst.glob(f"python*{debug_postfix}.lib")) + list(lib_dst.glob(f"python*_{debug_postfix}.lib"))
        if ctx.build_type == "Debug" and not debug_libs:
            release_libs = [p for p in sorted(lib_dst.glob("python*.lib")) if not p.name.lower().endswith(f"{debug_postfix}.lib")]
            if release_libs:
                source = release_libs[0]
                fallback_name = f"{source.stem}{debug_postfix}.lib"
                shutil.copy2(source, lib_dst / fallback_name)
                shutil.copy2(source, libs_compat_dst / fallback_name)

    def _install_only(self, repo: RepoConfig, ctx: BuildContext, env: dict[str, str]) -> bool:
        if repo.build_system == "cmake":
            return self._cmake_install_only(ctx, env)
        if repo.build_system == "autotools":
            return self._autotools_install_only(repo, ctx, env)
        if repo.build_system == "ffmpeg":
            return self._ffmpeg_install_only(ctx, env)
        if repo.build_system == "giflib":
            return self._giflib_install_only(repo, ctx, env)
        return False

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

        state, had_stamp, reason, current_stamp = self._stamp_state(repo, ctx, deps_heads, cflags, cxxflags)
        if state == "skip":
            if current_stamp is None:
                raise RuntimeError(f"Internal error: missing computed stamp for {repo.name} ({build_type})")
            marker_path = self._install_marker_path(install_prefix, repo.name, build_type)
            marker = self._read_install_marker(marker_path)
            desired_prefix_norm = os.path.normcase(os.path.normpath(str(install_prefix)))
            marker_stamp = marker.get("build_stamp") if isinstance(marker, dict) else None
            marker_prefix = marker.get("install_prefix") if isinstance(marker, dict) else None
            marker_stamp_ok = isinstance(marker_stamp, str) and marker_stamp == current_stamp
            marker_prefix_ok = (
                isinstance(marker_prefix, str) and os.path.normcase(os.path.normpath(marker_prefix)) == desired_prefix_norm
            )
            marker_ok = bool(marker) and marker_stamp_ok and marker_prefix_ok
            reinstall_reason = ""
            if self._reinstall_requested(repo.name):
                reinstall_reason = "requested"
            elif not marker:
                reinstall_reason = "marker-missing"
            elif not marker_stamp_ok:
                reinstall_reason = "marker-stamp-mismatch"
            elif not marker_prefix_ok:
                reinstall_reason = "marker-prefix-mismatch"

            if not reinstall_reason and marker_ok:
                print(f"[skip] {repo.name} ({build_type}) up-to-date")
                return "skipped", reason

            env = self._env_for_repo_build(repo, build_type, install_prefix)
            print(f"[reinstall] {repo.name} ({build_type}) -> {install_prefix} ({reinstall_reason})", flush=True)
            if self._install_only(repo, ctx, env):
                self._post_install_repo(repo, install_prefix, build_type)
                if not self.dry_run:
                    self._write_install_marker(repo, ctx, current_stamp)
                return "reinstalled", reinstall_reason
            # Fall back to a full build+install when install-only isn't available.
            print(f"[note] {repo.name} ({build_type}) reinstall requires rebuild (no install-only support)", flush=True)

        banner(f"{repo.name} ({build_type})", color="cyan")

        env = self._env_for_repo_build(repo, build_type, install_prefix)

        # Prefix compatibility shims that some downstream projects rely on.
        # These are cheap no-ops if the relevant files don't exist yet.
        if "libdeflate" in repo.deps:
            self._ensure_libdeflate_alias(install_prefix, build_type)
        # libjxl and other consumers may pull OpenJPH transitively via OpenEXR.
        if "openjph" in repo.deps or "openexr" in repo.deps:
            self._ensure_openjph_windows_alias(install_prefix, build_type)
        if "libheif" in repo.deps:
            self._ensure_aom_package(install_prefix, build_type)
            self._ensure_libheif_aom_dependency(install_prefix)
            self._ensure_libheif_consumer_definitions(install_prefix)
            self._ensure_libheif_windows_multiconfig_compat(install_prefix)

        self._prepare_repo_source(repo, src_dir)
        if repo.name == "libjxl":
            self._make_openexr_pc_override(install_prefix, build_type)
        if repo.name == "libjxl" and build_type == "Debug":
            self._ensure_openjph_alias(install_prefix)
        if repo.name in {"OpenColorIO", "OpenImageIO"}:
            self._ensure_pystring_package(install_prefix, build_type)
        if repo.name in {"libraw", "OpenImageIO"}:
            self._ensure_dng_sdk_lcms2_compat(install_prefix, build_type)
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
            cmake_args.extend(self._repo_cmake_user_override_args(repo.name))
            cmd.extend(cmake_args)

            print_cmd("Full cmake config command", cmd)
            banner(f"{repo.name} ({build_type}) - configure")
            run(cmd, env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "configure")))

            build_cmd = ["cmake", "--build", str(build_dir), "--config", build_type, "--parallel", str(self._jobs())]
            print_cmd("build command", build_cmd)
            banner(f"{repo.name} ({build_type}) - building")
            run(build_cmd, env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "build")))

            install_cmd = ["cmake", "--install", str(build_dir), "--config", build_type]
            print_cmd("install command", install_cmd)
            banner(f"{repo.name} ({build_type}) - install")
            run(install_cmd, env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "install")))
        elif repo.build_system == "autotools":
            build_dir.mkdir(parents=True, exist_ok=True)
            configure = src_dir / "configure"
            if not configure.exists():
                raise RuntimeError(f"Missing configure script for {repo.name}: {configure}")
            use_msys2_autotools = self._autotools_windows_msys2_active()
            if self.platform.os == "windows" and not use_msys2_autotools:
                raise RuntimeError(
                    f"{repo.name}: Windows autotools builds require MSYS2 shell/tools in PATH "
                    "(MSYSTEM set, plus bash+make)."
                )
            cflags, cxxflags, ldflags = self._non_cmake_flags(build_type)
            include_dir = install_prefix / "include"
            lib_dir = install_prefix / "lib"
            include_arg = include_dir.as_posix() if use_msys2_autotools else str(include_dir)
            lib_arg = lib_dir.as_posix() if use_msys2_autotools else str(lib_dir)
            prefix_arg = install_prefix.as_posix() if use_msys2_autotools else str(install_prefix)
            env = {
                **env,
                "CFLAGS": f"{cflags} -I{include_arg}",
                "CXXFLAGS": f"{cxxflags} -I{include_arg}",
                "LDFLAGS": f"{ldflags} -L{lib_arg}",
                "CPPFLAGS": f"-I{include_arg}",
            }
            configure_args = [f"--prefix={prefix_arg}", "--disable-shared", "--enable-static", *self._autotools_args(repo)]
            cmd = self._autotools_configure_command(configure, configure_args, env)
            print_cmd("configure command", cmd)
            banner(f"{repo.name} ({build_type}) - configure")
            run(cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "configure")))

            build_cmd = self._autotools_make_command([f"-j{self._jobs()}"], env)
            print_cmd("build command", build_cmd)
            banner(f"{repo.name} ({build_type}) - building")
            run(build_cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "build")))

            install_cmd = self._autotools_make_command(["install"], env)
            print_cmd("install command", install_cmd)
            banner(f"{repo.name} ({build_type}) - install")
            run(install_cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "install")))
        elif repo.build_system == "ffmpeg":
            build_dir.mkdir(parents=True, exist_ok=True)
            configure = src_dir / "configure"
            if not configure.exists():
                raise RuntimeError(f"Missing configure script for {repo.name}: {configure}")
            self._ensure_ffmpeg_posix_line_endings(src_dir)
            ffmpeg_args = self._ffmpeg_configure_args(ctx)
            cmd = self._ffmpeg_configure_command(configure, ffmpeg_args, env)
            print_cmd("configure command", cmd)
            banner(f"{repo.name} ({build_type}) - configure")
            run(cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "configure")))

            build_cmd = self._ffmpeg_make_command([f"-j{self._jobs()}"], env)
            print_cmd("build command", build_cmd)
            banner(f"{repo.name} ({build_type}) - building")
            run(build_cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "build")))

            install_cmd = self._ffmpeg_make_command(["install"], env)
            print_cmd("install command", install_cmd)
            banner(f"{repo.name} ({build_type}) - install")
            run(install_cmd, cwd=str(build_dir), env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "install")))
        elif repo.build_system == "cpython":
            if self.platform.os == "windows":
                self._build_cpython_windows(ctx, env)
            elif self.platform.os in {"linux", "macos"}:
                self._build_cpython_posix(ctx, env)
            else:
                raise RuntimeError(f"Unsupported platform for cpython build: {self.platform.os}")
        elif repo.build_system == "qt6":
            configure = src_dir / ("configure.bat" if self.platform.os == "windows" else "configure")
            if not configure.exists():
                raise RuntimeError(f"Missing Qt configure script for {repo.name}: {configure}")

            # Qt's WrapBrotli finder prefers a vcpkg-style `unofficial-brotli` package, otherwise it
            # falls back to pkg-config. Static builds may miss `libbrotlicommon` in the pkg-config
            # branch, so provide a tiny config shim when brotli is present in the prefix.
            self._ensure_unofficial_brotli_package(install_prefix, build_type)
            self._ensure_freetype_harfbuzz_compat(install_prefix, build_type)
            self._ensure_jasper_package(install_prefix, build_type)

            # Rebuild from a clean build tree to avoid confusing incremental states.
            if not self.dry_run:
                if build_dir.exists():
                    shutil.rmtree(build_dir, ignore_errors=True)
                build_dir.mkdir(parents=True, exist_ok=True)
            else:
                build_dir.mkdir(parents=True, exist_ok=True)

            qt_env = dict(env)
            if self.platform.os == "windows":
                # Avoid inheriting host-shell compiler flags (e.g. CL=/RTC1 /Zi)
                # that can conflict with the explicit CMake flags we pass for Qt.
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

            qt_submodules = self._qt6_submodules()

            if self.platform.os == "linux" and "qtwayland" in qt_submodules:
                if not shutil.which("wayland-scanner"):
                    raise RuntimeError(
                        "Qt6: wayland-scanner not found. Install Wayland development tools (wayland-scanner) to build qtwayland."
                    )

            pulse_ok = False
            alsa_ok = False
            if self.platform.os == "linux":
                pulse_ok = subprocess.run(["pkg-config", "--exists", "libpulse"], env=env, check=False).returncode == 0
                alsa_ok = subprocess.run(["pkg-config", "--exists", "alsa"], env=env, check=False).returncode == 0
                if self._ffmpeg_enabled() and not pulse_ok and not alsa_ok:
                    print(
                        "[note] Qt6: neither libpulse nor alsa dev packages were found via pkg-config. "
                        "QtMultimedia audio backends may be limited.",
                        flush=True,
                    )

            qt_args: list[str] = [
                "-prefix",
                str(install_prefix),
                "-extprefix",
                str(install_prefix),
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
                "-system-tiff",
                "-system-webp",
                "-no-feature-gstreamer",
                "-no-feature-pipewire",
            ]

            if build_type == "Debug":
                qt_args.append("-debug")
            else:
                qt_args.append("-release")

            if self.platform.os in {"linux", "macos"}:
                qt_args.extend(["-opengl", "desktop"])

            if self.platform.os == "linux":
                qt_args.extend(["-qpa", "xcb;wayland", "-default-qpa", "xcb"])
                qt_args.append("-no-gtk")

            if self.platform.os in {"linux", "windows"}:
                qt_args.append("-openssl-linked")
            if self.platform.os == "windows":
                qt_args.extend(["-static-runtime", "-no-schannel"])

            if self._ffmpeg_enabled():
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
                f"-DCMAKE_PREFIX_PATH={install_prefix}",
                f"-DCMAKE_INCLUDE_PATH={install_prefix / 'include'}",
                f"-DCMAKE_LIBRARY_PATH={install_prefix / 'lib'}",
                "-DPKG_CONFIG_USE_STATIC_LIBS=ON",
            ]
            freetype_dir = install_prefix / "lib" / "cmake" / "freetype"
            if freetype_dir.exists():
                cmake_args.append(f"-DFreetype_DIR={freetype_dir}")
            if self.platform.os == "macos":
                # Qt's Apple checks require xcodebuild; allow CLT-only setups by
                # supplying a reasonable Xcode version override.
                cmake_args.append("-DQT_INTERNAL_XCODE_VERSION=15.0")
            if self.config.global_cfg.pic:
                cmake_args.append("-DCMAKE_POSITION_INDEPENDENT_CODE=ON")

            cflags = self._base_flags(build_type)
            cxxflags = self._base_flags(build_type)
            if self.platform.os == "windows":
                cmake_args.append("-DCMAKE_POLICY_DEFAULT_CMP0091=NEW")
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

                # Qt's configure performs internal try_compile checks that may use
                # Debug flags even for a Release configure. Without explicit Debug
                # flag overrides, CMake's default /RTC1 can mix with our injected
                # /O2 from CMAKE_*_FLAGS_INIT and fail with D8016.
                if build_type != "Debug":
                    cmake_args.append("-DCMAKE_C_FLAGS_DEBUG=/Od /Zi")
                    cmake_args.append("-DCMAKE_CXX_FLAGS_DEBUG=/Od /Zi /bigobj")

                # CMake's FindPNG module on Windows often misses libpng static
                # names like libpng18_static[d].lib. Provide direct hints so
                # Qt's WrapSystemPNG check can succeed when using -system-libpng.
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
                # Qt's configure forwards CMake args via a whitespace split; avoid
                # embedding space-separated flags in -D arguments on POSIX.
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

            if self.toolchain:
                if "cc" in self.toolchain:
                    cmake_args.append(f"-DCMAKE_C_COMPILER={self.toolchain['cc']}")
                if "cxx" in self.toolchain:
                    cmake_args.append(f"-DCMAKE_CXX_COMPILER={self.toolchain['cxx']}")
                if "ld" in self.toolchain:
                    cmake_args.append(f"-DCMAKE_LINKER={self.toolchain['ld']}")
                if "ar" in self.toolchain:
                    cmake_args.append(f"-DCMAKE_AR={self.toolchain['ar']}")
                if "ranlib" in self.toolchain:
                    cmake_args.append(f"-DCMAKE_RANLIB={self.toolchain['ranlib']}")

            if self.platform.os != "windows" and self._ffmpeg_enabled():
                cmake_args.append(f"-DFFMPEG_DIR={install_prefix}")

            full_cmd: list[str] = []
            if self.platform.os == "windows":
                full_cmd = ["cmd", "/c", str(configure)]
            else:
                full_cmd = [str(configure)]
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
                        # Fallback project produced by CMake if project name differs.
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
        elif repo.build_system == "openssl":
            if self.platform.os != "windows":
                raise RuntimeError("openssl build system is only supported on Windows")
            build_dir.mkdir(parents=True, exist_ok=True)

            zip_path = self._openssl_export_zip(env)
            if not zip_path.exists():
                raise RuntimeError(f"Missing openssl vcpkg export zip: {zip_path}")

            banner(f"{repo.name} ({build_type}) - stage")
            print(f"vcpkg export zip: {zip_path}", flush=True)

            import zipfile

            export_dir = build_dir / "_openssl_vcpkg_export"
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
                if p.is_dir() and p.name != "vcpkg" and (p / "include" / "openssl" / "ssl.h").exists()
            ]
            if not triplet_candidates:
                raise RuntimeError(
                    f"vcpkg export zip does not contain installed/<triplet>/include/openssl/ssl.h: {zip_path}"
                )

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

            def _pick_lib(dir_path: Path, stems: list[str]) -> Path | None:
                for stem in stems:
                    p = dir_path / stem
                    if p.exists():
                        return p
                return None

            ssl_release = _pick_lib(lib_src, ["libssl.lib", "ssl.lib"])
            crypto_release = _pick_lib(lib_src, ["libcrypto.lib", "crypto.lib"])
            ssl_debug = _pick_lib(debug_lib_src, ["libssl.lib", "ssl.lib"])
            crypto_debug = _pick_lib(debug_lib_src, ["libcrypto.lib", "crypto.lib"])

            required = [
                include_src / "openssl" / "ssl.h",
                ssl_release,
                crypto_release,
                ssl_debug,
                crypto_debug,
            ]
            missing = [p for p in required if not p or not p.exists()]
            if missing:
                wanted = "\n".join(f"  - {p}" for p in missing)
                raise RuntimeError(f"openssl vcpkg export is missing expected files:\n{wanted}")

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
                dest = inc_dst / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                elif item.is_file():
                    shutil.copy2(item, dest)

            for item in lib_src.iterdir():
                if item.is_file() and item.suffix.lower() == ".lib":
                    shutil.copy2(item, lib_dst / item.name)
            for item in debug_lib_src.iterdir():
                if item.is_file() and item.suffix.lower() == ".lib":
                    shutil.copy2(item, lib_dst / _add_debug_postfix(item.name))

            if bin_src.is_dir():
                if any(bin_src.glob("*.dll")):
                    print("[note] openssl export contains DLLs; prefer exporting a *-static triplet for a fully static prefix", flush=True)
                for item in bin_src.iterdir():
                    if item.is_file() and item.suffix.lower() in {".dll", ".pdb", ".exe"}:
                        shutil.copy2(item, bin_dst / item.name)

            cmake_dir = install_prefix / "lib" / "cmake" / "OpenSSL"
            cmake_dir.mkdir(parents=True, exist_ok=True)

            ssl_name = ssl_release.name if ssl_release else "libssl.lib"
            crypto_name = crypto_release.name if crypto_release else "libcrypto.lib"
            ssl_dbg_name = _add_debug_postfix(ssl_name)
            crypto_dbg_name = _add_debug_postfix(crypto_name)

            (cmake_dir / "OpenSSLConfig.cmake").write_text(
                "\n".join(
                    [
                        "# Generated by oiio-builder (imported from vcpkg export)",
                        "set(OpenSSL_FOUND TRUE)",
                        "",
                        "get_filename_component(_openssl_prefix \"${CMAKE_CURRENT_LIST_DIR}/../../..\" ABSOLUTE)",
                        "set(OPENSSL_ROOT_DIR \"${_openssl_prefix}\")",
                        "set(OPENSSL_INCLUDE_DIR \"${_openssl_prefix}/include\")",
                        "set(OPENSSL_INCLUDE_DIRS \"${OPENSSL_INCLUDE_DIR}\")",
                        "set(_openssl_libdir \"${_openssl_prefix}/lib\")",
                        f"set(_openssl_debug_postfix \"{debug_postfix}\")",
                        "",
                        f"set(OPENSSL_SSL_LIBRARY \"${{_openssl_libdir}}/{ssl_name}\")",
                        f"set(OPENSSL_CRYPTO_LIBRARY \"${{_openssl_libdir}}/{crypto_name}\")",
                        "set(OPENSSL_LIBRARIES \"${OPENSSL_SSL_LIBRARY};${OPENSSL_CRYPTO_LIBRARY}\")",
                        "",
                        "if(NOT TARGET OpenSSL::Crypto)",
                        "  add_library(OpenSSL::Crypto UNKNOWN IMPORTED)",
                        "  set_property(TARGET OpenSSL::Crypto PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG\")",
                        "  set_target_properties(OpenSSL::Crypto PROPERTIES",
                        "    INTERFACE_INCLUDE_DIRECTORIES \"${OPENSSL_INCLUDE_DIR}\"",
                        f"    IMPORTED_LOCATION_RELEASE \"${{_openssl_libdir}}/{crypto_name}\"",
                        f"    IMPORTED_LOCATION_DEBUG \"${{_openssl_libdir}}/{crypto_dbg_name}\"",
                        "    MAP_IMPORTED_CONFIG_MINSIZEREL Release",
                        "    MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
                        "    MAP_IMPORTED_CONFIG_ASAN Release",
                        "  )",
                        "endif()",
                        "",
                        "if(NOT TARGET OpenSSL::SSL)",
                        "  add_library(OpenSSL::SSL UNKNOWN IMPORTED)",
                        "  set_property(TARGET OpenSSL::SSL PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG\")",
                        "  set_target_properties(OpenSSL::SSL PROPERTIES",
                        "    INTERFACE_INCLUDE_DIRECTORIES \"${OPENSSL_INCLUDE_DIR}\"",
                        f"    IMPORTED_LOCATION_RELEASE \"${{_openssl_libdir}}/{ssl_name}\"",
                        f"    IMPORTED_LOCATION_DEBUG \"${{_openssl_libdir}}/{ssl_dbg_name}\"",
                        "    INTERFACE_LINK_LIBRARIES OpenSSL::Crypto",
                        "    MAP_IMPORTED_CONFIG_MINSIZEREL Release",
                        "    MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
                        "    MAP_IMPORTED_CONFIG_ASAN Release",
                        "  )",
                        "endif()",
                        "",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        elif repo.build_system == "sqlite":
            if self.platform.os != "windows":
                raise RuntimeError("sqlite build system is only supported on Windows")
            build_dir.mkdir(parents=True, exist_ok=True)

            zip_path = self._sqlite_export_zip(env)
            if not zip_path.exists():
                raise RuntimeError(f"Missing sqlite vcpkg export zip: {zip_path}")

            banner(f"{repo.name} ({build_type}) - stage")
            print(f"vcpkg export zip: {zip_path}", flush=True)

            import zipfile

            export_dir = build_dir / "_sqlite_vcpkg_export"
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
                if p.is_dir() and p.name != "vcpkg" and (p / "include" / "sqlite3.h").exists()
            ]
            if not triplet_candidates:
                raise RuntimeError(f"vcpkg export zip does not contain installed/<triplet>/include/sqlite3.h: {zip_path}")

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

            sqlite_release = lib_src / "sqlite3.lib"
            sqlite_debug = debug_lib_src / "sqlite3.lib"
            required = [include_src / "sqlite3.h", sqlite_release, sqlite_debug]
            missing = [p for p in required if not p.exists()]
            if missing:
                wanted = "\n".join(f"  - {p}" for p in missing)
                raise RuntimeError(f"sqlite vcpkg export is missing expected files:\n{wanted}")

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
                if item.is_file() and item.name.lower().startswith("sqlite3"):
                    shutil.copy2(item, inc_dst / item.name)

            shutil.copy2(sqlite_release, lib_dst / sqlite_release.name)
            shutil.copy2(sqlite_debug, lib_dst / _add_debug_postfix(sqlite_release.name))

            if bin_src.is_dir():
                if any(bin_src.glob("*.dll")):
                    print("[note] sqlite export contains DLLs; prefer exporting a *-static triplet for a fully static prefix", flush=True)
                for item in bin_src.iterdir():
                    if item.is_file() and item.suffix.lower() in {".dll", ".pdb", ".exe"}:
                        shutil.copy2(item, bin_dst / item.name)
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
                            "if(EXISTS \"${GIFLIB_SRC_DIR}/gif_win32_compat.h\")",
                            "  install(FILES \"${GIFLIB_SRC_DIR}/gif_win32_compat.h\" DESTINATION include)",
                            "endif()",
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
                run(cmd, env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "configure")))

                build_cmd = ["cmake", "--build", str(build_dir), "--config", build_type, "--parallel", str(self._jobs())]
                print_cmd("build command", build_cmd)
                banner(f"{repo.name} ({build_type}) - building")
                run(build_cmd, env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "build")))

                install_cmd = ["cmake", "--install", str(build_dir), "--config", build_type]
                print_cmd("install command", install_cmd)
                banner(f"{repo.name} ({build_type}) - install")
                run(install_cmd, env=env, dry_run=self.dry_run, log_path=str(self._repo_log_path(repo.name, build_type, "install")))
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
                    run(
                        ["make", "clean"],
                        cwd=str(src_dir),
                        env=make_env,
                        dry_run=self.dry_run,
                        log_path=str(self._repo_log_path(repo.name, build_type, "clean")),
                    )
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
                    log_path=str(self._repo_log_path(repo.name, build_type, "build")),
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
                        dry_run=self.dry_run,
                        log_path=str(self._repo_log_path(repo.name, build_type, "install-bin")),
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
                    run(
                        ["install", "-m", "644", "gif_lib.h", str(install_prefix / "include" / "gif_lib.h")],
                        cwd=str(src_dir),
                        dry_run=self.dry_run,
                        log_path=str(self._repo_log_path(repo.name, build_type, "install-gif_lib_h")),
                    )
                    if (src_dir / "gif_win32_compat.h").exists():
                        print_cmd(
                            "install command",
                            [
                                "install",
                                "-m",
                                "644",
                                "gif_win32_compat.h",
                                str(install_prefix / "include" / "gif_win32_compat.h"),
                            ],
                        )
                        run(
                            ["install", "-m", "644", "gif_win32_compat.h", str(install_prefix / "include" / "gif_win32_compat.h")],
                            cwd=str(src_dir),
                            dry_run=self.dry_run,
                            log_path=str(self._repo_log_path(repo.name, build_type, "install-gif_win32_compat_h")),
                        )
                    print_cmd("install command", ["install", "-m", "644", "libgif.a", str(install_prefix / "lib" / "libgif.a")])
                    run(
                        ["install", "-m", "644", "libgif.a", str(install_prefix / "lib" / "libgif.a")],
                        cwd=str(src_dir),
                        dry_run=self.dry_run,
                        log_path=str(self._repo_log_path(repo.name, build_type, "install-libgif_a")),
                    )
                    print_cmd("install command", ["install", "-m", "644", "libutil.a", str(install_prefix / "lib" / "libutil.a")])
                    run(
                        ["install", "-m", "644", "libutil.a", str(install_prefix / "lib" / "libutil.a")],
                        cwd=str(src_dir),
                        dry_run=self.dry_run,
                        log_path=str(self._repo_log_path(repo.name, build_type, "install-libutil_a")),
                    )
        else:
            raise RuntimeError(f"Unsupported build_system: {repo.build_system}")

        self._post_install_repo(repo, install_prefix, build_type)

        if not self.dry_run:
            build_stamp = self._write_stamp(repo, ctx, deps_heads, cflags, cxxflags)
            self._write_install_marker(repo, ctx, build_stamp)

        return ("rebuilt" if had_stamp else "built"), ""

    def _jobs(self) -> int:
        cfg = self.config.global_cfg
        jobs = cfg.jobs if cfg.jobs > 0 else os.cpu_count() or 4
        if self.parallel_build_types and self._parallel_build_type_count > 1:
            return max(1, jobs // self._parallel_build_type_count)
        return jobs

    def _resolved_repo_config_for_build(self, repo: RepoConfig, src_dir: Path) -> RepoConfig:
        # Decide build system for xz/lcms2 based on config and source layout.
        if self.platform.os == "windows":
            if repo.name == "sqlite":
                return replace(repo, build_system="sqlite")
        if repo.name == "xz":
            cmake_lists = src_dir / "CMakeLists.txt"
            build_system = "autotools" if (self.config.global_cfg.xz_use_autotools or not cmake_lists.exists()) else "cmake"
            return replace(repo, build_system=build_system)
        if repo.name == "lcms2":
            cmake_lists = src_dir / "CMakeLists.txt"
            build_system = "autotools" if (self.config.global_cfg.lcms2_use_autotools or not cmake_lists.exists()) else "cmake"
            return replace(repo, build_system=build_system)
        return repo

    def _repo_requires_exclusive_build(self, repo: RepoConfig) -> bool:
        if self.platform.os == "windows":
            return False
        return repo.build_system in {"giflib", "qt6"}

    def _run_build_type(
        self,
        build_type: str,
        order: list[str],
        repos_by_name: dict[str, RepoConfig],
        report: BuildReport,
        cancel_event: threading.Event,
    ) -> None:
        for idx, repo_name in enumerate(order):
            if cancel_event.is_set():
                for remaining in order[idx:]:
                    report.record(build_type, remaining, "canceled", "canceled")
                return
            repo = repos_by_name[repo_name]
            src_dir = self.repo_paths.get(repo.name, self._resolve_repo_dir(repo))
            if self._maybe_skip_missing(repo, src_dir):
                report.record(build_type, repo.name, "missing", "not-found")
                continue

            deps_heads: dict[str, str | None] = {}
            for dep in repo.deps:
                if dep not in repos_by_name:
                    continue
                if dep not in self.repo_paths:
                    continue
                deps_heads[dep] = self._dep_fingerprint(dep, build_type)

            repo_for_build = self._resolved_repo_config_for_build(repo, src_dir)

            try:
                if self.parallel_build_types and self._repo_requires_exclusive_build(repo_for_build):
                    lock = self._repo_exclusive_build_locks[repo.name]
                    with lock:
                        status, detail = self._build_repo(repo_for_build, build_type, deps_heads)
                else:
                    status, detail = self._build_repo(repo_for_build, build_type, deps_heads)
                report.record(build_type, repo.name, status, detail)
            except Exception as exc:
                message = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                report.record(build_type, repo.name, "failed", message)
                cancel_event.set()
                for remaining in order[idx + 1 :]:
                    report.record(build_type, remaining, "canceled", "canceled")
                raise

    def _run_parallel_build_types(
        self,
        build_types: list[str],
        order: list[str],
        repos_by_name: dict[str, RepoConfig],
        report: BuildReport,
    ) -> None:
        # Parallel build types require distinct install prefixes to avoid file races.
        by_prefix: dict[str, list[str]] = {}
        for build_type in build_types:
            prefix = self.prefixes.get(build_type)
            if not prefix:
                continue
            normalized = os.path.normcase(os.path.normpath(str(prefix)))
            by_prefix.setdefault(normalized, []).append(build_type)
        conflicts = [(types, self.prefixes[types[0]]) for types in by_prefix.values() if len(types) > 1]
        if conflicts:
            lines = ["--parallel-build-types requires unique install prefixes per build type."]
            for types, prefix in conflicts:
                lines.append(f"  {', '.join(types)} -> {prefix}")
            lines.append("Use prefix_layout='by-build-type' or set distinct debug_suffix/asan_suffix.")
            raise SystemExit("\n".join(lines))

        cancel_event = threading.Event()
        self._parallel_build_type_count = max(1, len(build_types))
        try:
            with ThreadPoolExecutor(max_workers=len(build_types)) as executor:
                futures = {
                    executor.submit(self._run_build_type, build_type, order, repos_by_name, report, cancel_event): build_type
                    for build_type in build_types
                }
                first_exc: BaseException | None = None
                for future in as_completed(futures):
                    try:
                        future.result()
                    except BaseException as exc:
                        if first_exc is None:
                            first_exc = exc
                            cancel_event.set()
                report.print()
                if first_exc is not None:
                    raise first_exc
        finally:
            self._parallel_build_type_count = 1

    def _sync_repos(self, order: list[str], repos_by_name: dict[str, RepoConfig]) -> None:
        """Resolve repo paths and perform clone/fetch/checkout/update."""
        for repo_name in order:
            repo = repos_by_name[repo_name]
            repo_dir = self._resolve_repo_dir(repo)
            self.repo_paths[repo.name] = repo_dir
            if self._maybe_skip_missing(repo, repo_dir):
                continue
            if repo.name == "libiconv" and self.platform.os == "windows":
                continue
            if repo.name == "openssl" and self.platform.os == "windows":
                continue
            if repo.name == "sqlite" and self.platform.os == "windows":
                continue
            ensure_repo(repo_dir, repo.url, repo.ref, repo.ref_type, update=not self.no_update, dry_run=self.dry_run)

    def update_only(self) -> int:
        deps_map = {repo.name: repo.deps for repo in self.repos}
        order = topo_sort(
            [r.name for r in self.repos],
            deps_map,
            preferred_order=self.config.global_cfg.preferred_repo_order,
        )
        repos_by_name = {repo.name: repo for repo in self.repos}
        self._sync_repos(order, repos_by_name)
        print("Repo update-only completed.")
        return 0

    def prepare_only(self) -> int:
        deps_map = {repo.name: repo.deps for repo in self.repos}
        order = topo_sort(
            [r.name for r in self.repos],
            deps_map,
            preferred_order=self.config.global_cfg.preferred_repo_order,
        )
        repos_by_name = {repo.name: repo for repo in self.repos}
        self._sync_repos(order, repos_by_name)

        for repo_name in order:
            repo = repos_by_name[repo_name]
            src_dir = self.repo_paths.get(repo.name, self._resolve_repo_dir(repo))
            if repo.source_subdir:
                src_dir = src_dir / repo.source_subdir
            if not src_dir.exists():
                continue
            self._prepare_repo_source(repo, src_dir)

        print("Repo prepare-only completed.")
        return 0

    def run(self) -> int:
        deps_map = {repo.name: repo.deps for repo in self.repos}
        order = topo_sort(
            [r.name for r in self.repos],
            deps_map,
            preferred_order=self.config.global_cfg.preferred_repo_order,
        )
        repos_by_name = {repo.name: repo for repo in self.repos}
        build_types = self._build_type_order()
        report = BuildReport(build_types, order, self.prefixes)

        # Resolve paths and clone/update repos.
        self._sync_repos(order, repos_by_name)

        if self.parallel_build_types and self.platform.os in {"macos", "linux"} and len(build_types) > 1:
            self._run_parallel_build_types(build_types, order, repos_by_name, report)
            return 0

        cancel_event = threading.Event()
        self._parallel_build_type_count = 1
        try:
            for build_type in build_types:
                self._run_build_type(build_type, order, repos_by_name, report, cancel_event)
        except Exception:
            report.print()
            raise
        report.print()
        return 0
