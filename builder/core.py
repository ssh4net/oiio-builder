from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading

from .config import Config, RepoConfig
from . import license_policy
from . import backends as build_backends
from .git_ops import ensure_repo, git_head
from .platform import PlatformInfo
from .recipes import registry as recipe_registry
from .repo_options import CMakeOptions, load_repo_defaults, load_user_overrides, render_cmake_options
from .runner import banner, print_cmd, run, set_output_lock
from .stamps import compute_stamp, read_stamp, write_stamp
from .tooling import (
    normalize_override as _normalize_override,
    resolve_executable_candidate as _resolve_executable_candidate,
    resolve_nasm_executable,
)
from .topo import topo_sort


@dataclass
class BuildContext:
    repo: RepoConfig
    build_type: str
    build_dir: Path
    install_prefix: Path
    src_dir: Path


@dataclass
class PrefixContractCheck:
    prefix: Path
    build_types: list[str]
    state: str
    hard_mismatches: list[str]
    soft_mismatches: list[str]
    files: dict[str, Path]


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
        apply_prefix_contract: bool = False,
    ) -> None:
        self.config = config
        self.platform = platform
        self.license_profile = license_policy.resolve_profile(self.config.global_cfg.profile)
        self._license_profile_exclusions: dict[str, str] = {}
        self.config.global_cfg.build_root = self._host_build_root(self.config.global_cfg.build_root)
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
        self._windows_msvc_env_cache: dict[str, str] | None = None
        self._windows_msvc_env_loaded = False

        self.parallel_build_types = parallel_build_types
        self.apply_prefix_contract = apply_prefix_contract
        if self.parallel_build_types and self.platform.os == "windows":
            raise SystemExit("--parallel-build-types is supported only on macOS/Linux.")
        self._parallel_build_type_count = 1
        self._output_lock = threading.Lock() if self.parallel_build_types else None
        set_output_lock(self._output_lock)
        self._repo_source_prepared: set[str] = set()
        self._repo_source_prepare_locks: dict[str, threading.Lock] = {repo.name: threading.Lock() for repo in self.repos}
        self._repo_exclusive_build_locks: dict[str, threading.Lock] = {repo.name: threading.Lock() for repo in self.repos}

    def _apply_platform_dependency_policy(self, repos: list[RepoConfig]) -> None:
        """Adjust dependency edges that are platform-specific in recipes."""
        for repo in repos:
            if repo.name != "libtiff":
                continue
            if self.platform.os == "windows":
                if "freeglut" not in repo.deps:
                    repo.deps.append("freeglut")
            else:
                repo.deps = [dep for dep in repo.deps if dep != "freeglut"]

    def _host_build_root(self, base_root: Path) -> Path:
        host_dir = self.platform.os
        if base_root.name.strip().lower() == host_dir:
            return base_root
        return base_root / host_dir

    def _cmake_path_arg(self, value: str | Path) -> str:
        text = str(value)
        if self.platform.os == "windows":
            return text.replace("\\", "/")
        return text

    def _windows_path_to_msys(self, value: str | Path) -> str:
        text = str(value).strip().strip("\"'")
        if not text:
            return text
        if re.match(r"^[A-Za-z]:[\\/]", text):
            drive = text[0].lower()
            rest = text[2:].replace("\\", "/")
            if not rest.startswith("/"):
                rest = f"/{rest}"
            return f"/{drive}{rest}"
        return text.replace("\\", "/")

    def _windows_split_env_path_list(self, value: str) -> list[str]:
        text = value.strip()
        if not text:
            return []
        if ";" in text:
            return [part.strip() for part in text.split(";") if part.strip()]
        if re.match(r"^[A-Za-z]:[\\/]", text) and text.count(":") == 1:
            return [text]
        return [part.strip() for part in text.split(":") if part.strip()]

    def _adapt_windows_env_for_msys(self, env: dict[str, str]) -> None:
        if self.platform.os != "windows" or not self._windows_msys2_detected():
            return

        env["MSYS2_PATH_TYPE"] = "inherit"

        def _merge_path_lists(*raw_values: str | None) -> str:
            merged: list[str] = []
            seen: set[str] = set()
            for raw in raw_values:
                if not raw:
                    continue
                for part in self._windows_split_env_path_list(raw):
                    converted = self._windows_path_to_msys(part)
                    key = converted.rstrip("/").lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    merged.append(converted)
            return ":".join(merged)

        merged_path = _merge_path_lists(os.environ.get("PATH"), env.get("PATH"))
        if merged_path:
            env["PATH"] = merged_path

        merged_pkg = _merge_path_lists(os.environ.get("PKG_CONFIG_PATH"), env.get("PKG_CONFIG_PATH"))
        if merged_pkg:
            env["PKG_CONFIG_PATH"] = merged_pkg

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

    def _prefix_contract_enabled(self) -> bool:
        return bool(self.config.global_cfg.write_prefix_contract or self.apply_prefix_contract)

    def _unique_prefix_items(self) -> list[tuple[Path, list[str]]]:
        ordered_types = [build_type for build_type in self._build_type_order() if build_type in self.prefixes]
        grouped: dict[str, tuple[Path, list[str]]] = {}
        for build_type in ordered_types:
            prefix = self.prefixes[build_type]
            key = os.path.normcase(os.path.normpath(str(prefix)))
            existing = grouped.get(key)
            if existing is None:
                grouped[key] = (prefix, [build_type])
            else:
                existing[1].append(build_type)
        return list(grouped.values())

    def _prefix_contract_dir(self, install_prefix: Path) -> Path:
        return install_prefix / ".oiio-builder"

    def _prefix_contract_file_paths(self, install_prefix: Path) -> dict[str, Path]:
        root = self._prefix_contract_dir(install_prefix)
        return {
            "json": root / "prefix-contract.json",
            "cmake": root / "prefix-contract.cmake",
            "init_cache": root / "prefix-init-cache.cmake",
            "presets": root / "prefix-presets.json",
            "license_policy": root / "license-policy.json",
        }

    def _prefix_contract_token(self, install_prefix: Path) -> str:
        normalized = os.path.normcase(os.path.normpath(str(install_prefix)))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]

    def _prefix_contract_cxx_stdlib(self) -> str:
        if self.platform.os == "windows":
            return "msvc"
        return "libc++" if self.config.global_cfg.use_libcxx else "libstdc++"

    def _prefix_contract_glibcxx_cxx11_abi(self) -> int | None:
        if self.platform.os == "windows" or self.config.global_cfg.use_libcxx:
            return None

        candidates = [
            self.config.global_cfg.env.get("_GLIBCXX_USE_CXX11_ABI"),
            self.config.global_cfg.env.get("GLIBCXX_USE_CXX11_ABI"),
            os.environ.get("_GLIBCXX_USE_CXX11_ABI"),
            os.environ.get("GLIBCXX_USE_CXX11_ABI"),
        ]
        for key in ("CFLAGS", "CXXFLAGS"):
            raw = self.config.global_cfg.env.get(key)
            if raw:
                match = re.search(r"-D_GLIBCXX_USE_CXX11_ABI=(0|1)", raw)
                if match:
                    return int(match.group(1))
        for raw in candidates:
            if raw is None:
                continue
            text = str(raw).strip()
            if text in {"0", "1"}:
                return int(text)
        return None

    def _prefix_contract_runtime(self) -> str | None:
        if self.platform.os != "windows":
            return None
        runtime_mode = self._windows_runtime_mode()
        if runtime_mode == "static":
            return "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        if runtime_mode == "dynamic":
            return "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL"
        raw = self.config.global_cfg.windows.get("msvc_runtime")
        if raw is None:
            return None
        return str(raw).strip() or None

    def _prefix_contract_sanitizers(self, build_types: list[str]) -> list[str]:
        if build_types == ["ASAN"]:
            return ["address"]
        return []

    def _prefix_contract_payload(self, install_prefix: Path, build_types: list[str]) -> dict[str, object]:
        cfg = self.config.global_cfg
        profile_name = self.license_profile.name if self.license_profile is not None else None
        profile_linkage = self.license_profile.linkage if self.license_profile is not None else None
        consumer_definitions = (
            list(self.license_profile.consumer_compile_definitions) if self.license_profile is not None else []
        )
        return {
            "schema": 2,
            "install_prefix": str(install_prefix),
            "build_types": list(build_types),
            "platform": {
                "os": self.platform.os,
                "arch": self.platform.arch,
            },
            "abi": {
                "cxx_stdlib": self._prefix_contract_cxx_stdlib(),
                "glibcxx_cxx11_abi": self._prefix_contract_glibcxx_cxx11_abi(),
                "build_shared_libs_default": not cfg.static_default,
                "position_independent_code": bool(cfg.pic),
                "msvc_runtime": self._prefix_contract_runtime(),
                "sanitizers": self._prefix_contract_sanitizers(build_types),
            },
            "policy": {
                "cxx_standard": int(cfg.cxx_standard),
                "cxx_extensions": bool(cfg.cxx_extensions),
                "pkg_config_use_static_libs": True,
                "use_lld": bool(cfg.use_lld),
            },
            "license": {
                "profile": profile_name,
                "linkage": profile_linkage,
                "consumer_compile_definitions": consumer_definitions,
            },
            "toolchain": {
                "fingerprint": self._toolchain_fingerprint(),
            },
        }

    def _prefix_contract_cache_variables(self, install_prefix: Path) -> dict[str, object]:
        cfg = self.config.global_cfg
        cache: dict[str, object] = {
            "BUILD_SHARED_LIBS": not cfg.static_default,
            "CMAKE_POSITION_INDEPENDENT_CODE": bool(cfg.pic),
            "CMAKE_CXX_STANDARD": int(cfg.cxx_standard),
            "CMAKE_CXX_EXTENSIONS": bool(cfg.cxx_extensions),
            "CMAKE_PREFIX_PATH": self._cmake_path_arg(install_prefix),
            "PKG_CONFIG_USE_STATIC_LIBS": True,
        }
        if self.platform.os == "windows":
            cache["CMAKE_POLICY_DEFAULT_CMP0091"] = "NEW"
            runtime = self._prefix_contract_runtime()
            if runtime:
                cache["CMAKE_MSVC_RUNTIME_LIBRARY"] = runtime
        return cache

    def _cmake_bool(self, value: bool) -> str:
        return "ON" if value else "OFF"

    def _cmake_quote(self, value: object) -> str:
        text = str(value)
        text = text.replace("\\", "\\\\").replace("\"", "\\\"")
        return f"\"{text}\""

    def _prefix_init_cache_text(self, install_prefix: Path) -> str:
        cache = self._prefix_contract_cache_variables(install_prefix)
        lines = [
            "# Generated by oiio-builder.",
            "# Safe shared cache defaults for consumers of this prefix.",
            f"# Prefix: {install_prefix}",
            "",
        ]
        bool_keys = {"BUILD_SHARED_LIBS", "CMAKE_POSITION_INDEPENDENT_CODE", "CMAKE_CXX_EXTENSIONS", "PKG_CONFIG_USE_STATIC_LIBS"}
        for key in sorted(cache.keys()):
            value = cache[key]
            if key in bool_keys:
                lines.append(f"set({key} {self._cmake_bool(bool(value))} CACHE BOOL \"Managed by oiio-builder\" FORCE)")
            elif isinstance(value, int):
                lines.append(f"set({key} {value} CACHE STRING \"Managed by oiio-builder\" FORCE)")
            else:
                lines.append(
                    f"set({key} {self._cmake_quote(value)} CACHE STRING \"Managed by oiio-builder\" FORCE)"
                )
        lines.append("")
        return "\n".join(lines)

    def _prefix_contract_cmake_text(self, install_prefix: Path, build_types: list[str]) -> str:
        payload = self._prefix_contract_payload(install_prefix, build_types)
        abi = payload["abi"]
        policy = payload["policy"]
        license_info = payload["license"]
        assert isinstance(abi, dict)
        assert isinstance(policy, dict)
        assert isinstance(license_info, dict)
        prefix_var = self._cmake_quote(self._cmake_path_arg(install_prefix))
        profile_name = license_info["profile"] or ""
        consumer_definitions = license_info["consumer_compile_definitions"]
        assert isinstance(consumer_definitions, list)
        lines = [
            "# Generated by oiio-builder.",
            "# Helper variables/functions for the prefix contract described in prefix-contract.json.",
            "",
            f"set(OIIO_BUILDER_PREFIX_CONTRACT_SCHEMA {payload['schema']})",
            f"set(OIIO_BUILDER_PREFIX_PATH {prefix_var})",
            f"set(OIIO_BUILDER_PREFIX_LICENSE_PROFILE {self._cmake_quote(profile_name)})",
            "set(OIIO_BUILDER_PREFIX_CONSUMER_COMPILE_DEFINITIONS "
            f"{self._cmake_quote(';'.join(str(item) for item in consumer_definitions))})",
            f"set(OIIO_BUILDER_PREFIX_CXX_STDLIB {self._cmake_quote(abi['cxx_stdlib'])})",
            f"set(OIIO_BUILDER_PREFIX_BUILD_SHARED_LIBS_DEFAULT {self._cmake_bool(bool(abi['build_shared_libs_default']))})",
            f"set(OIIO_BUILDER_PREFIX_POSITION_INDEPENDENT_CODE {self._cmake_bool(bool(abi['position_independent_code']))})",
            f"set(OIIO_BUILDER_PREFIX_CXX_STANDARD {policy['cxx_standard']})",
            f"set(OIIO_BUILDER_PREFIX_CXX_EXTENSIONS {self._cmake_bool(bool(policy['cxx_extensions']))})",
            f"set(OIIO_BUILDER_PREFIX_PKG_CONFIG_USE_STATIC_LIBS {self._cmake_bool(bool(policy['pkg_config_use_static_libs']))})",
        ]
        runtime = abi.get("msvc_runtime")
        if runtime:
            lines.append(f"set(OIIO_BUILDER_PREFIX_MSVC_RUNTIME_LIBRARY {self._cmake_quote(runtime)})")
        glibcxx = abi.get("glibcxx_cxx11_abi")
        if glibcxx is None:
            lines.append("set(OIIO_BUILDER_PREFIX_GLIBCXX_USE_CXX11_ABI \"\")")
        else:
            lines.append(f"set(OIIO_BUILDER_PREFIX_GLIBCXX_USE_CXX11_ABI {glibcxx})")
        lines.extend(
            [
                "",
                "function(oiio_builder_apply_prefix_contract)",
                "  set(BUILD_SHARED_LIBS ${OIIO_BUILDER_PREFIX_BUILD_SHARED_LIBS_DEFAULT} CACHE BOOL \"Managed by oiio-builder\" FORCE)",
                "  set(CMAKE_POSITION_INDEPENDENT_CODE ${OIIO_BUILDER_PREFIX_POSITION_INDEPENDENT_CODE} CACHE BOOL \"Managed by oiio-builder\" FORCE)",
                "  set(CMAKE_CXX_STANDARD ${OIIO_BUILDER_PREFIX_CXX_STANDARD} CACHE STRING \"Managed by oiio-builder\" FORCE)",
                "  set(CMAKE_CXX_EXTENSIONS ${OIIO_BUILDER_PREFIX_CXX_EXTENSIONS} CACHE BOOL \"Managed by oiio-builder\" FORCE)",
                "  set(PKG_CONFIG_USE_STATIC_LIBS ${OIIO_BUILDER_PREFIX_PKG_CONFIG_USE_STATIC_LIBS} CACHE BOOL \"Managed by oiio-builder\" FORCE)",
                "  if(NOT OIIO_BUILDER_PREFIX_CONSUMER_COMPILE_DEFINITIONS STREQUAL \"\")",
                "    add_compile_definitions(${OIIO_BUILDER_PREFIX_CONSUMER_COMPILE_DEFINITIONS})",
                "  endif()",
                "  if(DEFINED OIIO_BUILDER_PREFIX_MSVC_RUNTIME_LIBRARY AND NOT OIIO_BUILDER_PREFIX_MSVC_RUNTIME_LIBRARY STREQUAL \"\")",
                "    set(CMAKE_POLICY_DEFAULT_CMP0091 NEW CACHE STRING \"Managed by oiio-builder\" FORCE)",
                "    set(CMAKE_MSVC_RUNTIME_LIBRARY ${OIIO_BUILDER_PREFIX_MSVC_RUNTIME_LIBRARY} CACHE STRING \"Managed by oiio-builder\" FORCE)",
                "  endif()",
                "  if(DEFINED CMAKE_PREFIX_PATH AND NOT CMAKE_PREFIX_PATH STREQUAL \"\")",
                "    set(_oiio_builder_prefix_path_list ${CMAKE_PREFIX_PATH})",
                "    list(PREPEND _oiio_builder_prefix_path_list ${OIIO_BUILDER_PREFIX_PATH})",
                "    list(REMOVE_DUPLICATES _oiio_builder_prefix_path_list)",
                "    set(CMAKE_PREFIX_PATH ${_oiio_builder_prefix_path_list} CACHE STRING \"Managed by oiio-builder\" FORCE)",
                "  else()",
                "    set(CMAKE_PREFIX_PATH ${OIIO_BUILDER_PREFIX_PATH} CACHE STRING \"Managed by oiio-builder\" FORCE)",
                "  endif()",
                "endfunction()",
                "",
            ]
        )
        return "\n".join(lines)

    def _prefix_contract_presets_text(self, install_prefix: Path) -> str:
        token = self._prefix_contract_token(install_prefix)
        contract_name = f"oiio-builder-contract-{token}"
        prefix_name = f"oiio-builder-prefix-{token}"
        cache = self._prefix_contract_cache_variables(install_prefix)
        prefix_path = cache.pop("CMAKE_PREFIX_PATH")
        payload = {
            "version": 4,
            "vendor": {
                "oiio-builder": {
                    "managed": True,
                    "schema": 1,
                    "installPrefix": str(install_prefix),
                    "token": token,
                }
            },
            "configurePresets": [
                {
                    "name": contract_name,
                    "hidden": True,
                    "cacheVariables": cache,
                },
                {
                    "name": prefix_name,
                    "hidden": True,
                    "inherits": [contract_name],
                    "cacheVariables": {
                        "CMAKE_PREFIX_PATH": prefix_path,
                    },
                },
            ],
        }
        return json.dumps(payload, indent=2) + "\n"

    def _write_managed_text_file(self, path: Path, text: str, *, label: str) -> None:
        if self.dry_run:
            print(f"[dry-run] write {label}: {path}", flush=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = None
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                existing = None
        if existing == text:
            return
        path.write_text(text, encoding="utf-8")
        print(f"[write] {label}: {path}", flush=True)

    def _ensure_prefix_contracts(self) -> None:
        if not self._prefix_contract_enabled():
            return
        for install_prefix, build_types in self._unique_prefix_items():
            paths = self._prefix_contract_file_paths(install_prefix)
            payload = self._prefix_contract_payload(install_prefix, build_types)
            self._write_managed_text_file(
                paths["json"],
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                label="prefix contract",
            )
            self._write_managed_text_file(
                paths["init_cache"],
                self._prefix_init_cache_text(install_prefix),
                label="prefix init cache",
            )
            self._write_managed_text_file(
                paths["cmake"],
                self._prefix_contract_cmake_text(install_prefix, build_types),
                label="prefix contract cmake",
            )
            self._write_managed_text_file(
                paths["presets"],
                self._prefix_contract_presets_text(install_prefix),
                label="prefix presets",
            )
            self._write_license_policy_manifest(paths["license_policy"])

    def _write_license_policy_manifest(self, path: Path) -> None:
        if self.license_profile is None:
            return
        payload = license_policy.profile_manifest(
            self.license_profile,
            (repo.name for repo in self.repos),
            self._license_profile_exclusions,
        )
        self._write_managed_text_file(
            path,
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            label="license policy",
        )

    def _prefix_has_non_metadata_content(self, install_prefix: Path) -> bool:
        if not install_prefix.exists() or not install_prefix.is_dir():
            return False
        try:
            for child in install_prefix.iterdir():
                if child.name == ".oiio-builder":
                    continue
                return True
        except OSError:
            return False
        return False

    def _contract_value(self, data: dict[str, object], path: tuple[str, ...]) -> object:
        current: object = data
        for key in path:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return current

    def check_prefix_contract(self, install_prefix: Path, build_types: list[str]) -> PrefixContractCheck:
        files = self._prefix_contract_file_paths(install_prefix)
        json_path = files["json"]
        if not json_path.exists():
            state = "missing-populated" if self._prefix_has_non_metadata_content(install_prefix) else "missing-empty"
            return PrefixContractCheck(install_prefix, build_types, state, [], [], files)

        try:
            current = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            return PrefixContractCheck(install_prefix, build_types, "invalid", ["invalid JSON"], [], files)
        if not isinstance(current, dict):
            return PrefixContractCheck(install_prefix, build_types, "invalid", ["contract is not a JSON object"], [], files)

        expected = self._prefix_contract_payload(install_prefix, build_types)
        hard_fields = [
            (("schema",), "schema"),
            (("platform", "os"), "platform.os"),
            (("platform", "arch"), "platform.arch"),
            (("abi", "cxx_stdlib"), "abi.cxx_stdlib"),
            (("abi", "glibcxx_cxx11_abi"), "abi.glibcxx_cxx11_abi"),
            (("abi", "build_shared_libs_default"), "abi.build_shared_libs_default"),
            (("abi", "position_independent_code"), "abi.position_independent_code"),
            (("abi", "msvc_runtime"), "abi.msvc_runtime"),
            (("abi", "sanitizers"), "abi.sanitizers"),
            (("license", "profile"), "license.profile"),
            (("license", "linkage"), "license.linkage"),
            (("license", "consumer_compile_definitions"), "license.consumer_compile_definitions"),
        ]
        soft_fields = [
            (("policy", "cxx_standard"), "policy.cxx_standard"),
            (("policy", "cxx_extensions"), "policy.cxx_extensions"),
            (("policy", "pkg_config_use_static_libs"), "policy.pkg_config_use_static_libs"),
            (("policy", "use_lld"), "policy.use_lld"),
        ]
        hard_mismatches: list[str] = []
        soft_mismatches: list[str] = []
        for path, label in hard_fields:
            lhs = self._contract_value(current, path)
            rhs = self._contract_value(expected, path)
            if lhs != rhs:
                hard_mismatches.append(f"{label}: expected {rhs!r}, found {lhs!r}")
        for path, label in soft_fields:
            lhs = self._contract_value(current, path)
            rhs = self._contract_value(expected, path)
            if lhs != rhs:
                soft_mismatches.append(f"{label}: expected {rhs!r}, found {lhs!r}")

        required_aux = ["cmake", "init_cache", "presets"]
        if self.license_profile is not None:
            required_aux.append("license_policy")
        missing_aux = [name for name in required_aux if not files[name].exists()]
        if missing_aux:
            hard_mismatches.append(f"missing generated files: {', '.join(missing_aux)}")

        state = "ok"
        if hard_mismatches:
            state = "mismatch"
        elif soft_mismatches:
            state = "soft-mismatch"
        return PrefixContractCheck(install_prefix, build_types, state, hard_mismatches, soft_mismatches, files)

    def _filter_repos(self) -> list[RepoConfig]:
        all_configured_repos = [r for r in self.config.repos if r.enabled]
        self._license_profile_exclusions = {
            repo.name: reason
            for repo in all_configured_repos
            if (reason := license_policy.rejected_reason(self.license_profile, repo.name)) is not None
        }
        configured_repos = [
            repo for repo in all_configured_repos if repo.name not in self._license_profile_exclusions
        ]
        self._apply_platform_dependency_policy(configured_repos)
        by_name_configured = {repo.name: repo for repo in all_configured_repos}
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
            rejected_explicit = [name for name in sorted(explicit) if name in self._license_profile_exclusions]
            if rejected_explicit:
                details = "\n".join(
                    f"  {name}: {self._license_profile_exclusions[name]}" for name in rejected_explicit
                )
                raise SystemExit(
                    f"Repo(s) requested by --only are rejected by profile {self.license_profile.name}:\n{details}"
                )
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
                    prefix / "bin" / f"python_{debug_postfix}.exe",
                    prefix / "bin" / f"python{debug_postfix}.exe",
                    prefix / "bin" / "python.exe",
                    prefix / f"python_{debug_postfix}.exe",
                    prefix / f"python{debug_postfix}.exe",
                    prefix / "python.exe",
                ]
            else:
                candidates = [
                    prefix / "bin" / "python.exe",
                    prefix / "bin" / f"python_{debug_postfix}.exe",
                    prefix / "bin" / f"python{debug_postfix}.exe",
                    prefix / "python.exe",
                    prefix / f"python_{debug_postfix}.exe",
                    prefix / f"python{debug_postfix}.exe",
                ]
        else:
            candidates = [prefix / "bin" / "python3", prefix / "bin" / "python"]
        expected_version = self._prefix_python_major_minor(prefix)
        for candidate in candidates:
            if not candidate.exists():
                continue
            if self.platform.os != "windows" or expected_version is None:
                return candidate
            if self._python_executable_major_minor(candidate) == expected_version:
                return candidate
        return None

    def _prefix_python_major_minor(self, prefix: Path) -> tuple[int, int] | None:
        patchlevel = prefix / "include" / "patchlevel.h"
        if not patchlevel.exists():
            return None
        text = patchlevel.read_text(encoding="utf-8", errors="replace")
        major = re.search(r"^\s*#\s*define\s+PY_MAJOR_VERSION\s+(\d+)\s*$", text, re.MULTILINE)
        minor = re.search(r"^\s*#\s*define\s+PY_MINOR_VERSION\s+(\d+)\s*$", text, re.MULTILINE)
        if major is None or minor is None:
            return None
        return int(major.group(1)), int(minor.group(1))

    def _prefix_python_lib_stem(self, prefix: Path) -> str | None:
        version = self._prefix_python_major_minor(prefix)
        if version is None:
            return None
        return f"python{version[0]}{version[1]}"

    def _python_executable_major_minor(self, executable: Path) -> tuple[int, int] | None:
        try:
            proc = subprocess.run(
                [str(executable), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        match = re.search(r"(\d+)\.(\d+)", proc.stdout)
        if match is None:
            return None
        return int(match.group(1)), int(match.group(2))

    def _host_python_executable_for_prefix(self, prefix: Path) -> str | None:
        expected_version = self._prefix_python_major_minor(prefix)
        if expected_version is None:
            return None

        candidates: list[Path] = []
        sys_executable = Path(sys.executable)
        if sys_executable.is_file():
            candidates.append(sys_executable)
        for env_name in ("VIRTUAL_ENV", "CONDA_PREFIX"):
            env_value = os.environ.get(env_name)
            if not env_value:
                continue
            env_root = Path(env_value)
            if self.platform.os == "windows":
                candidates.append(env_root / "Scripts" / "python.exe")
            else:
                candidates.append(env_root / "bin" / "python3")
                candidates.append(env_root / "bin" / "python")

        seen: set[str] = set()
        for candidate in candidates:
            key = os.path.normcase(os.path.normpath(str(candidate)))
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file() and self._python_executable_major_minor(candidate) == expected_version:
                return str(candidate)
        return None

    def _prefix_windows_python_libraries(self, prefix: Path) -> tuple[Path | None, Path | None]:
        debug_postfix = str(self.config.global_cfg.windows.get("debug_postfix", "d"))
        lib_dirs = [prefix / "libs", prefix / "lib"]
        release_candidates: list[Path] = []
        debug_candidates: list[Path] = []
        version_stem = self._prefix_python_lib_stem(prefix)

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

        if version_stem:
            debug_stems = {f"{version_stem}_{debug_postfix}", f"{version_stem}{debug_postfix}"}
            versioned_release = [path for path in release_candidates if path.stem.lower() == version_stem]
            versioned_debug = [path for path in debug_candidates if path.stem.lower() in debug_stems]
            if versioned_release or versioned_debug:
                release_candidates = versioned_release
                debug_candidates = versioned_debug

        def _priority(path: Path) -> tuple[int, str]:
            stem = path.stem.lower()
            # Prefer versioned libs (python313.lib / python313_d.lib) over
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
        return release_lib, debug_lib

    def _compute_prefixes(self) -> dict[str, Path]:
        cfg = self.config.global_cfg
        prefixes: dict[str, Path] = {}

        def _resolve_prefix(raw: str) -> Path:
            expanded = os.path.expanduser(os.path.expandvars(str(raw)))
            drive_match = re.match(r"^([A-Za-z]):[\\/](.*)$", expanded)
            if self.platform.os != "windows" and drive_match is not None:
                drive = drive_match.group(1).lower()
                rest = drive_match.group(2).replace("\\", "/")
                mount_root = Path("/mnt") / drive
                if mount_root.exists() or os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
                    return mount_root / rest
                raise RuntimeError(
                    f"Windows drive-style prefix {raw!r} is not valid on {self.platform.os}; "
                    "use a POSIX absolute path for [global].install_prefix."
                )
            path = Path(expanded)
            if not path.is_absolute():
                path = (cfg.repo_root / path).resolve()
            return path

        if self.license_profile is not None:
            profile_root_raw = cfg.profile_prefix_base or str(cfg.repo_root / "developer" / "prefixes")
            profile_root = _resolve_prefix(profile_root_raw) / self.license_profile.name
            if self.platform.os == "windows":
                prefixes["Release"] = profile_root
                prefixes["Debug"] = profile_root
                if "ASAN" in cfg.build_types:
                    prefixes["ASAN"] = profile_root / "ASAN"
                return prefixes
            prefixes["Release"] = profile_root / "Release"
            prefixes["Debug"] = profile_root / "Debug"
            if "ASAN" in cfg.build_types:
                prefixes["ASAN"] = profile_root / "ASAN"
            return prefixes

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
            f"license-profile:{self.license_profile.name if self.license_profile is not None else 'none'}",
        ]
        if self.platform.os == "windows":
            generator = str(cfg.windows.get("generator", ""))
            parts.append(f"gen:{generator}")
            for key in ("cc", "cxx", "ld", "ar", "ranlib"):
                value = self.toolchain.get(key)
                if not value:
                    continue
                normalized = str(value).replace("\\", "/").lower()
                parts.append(f"{key}:{normalized}")
                path = Path(value)
                if path.exists():
                    try:
                        parts.append(f"{key}_mtime:{int(path.stat().st_mtime)}")
                    except OSError:
                        pass
        return ";".join(parts)

    def _license_profile_cmake_args(self, repo_name: str) -> list[str]:
        return license_policy.profile_cmake_args(self.license_profile, repo_name)

    def _print_license_profile_notes(self) -> None:
        if self.license_profile is None:
            return
        excluded = ", ".join(sorted(self._license_profile_exclusions, key=str.lower))
        print("\n=== License Profile ===", flush=True)
        print(f"  profile: {self.license_profile.name} ({self.license_profile.linkage})", flush=True)
        print(f"  excluded: {excluded or '(none)'}", flush=True)
        for warning in license_policy.profile_warnings(self.license_profile, (repo.name for repo in self.repos)):
            print(f"  warning: {warning}", flush=True)

    def _windows_generator(self) -> str:
        return str(self.config.global_cfg.windows.get("generator", "ninja-msvc")).strip().lower()

    def _windows_expected_compilers(self) -> tuple[str, str]:
        generator = self._windows_generator()
        if generator in {"msvc-clang-cl", "ninja-clang-cl"}:
            return "clang-cl", "clang-cl"
        return "cl", "cl"

    def _windows_should_pin_cmake_compiler(self) -> bool:
        return self.platform.os == "windows" and self._windows_generator() in {"ninja-msvc", "ninja-clang-cl"}

    def _effective_host_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.config.global_cfg.env)
        if self.platform.os == "windows":
            env.update(self.config.global_cfg.windows_env)
        return env

    def _resolve_windows_compiler_path(self, compiler: str | None, env: dict[str, str] | None = None) -> str | None:
        if self.platform.os != "windows" or not compiler:
            return compiler

        resolved_env = env or self._effective_host_env()
        raw = str(compiler).strip().strip("\"'")
        if not raw:
            return None

        direct = Path(raw)
        if direct.is_absolute() and direct.exists():
            return str(direct)

        exe_name = raw if raw.lower().endswith(".exe") else f"{raw}.exe"
        arch_dir = "arm64" if self.platform.arch == "arm64" else "x64"
        host_arch = "Hostarm64" if self.platform.arch == "arm64" else "Hostx64"
        candidates: list[Path] = []
        fallback_candidates: list[Path] = []

        def _append_if_exists(path: Path, *, fallback: bool = False) -> None:
            if path.exists():
                (fallback_candidates if fallback else candidates).append(path)

        def _append_msvc_bins(root: Path) -> None:
            if not root.exists():
                return
            versions = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
            for version_dir in versions:
                _append_if_exists(version_dir / "bin" / host_arch / arch_dir / exe_name)

        if exe_name.lower() == "cl.exe":
            vc_tools = resolved_env.get("VCToolsInstallDir")
            if vc_tools:
                _append_if_exists(Path(vc_tools) / "bin" / host_arch / arch_dir / exe_name)

            vc_install = resolved_env.get("VCINSTALLDIR")
            if vc_install:
                _append_msvc_bins(Path(vc_install) / "Tools" / "MSVC")

            vs_install = resolved_env.get("VSINSTALLDIR")
            if vs_install:
                _append_msvc_bins(Path(vs_install) / "VC" / "Tools" / "MSVC")

            for env_key in ("ProgramFiles(x86)", "ProgramFiles"):
                root = resolved_env.get(env_key)
                if not root:
                    continue
                vs_root = Path(root) / "Microsoft Visual Studio"
                if not vs_root.exists():
                    continue
                years = sorted((p for p in vs_root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
                for year_dir in years:
                    for edition in ("BuildTools", "Community", "Professional", "Enterprise", "Preview"):
                        _append_msvc_bins(year_dir / edition / "VC" / "Tools" / "MSVC")

        if exe_name.lower() == "clang-cl.exe":
            # Prefer Visual Studio's bundled LLVM toolset for clang-cl generators.
            # Standalone LLVM remains available through an absolute cc/cxx override
            # or as a fallback when no VS clang-cl installation can be found.
            for env_key in ("VCToolsInstallDir", "VCINSTALLDIR", "VSINSTALLDIR"):
                base = resolved_env.get(env_key)
                if not base:
                    continue
                root = Path(base)
                if env_key == "VCToolsInstallDir":
                    if len(root.parents) > 1:
                        llvm_root = root.parents[1] / "Llvm"
                        _append_if_exists(llvm_root / arch_dir / "bin" / exe_name)
                        _append_if_exists(llvm_root / "bin" / exe_name)
                elif env_key == "VCINSTALLDIR":
                    _append_if_exists(root / "Tools" / "Llvm" / arch_dir / "bin" / exe_name)
                    _append_if_exists(root / "Tools" / "Llvm" / "bin" / exe_name)
                else:
                    _append_if_exists(root / "VC" / "Tools" / "Llvm" / arch_dir / "bin" / exe_name)
                    _append_if_exists(root / "VC" / "Tools" / "Llvm" / "bin" / exe_name)

            for env_key in ("ProgramFiles(x86)", "ProgramFiles"):
                root = resolved_env.get(env_key)
                if not root:
                    continue
                vs_root = Path(root) / "Microsoft Visual Studio"
                if not vs_root.exists():
                    continue
                years = sorted((p for p in vs_root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
                for year_dir in years:
                    for edition in ("BuildTools", "Community", "Professional", "Enterprise", "Preview"):
                        llvm_root = year_dir / edition / "VC" / "Tools" / "Llvm"
                        _append_if_exists(llvm_root / arch_dir / "bin" / exe_name)
                        _append_if_exists(llvm_root / "bin" / exe_name)

            for vs_root in (
                Path("C:/Program Files/Microsoft Visual Studio"),
                Path("C:/Program Files (x86)/Microsoft Visual Studio"),
            ):
                if not vs_root.exists():
                    continue
                years = sorted((p for p in vs_root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
                for year_dir in years:
                    for edition in ("BuildTools", "Community", "Professional", "Enterprise", "Preview"):
                        llvm_root = year_dir / edition / "VC" / "Tools" / "Llvm"
                        _append_if_exists(llvm_root / arch_dir / "bin" / exe_name)
                        _append_if_exists(llvm_root / "bin" / exe_name)

            for base in (
                Path("C:/Program Files/LLVM/bin"),
                Path("C:/LLVM/bin"),
            ):
                _append_if_exists(base / exe_name, fallback=True)

        if candidates:
            return str(candidates[0])

        found = shutil.which(raw, path=resolved_env.get("PATH"))
        if found:
            return found

        return str(fallback_candidates[0]) if fallback_candidates else raw

    def _resolve_windows_sdk_tool(self, tool_name: str, env: dict[str, str] | None = None) -> str | None:
        if self.platform.os != "windows":
            return None

        resolved_env = env or self._effective_host_env()
        raw = str(tool_name).strip().strip("\"'")
        if not raw:
            return None

        direct = Path(raw)
        if direct.is_absolute() and direct.exists():
            return str(direct)

        found = shutil.which(raw, path=resolved_env.get("PATH"))
        if found:
            return found

        exe_name = raw if raw.lower().endswith(".exe") else f"{raw}.exe"
        arch_dir = "arm64" if self.platform.arch == "arm64" else "x64"
        candidates: list[Path] = []
        seen: set[str] = set()

        def _append_if_exists(path: Path) -> None:
            norm = os.path.normcase(os.path.normpath(str(path)))
            if norm in seen:
                return
            if path.exists():
                seen.add(norm)
                candidates.append(path)

        bin_roots: list[Path] = []
        for env_key in ("WindowsSdkVerBinPath", "WindowsSdkBinPath"):
            value = resolved_env.get(env_key)
            if value:
                bin_roots.append(Path(value))

        sdk_dir = resolved_env.get("WindowsSdkDir") or resolved_env.get("WindowsSDKDir")
        sdk_version = resolved_env.get("WindowsSDKVersion") or resolved_env.get("WindowsSdkVersion")
        if sdk_dir:
            sdk_bin = Path(sdk_dir) / "bin"
            if sdk_version:
                bin_roots.append(sdk_bin / sdk_version)
            bin_roots.append(sdk_bin)

        for env_key in ("ProgramFiles(x86)", "ProgramFiles"):
            root = resolved_env.get(env_key)
            if not root:
                continue
            for kits_name in ("10", "11"):
                bin_roots.append(Path(root) / "Windows Kits" / kits_name / "bin")

        for root in (Path("C:/Program Files (x86)"), Path("C:/Program Files")):
            for kits_name in ("10", "11"):
                bin_roots.append(root / "Windows Kits" / kits_name / "bin")

        for root in bin_roots:
            if not root.exists():
                continue
            _append_if_exists(root / arch_dir / exe_name)
            version_dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
            for version_dir in version_dirs:
                _append_if_exists(version_dir / arch_dir / exe_name)

        return str(candidates[0]) if candidates else None

    def _resolve_windows_vcvarsall(self, env: dict[str, str] | None = None) -> Path | None:
        if self.platform.os != "windows":
            return None

        resolved_env = env or self._effective_host_env()
        candidates: list[Path] = []

        def _append(path: Path | None) -> None:
            if path is None:
                return
            candidate = path / "Auxiliary" / "Build" / "vcvarsall.bat"
            if candidate.exists():
                candidates.append(candidate)

        vc_install = resolved_env.get("VCINSTALLDIR")
        if vc_install:
            _append(Path(vc_install))

        vs_install = resolved_env.get("VSINSTALLDIR")
        if vs_install:
            _append(Path(vs_install) / "VC")

        compiler_path = self._resolve_windows_compiler_path("cl", resolved_env)
        if compiler_path:
            try:
                compiler_file = Path(compiler_path)
                if compiler_file.name.lower() == "cl.exe" and len(compiler_file.parents) >= 7:
                    _append(compiler_file.parents[6])
            except OSError:
                pass

        return candidates[0] if candidates else None

    def _load_windows_msvc_env(self, env: dict[str, str]) -> dict[str, str]:
        if self.platform.os != "windows":
            return {}
        if self._windows_msvc_env_loaded:
            return dict(self._windows_msvc_env_cache or {})

        self._windows_msvc_env_loaded = True
        self._windows_msvc_env_cache = {}

        vcvarsall = self._resolve_windows_vcvarsall(env)
        if vcvarsall is None:
            return {}

        system_root = env.get("SystemRoot") or os.environ.get("SystemRoot") or r"C:\Windows"
        cmd_exe = Path(system_root) / "System32" / "cmd.exe"
        if not cmd_exe.exists():
            return {}

        arch_arg = "arm64" if self.platform.arch == "arm64" else "x64"
        cmd = [str(cmd_exe), "/d", "/s", "/c", f'call "{vcvarsall}" {arch_arg} >nul && set']
        try:
            output = subprocess.check_output(cmd, env=env, text=True, errors="replace", stderr=subprocess.STDOUT)
        except (subprocess.CalledProcessError, OSError):
            return {}

        captured: dict[str, str] = {}
        for line in output.splitlines():
            if not line or line.startswith("=") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            captured[key] = value
            captured.setdefault(key.upper(), value)
        self._windows_msvc_env_cache = captured
        return dict(captured)

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

    @staticmethod
    def _windows_is_posix_ninja(path: str | Path) -> bool:
        text = re.sub(r"/+", "/", str(path).replace("\\", "/").lower())
        base = text.rsplit("/", 1)[-1]
        if base not in {"ninja", "ninja.exe"}:
            return False
        return "/usr/bin/" in text or ("/cygwin" in text and "/bin/" in text)

    def _windows_ninja_generator_active(self) -> bool:
        return self.platform.os == "windows" and self._windows_generator() in {"ninja-msvc", "ninja-clang-cl"}

    def _windows_path_tool_candidates(self, tool_name: str, env: dict[str, str]) -> list[Path]:
        candidates: list[Path] = []
        seen: set[str] = set()
        raw_path = env.get("PATH") or os.environ.get("PATH") or ""
        for entry in self._windows_split_env_path_list(raw_path):
            cleaned = entry.strip().strip("\"'")
            if not cleaned:
                continue
            for leaf in (f"{tool_name}.exe", tool_name):
                candidate = Path(cleaned) / leaf
                key = os.path.normcase(os.path.normpath(str(candidate)))
                if key in seen:
                    continue
                seen.add(key)
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    candidates.append(candidate)
        return candidates

    def _windows_native_ninja_probe_candidates(self, env: dict[str, str]) -> list[Path]:
        candidates = self._windows_path_tool_candidates("ninja", env)

        cmake = self._which_in_env("cmake", env)
        if cmake:
            candidates.append(Path(cmake).with_name("ninja.exe"))

        for env_name in ("VSINSTALLDIR", "VCINSTALLDIR"):
            value = env.get(env_name) or os.environ.get(env_name)
            if not value:
                continue
            root = Path(value)
            if env_name == "VCINSTALLDIR":
                root = root.parent
            candidates.append(root / "Common7" / "IDE" / "CommonExtensions" / "Microsoft" / "CMake" / "Ninja" / "ninja.exe")

        for env_name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            value = env.get(env_name) or os.environ.get(env_name)
            if not value:
                continue
            base = Path(value)
            candidates.append(base / "CMake" / "bin" / "ninja.exe")
            vs_base = base / "Microsoft Visual Studio"
            if vs_base.is_dir():
                candidates.extend(
                    vs_base.glob("*/**/Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe")
                )

        unique: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            key = os.path.normcase(os.path.normpath(str(candidate)))
            if key in seen:
                continue
            seen.add(key)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                unique.append(candidate)
        return unique

    def _resolve_windows_native_ninja(self, env: dict[str, str] | None = None) -> str:
        if self.platform.os != "windows":
            raise RuntimeError("native Windows ninja resolution is only valid on Windows")

        effective_env = env or self._effective_host_env()
        search_path = effective_env.get("PATH") or os.environ.get("PATH", "")

        for key in ("CMAKE_MAKE_PROGRAM", "NINJA"):
            override = _normalize_override(effective_env.get(key) or os.environ.get(key))
            if not override:
                continue
            resolved = _resolve_executable_candidate(override, search_path=search_path)
            if not resolved:
                raise RuntimeError(f"{key} is set to {override!r}, but that executable was not found")
            if self._windows_is_posix_ninja(resolved):
                raise RuntimeError(
                    f"{key} points to MSYS2/Cygwin POSIX Ninja ({resolved}); use a native Windows ninja.exe"
                )
            return resolved

        skipped_posix: list[str] = []
        for candidate in self._windows_native_ninja_probe_candidates(effective_env):
            if self._windows_is_posix_ninja(candidate):
                skipped_posix.append(str(candidate))
                continue
            return str(candidate)

        if skipped_posix:
            bad = skipped_posix[0]
            raise RuntimeError(
                "Windows Ninja generators require a native Windows ninja.exe. "
                f"CMake would pick POSIX Ninja from MSYS2/Cygwin ({bad}), which breaks MSVC try-compile commands. "
                "Install/use the Ninja bundled with CMake or Visual Studio, put it earlier in PATH, "
                "or set windows.env.CMAKE_MAKE_PROGRAM to that native ninja.exe."
            )

        raise RuntimeError(
            "Windows Ninja generators require native Windows ninja.exe, but none was found. "
            "Install Ninja via CMake/Visual Studio/winget, set windows.env.CMAKE_MAKE_PROGRAM, "
            "or use windows.generator = \"msvc\"."
        )

    def _resolve_windows_posix_shell(self, env: dict[str, str]) -> str | None:
        if self.platform.os != "windows":
            return None

        for name in ("bash", "bash.exe", "sh", "sh.exe"):
            resolved = self._which_in_env(name, env)
            if resolved:
                return resolved

        candidates: list[str] = []
        msystem_prefix = (
            env.get("MSYSTEM_PREFIX")
            or env.get("MINGW_PREFIX")
            or self.config.global_cfg.windows_env.get("MSYSTEM_PREFIX")
            or self.config.global_cfg.windows_env.get("MINGW_PREFIX")
            or os.environ.get("MSYSTEM_PREFIX")
            or os.environ.get("MINGW_PREFIX")
        )
        if msystem_prefix:
            base = Path(str(msystem_prefix))
            for name in ("bash.exe", "sh.exe", "bash", "sh"):
                candidates.append(str(base / "bin" / name))

        shell_env = env.get("SHELL") or os.environ.get("SHELL")
        if shell_env:
            shell_text = str(shell_env).strip().strip("\"'")
            if shell_text:
                if shell_text.startswith("/"):
                    if msystem_prefix:
                        prefix = Path(str(msystem_prefix))
                        if shell_text.startswith("/usr/bin/"):
                            candidates.append(str(prefix / "bin" / Path(shell_text).name))
                        else:
                            candidates.append(self._windows_path_to_msys(shell_text))
                else:
                    candidates.append(shell_text)

        for candidate in (
            r"C:\msys64\usr\bin\bash.exe",
            r"C:\msys64\usr\bin\sh.exe",
        ):
            candidates.append(candidate)

        seen: set[str] = set()
        for candidate in candidates:
            normalized = _normalize_override(candidate)
            if not normalized:
                continue
            if normalized.startswith("/") and re.match(r"^/[A-Za-z]/", normalized):
                drive = normalized[1].upper()
                tail = normalized[2:].replace("/", "\\")
                normalized = f"{drive}:{tail}"
            key = os.path.normcase(os.path.normpath(normalized))
            if key in seen:
                continue
            seen.add(key)
            path = Path(normalized)
            if path.exists():
                return str(path)
        return None

    def _resolve_windows_msys_tool(self, env: dict[str, str], *names: str) -> str | None:
        if self.platform.os != "windows":
            return None

        for name in names:
            resolved = self._which_in_env(name, env)
            if resolved:
                return resolved

        candidates: list[str] = []
        msystem_prefix = (
            env.get("MSYSTEM_PREFIX")
            or env.get("MINGW_PREFIX")
            or self.config.global_cfg.windows_env.get("MSYSTEM_PREFIX")
            or self.config.global_cfg.windows_env.get("MINGW_PREFIX")
            or os.environ.get("MSYSTEM_PREFIX")
            or os.environ.get("MINGW_PREFIX")
        )
        if msystem_prefix:
            prefix = Path(str(msystem_prefix))
            for name in names:
                candidates.append(str(prefix / "bin" / name))

        for name in names:
            candidates.append(str(Path(r"C:\msys64\usr\bin") / name))

        seen: set[str] = set()
        for candidate in candidates:
            normalized = _normalize_override(candidate)
            if not normalized:
                continue
            if normalized.startswith("/") and re.match(r"^/[A-Za-z]/", normalized):
                drive = normalized[1].upper()
                tail = normalized[2:].replace("/", "\\")
                normalized = f"{drive}:{tail}"
            key = os.path.normcase(os.path.normpath(normalized))
            if key in seen:
                continue
            seen.add(key)
            path = Path(normalized)
            if path.exists():
                return str(path)
        return None

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

        if self.platform.os == "windows":
            cc, cxx = self._windows_expected_compilers()
            host_env = self._effective_host_env()
            toolchain.setdefault("cc", cc)
            toolchain.setdefault("cxx", cxx)
            toolchain["cc"] = self._resolve_windows_compiler_path(toolchain.get("cc"), host_env)
            toolchain["cxx"] = self._resolve_windows_compiler_path(toolchain.get("cxx"), host_env)
            return toolchain

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

    def _normalize_posix_shell_scripts(self, repo_name: str, paths: list[Path]) -> None:
        if self.platform.os == "windows":
            return

        changed: list[str] = []
        for path in paths:
            if not path.exists() or not path.is_file():
                continue
            data = path.read_bytes()
            if b"\r\n" not in data:
                continue
            changed.append(path.name)

        if changed:
            preview = ", ".join(changed[:4])
            if len(changed) > 4:
                preview += f", +{len(changed) - 4} more"
            if self.dry_run:
                print(f"[dry-run] {repo_name}: normalize CRLF line endings for POSIX scripts: {preview}", flush=True)
                return
            for path in paths:
                if not path.exists() or not path.is_file():
                    continue
                data = path.read_bytes()
                if b"\r\n" in data:
                    path.write_bytes(data.replace(b"\r\n", b"\n"))
            print(f"[note] {repo_name}: normalized CRLF line endings for POSIX scripts: {preview}", flush=True)

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

        if self.platform.os == "linux":
            runtime_paths = [
                str(prefix / "lib"),
                str(prefix / "lib64"),
            ]
            existing_runtime = [
                env.get("LD_LIBRARY_PATH", ""),
                os.environ.get("LD_LIBRARY_PATH", ""),
            ]
            for value in existing_runtime:
                if value:
                    runtime_paths.extend(value.split(os.pathsep))
            deduped_runtime_paths: list[str] = []
            seen_runtime: set[str] = set()
            for path_item in runtime_paths:
                if not path_item:
                    continue
                normalized = os.path.normcase(os.path.normpath(path_item))
                if normalized in seen_runtime:
                    continue
                seen_runtime.add(normalized)
                deduped_runtime_paths.append(path_item)
            env["LD_LIBRARY_PATH"] = os.pathsep.join(deduped_runtime_paths)

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
            elif _ensure_writable(fallback_cache_dir):
                env["CCACHE_DIR"] = str(fallback_cache_dir)
            else:
                env["CCACHE_DISABLE"] = "1"

        if self.platform.os == "windows":
            for var in ("CC", "CXX", "CMAKE_C_COMPILER", "CMAKE_CXX_COMPILER"):
                env.pop(var, None)
            effective_env = dict(os.environ)
            effective_env.update(env)
            msys_tool_dirs: list[Path] = []
            msystem_prefix = (
                effective_env.get("MSYSTEM_PREFIX")
                or effective_env.get("MINGW_PREFIX")
                or self.config.global_cfg.windows_env.get("MSYSTEM_PREFIX")
                or self.config.global_cfg.windows_env.get("MINGW_PREFIX")
                or os.environ.get("MSYSTEM_PREFIX")
                or os.environ.get("MINGW_PREFIX")
            )
            if msystem_prefix:
                msys_bin = Path(str(msystem_prefix)) / "bin"
                if msys_bin.exists():
                    msys_tool_dirs.append(msys_bin)
            for fallback in (Path(r"C:\msys64\usr\bin"), Path(r"C:\msys64\mingw64\bin")):
                if fallback.exists():
                    msys_tool_dirs.append(fallback)
            if msys_tool_dirs:
                env.setdefault("PATH", os.environ.get("PATH", ""))
                self._prepend_windows_env_paths(env, "PATH", msys_tool_dirs)
                effective_env = dict(os.environ)
                effective_env.update(env)
            msvc_env = self._load_windows_msvc_env(effective_env)
            for key in (
                "PATH",
                "INCLUDE",
                "LIB",
                "LIBPATH",
                "VCINSTALLDIR",
                "VCToolsInstallDir",
                "VSINSTALLDIR",
                "WindowsSdkDir",
                "WindowsSDKDir",
                "WindowsSdkVersion",
                "WindowsSDKVersion",
                "WindowsSdkBinPath",
                "WindowsSdkVerBinPath",
                "UniversalCRTSdkDir",
                "UCRTVersion",
            ):
                value = msvc_env.get(key)
                if value:
                    env[key] = value
            effective_env = dict(os.environ)
            effective_env.update(env)
            compiler_dirs: list[Path] = []
            for tool_name in ("cc", "cxx"):
                compiler_path = self.toolchain.get(tool_name)
                if compiler_path:
                    compiler_dirs.append(Path(compiler_path).parent)
            if compiler_dirs:
                env.setdefault("PATH", os.environ.get("PATH", ""))
                self._prepend_windows_env_paths(env, "PATH", compiler_dirs)
                effective_env = dict(os.environ)
                effective_env.update(env)
            nasm = resolve_nasm_executable(effective_env, platform_os="windows")
            if nasm:
                env.setdefault("PATH", os.environ.get("PATH", ""))
                self._prepend_windows_env_paths(env, "PATH", [Path(nasm).parent])
                effective_env = dict(os.environ)
                effective_env.update(env)
            sdk_tool_dirs: list[Path] = []
            for tool_name in ("rc.exe", "mt.exe"):
                tool_path = self._resolve_windows_sdk_tool(tool_name, effective_env)
                if tool_path:
                    sdk_tool_dirs.append(Path(tool_path).parent)
            if sdk_tool_dirs:
                env.setdefault("PATH", os.environ.get("PATH", ""))
                self._prepend_windows_env_paths(env, "PATH", sdk_tool_dirs)
            self._adapt_windows_env_for_msys(env)

        return env

    def _env_for_repo_build(self, repo: RepoConfig, build_type: str, prefix: Path) -> dict[str, str]:
        env = self._env_for_build(build_type, prefix)
        recipe_registry.build_env(repo.name, self, repo, build_type, prefix, env)
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
            generator = self._windows_generator()
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

    def _maybe_skip_missing(self, repo: RepoConfig, path: Path) -> bool:
        recipe_decision = recipe_registry.missing_source_skip(repo.name, self, repo, path)
        if recipe_decision is not None:
            return recipe_decision
        if path.exists():
            return False
        if repo.optional and not repo.url:
            print(f"[skip] {repo.name}: missing optional source at {path}")
            return True
        return False

    def _source_tree_dir(self, repo: RepoConfig, repo_root: Path) -> Path:
        if repo.source_subdir:
            return repo_root / repo.source_subdir
        return repo_root

    def _source_tree_contract_shim_text(self, repo: RepoConfig, source_dir: Path) -> str:
        includes: list[str] = []
        configure_presets: list[dict[str, object]] = []
        seen_include_paths: set[str] = set()
        for build_type in self._build_type_order():
            install_prefix = self.prefixes.get(build_type)
            if install_prefix is None:
                continue
            presets_path = self._prefix_contract_file_paths(install_prefix)["presets"]
            include_path = str(presets_path.resolve())
            if include_path not in seen_include_paths:
                seen_include_paths.add(include_path)
                includes.append(include_path)
            token = self._prefix_contract_token(install_prefix)
            preset_name = f"oiio-builder-{build_type.lower()}"
            configure_presets.append(
                {
                    "name": preset_name,
                    "displayName": f"oiio-builder {build_type}",
                    "description": f"{repo.name}: build against/install into {install_prefix}",
                    "inherits": [f"oiio-builder-prefix-{token}"],
                    "binaryDir": f"${{sourceDir}}/out/build/{preset_name}",
                    "cacheVariables": {
                        "CMAKE_BUILD_TYPE": build_type,
                        "CMAKE_INSTALL_PREFIX": self._cmake_path_arg(install_prefix),
                    },
                }
            )

        payload = {
            "version": 4,
            "vendor": {
                "oiio-builder": {
                    "managed": True,
                    "schema": 1,
                    "repo": repo.name,
                    "sourceDir": str(source_dir),
                }
            },
            "include": includes,
            "configurePresets": configure_presets,
        }
        return json.dumps(payload, indent=2) + "\n"

    def _is_managed_prefix_contract_shim(self, path: Path) -> bool:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        vendor = data.get("vendor")
        if not isinstance(vendor, dict):
            return False
        oiio_builder = vendor.get("oiio-builder")
        if not isinstance(oiio_builder, dict):
            return False
        return bool(oiio_builder.get("managed"))

    def _apply_prefix_contract_to_source_tree(self, repo: RepoConfig, repo_root: Path) -> None:
        source_dir = self._source_tree_dir(repo, repo_root)
        if not (source_dir / "CMakeLists.txt").exists():
            return

        shim_path = source_dir / "CMakeUserPresets.json"
        if shim_path.exists() and not self._is_managed_prefix_contract_shim(shim_path):
            print(
                f"[skip] {repo.name}: existing unmanaged {shim_path.name} present at {source_dir}",
                flush=True,
            )
            return

        text = self._source_tree_contract_shim_text(repo, source_dir)
        self._write_managed_text_file(shim_path, text, label=f"{repo.name} source preset shim")

    def _prepare_repo_source(self, repo: RepoConfig, src_dir: Path) -> None:
        lock = self._repo_source_prepare_locks.get(repo.name)
        if lock is None:
            return
        with lock:
            if repo.name in self._repo_source_prepared:
                return
            recipe_registry.patch_source(repo.name, self, src_dir)
            if self.apply_prefix_contract:
                self._apply_prefix_contract_to_source_tree(repo, src_dir)
            self._repo_source_prepared.add(repo.name)

    def _source_prep_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.config.global_cfg.env)
        if self.platform.os == "windows":
            env.update(self.config.global_cfg.windows_env)
        return env

    def _patch_glew_macos(self, src_dir: Path) -> None:
        if self.platform.os != "macos":
            return
        cmake_lists = src_dir / "CMakeLists.txt"
        if not cmake_lists.exists():
            return
        text = cmake_lists.read_text(encoding="utf-8")
        if "AGL_LIBRARY AGL REQUIRED" not in text:
            return
        pattern = (
            r"if\(APPLE AND CMAKE_SYSTEM_VERSION VERSION_LESS \"25\.0\.0\"\)\s*\n"
            r"\s*find_library\(AGL_LIBRARY AGL REQUIRED\)\s*\n"
            r"\s*list\(APPEND LIBRARIES \$\{AGL_LIBRARY\}\)"
        )
        replacement = (
            "if(APPLE)\\n"
            "  find_library(AGL_LIBRARY AGL)\\n"
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

    def _minizip_exports_ppmd(self, prefix: Path) -> bool:
        cmake_dir = prefix / "lib" / "cmake" / "minizip-ng"
        patterns = (
            "find_dependency(PPMD",
            "MINIZIP::ppmd",
        )
        for name in ("minizip-ng-config.cmake", "minizip-ng.cmake"):
            path = cmake_dir / name
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(pattern in text for pattern in patterns):
                return True
        return False

    def _qt_exports_dbus(self, prefix: Path) -> bool:
        # Key off the QtGui export that iv actually consumes, not on the
        # presence of standalone Qt6DBus package files, which may be stale
        # leftovers in a prefix after Qt was rebuilt with -no-dbus.
        cmake_files = (
            prefix / "lib" / "cmake" / "Qt6Gui" / "Qt6GuiTargets.cmake",
        )
        patterns = (
            "Qt6::DBus",
            "dbus-1",
        )
        for path in cmake_files:
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(pattern in text for pattern in patterns):
                return True
        return False

    def _ensure_ppmd_package(self, prefix: Path, build_type: str) -> None:
        if self.dry_run:
            return

        # Only materialize a synthetic PPMD package when the installed
        # minizip-ng export actually requires it. This avoids reviving stale
        # libppmd artifacts left in a prefix after minizip-ng was rebuilt with
        # MZ_PPMD=OFF.
        if not self._minizip_exports_ppmd(prefix):
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
            "license_profile": self.license_profile.name if self.license_profile is not None else None,
        }
        if repo.build_system == "cmake":
            effective = self._repo_cmake_effective_toml_options(repo.name)
            if effective.cache or effective.args:
                payload["cmake_cache_toml"] = effective.cache
                payload["cmake_args_toml"] = effective.args
        recipe_revision = recipe_registry.stamp_revision(repo.name)
        if recipe_revision is not None:
            payload["builder_patch_rev"] = recipe_revision
        recipe_registry.stamp_payload(repo.name, self, repo, ctx, payload)
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

    def _install_only(self, repo: RepoConfig, ctx: BuildContext, env: dict[str, str]) -> bool:
        return build_backends.install_only(self, ctx, env)

    def _build_repo(self, repo: RepoConfig, build_type: str, deps_heads: dict[str, str | None]) -> tuple[str, str]:
        if not repo.build_system:
            return "skipped", "no-build-system"

        install_prefix = self.prefixes[build_type]
        build_dir = self.config.global_cfg.build_root / build_type / repo.name
        repo_root = self.repo_paths[repo.name]
        src_dir = repo_root
        if repo.source_subdir:
            src_dir = repo_root / repo.source_subdir

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

        self._prepare_repo_source(repo, repo_root)
        recipe_registry.pre_build(repo.name, self, repo, ctx, env)

        build_backends.build(self, ctx, env)

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
        build_system = recipe_registry.resolve_build_system(repo.name, self, repo, src_dir)
        if build_system is not None:
            return replace(repo, build_system=build_system)
        return repo

    def _repo_requires_exclusive_build(self, repo: RepoConfig) -> bool:
        if self.platform.os == "windows":
            return False
        return repo.build_system == "qt6"

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
                self._print_license_profile_notes()
                if first_exc is not None:
                    raise first_exc
        finally:
            self._parallel_build_type_count = 1

    def _sync_repos(self, order: list[str], repos_by_name: dict[str, RepoConfig]) -> None:
        """Resolve repo paths and perform clone/fetch/checkout/update."""
        if self.dry_run and not self.no_update:
            print(
                "[note] --dry-run prints git update commands but does not advance checkouts; "
                "stamp checks use the current local HEAD.",
                flush=True,
            )
        for repo_name in order:
            repo = repos_by_name[repo_name]
            repo_dir = self._resolve_repo_dir(repo)
            self.repo_paths[repo.name] = repo_dir
            if self._maybe_skip_missing(repo, repo_dir):
                continue
            if recipe_registry.skip_update(repo.name, self, repo):
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
        self._ensure_prefix_contracts()

        for repo_name in order:
            repo = repos_by_name[repo_name]
            repo_root = self.repo_paths.get(repo.name, self._resolve_repo_dir(repo))
            src_dir = repo_root
            if repo.source_subdir:
                src_dir = repo_root / repo.source_subdir
            if not repo_root.exists() or not src_dir.exists():
                continue
            self._prepare_repo_source(repo, repo_root)

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
        self._ensure_prefix_contracts()

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
            self._print_license_profile_notes()
            raise
        report.print()
        self._print_license_profile_notes()
        return 0
