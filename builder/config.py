from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import os
import tomllib


@dataclass
class RepoConfig:
    name: str
    dir: str
    dir_candidates: list[str] = field(default_factory=list)
    url: str | None = None
    enabled: bool = True
    build_system: str | None = None  # cmake, autotools, giflib, ffmpeg, libiconv
    ref: str | None = None
    ref_type: str = "branch"  # branch, tag, commit
    deps: list[str] = field(default_factory=list)
    source_subdir: str | None = None
    cmake_args: list[str] = field(default_factory=list)
    cxx_standard: int | None = None
    shared: bool | None = None
    optional: bool = False
    group: str | None = None


@dataclass
class GlobalConfig:
    repo_root: Path
    src_root: Path
    build_root: Path
    prefix_base: str | None
    prefix_layout: str  # "suffix" (legacy) or "by-build-type"
    build_types: list[str]
    preferred_repo_order: list[str]
    cxx_standard: int
    cxx_extensions: bool
    use_libcxx: bool
    use_lld: bool
    static_default: bool
    pic: bool
    jobs: int
    debug_suffix: str
    asan_suffix: str
    env: dict[str, str] = field(default_factory=dict)
    only: set[str] = field(default_factory=set)
    skip: set[str] = field(default_factory=set)
    no_update: bool = True
    windows: dict[str, Any] = field(default_factory=dict)
    windows_env: dict[str, str] = field(default_factory=dict)
    # Build group toggles
    build_gl_stack: bool = True
    build_imageio_stack: bool = True
    build_exr_stack: bool = True
    build_gtest: bool = False
    build_libjxl: bool = True
    build_libuhdr: bool = True
    build_ocio: bool = True
    build_libraw: bool = True
    build_libheif: bool = True
    build_aom: bool = True
    build_libde265: bool = True
    build_x265: bool = True
    build_kvazaar: bool = True
    build_webp: bool = True
    build_ptex: bool = True
    build_pybind11: bool = True
    build_ffmpeg: bool = True
    build_oiio: bool = True
    build_qt6: bool = False
    build_dng_sdk: bool = False
    openimageio_patch_png_include: bool = True
    # Repo-specific feature toggles
    openjpeg_build_codec: str | None = None
    ocio_build_apps: str = "OFF"
    libjxl_enable_tools: str = "ON"
    libraw_enable_examples: str = "ON"
    libraw_enable_openmp: str = "OFF"
    xz_use_autotools: bool = False
    lcms2_use_autotools: bool = False
    # Toolchain overrides (optional)
    cc: str | None = None
    cxx: str | None = None
    ld: str | None = None
    ar: str | None = None
    ranlib: str | None = None


@dataclass
class Config:
    global_cfg: GlobalConfig
    repos: list[RepoConfig]

    @property
    def build_types(self) -> list[str]:
        return self.global_cfg.build_types

    @build_types.setter
    def build_types(self, value: list[str]) -> None:
        self.global_cfg.build_types = value

    @property
    def only(self) -> set[str]:
        return self.global_cfg.only

    @only.setter
    def only(self, value: set[str]) -> None:
        self.global_cfg.only = value

    @property
    def skip(self) -> set[str]:
        return self.global_cfg.skip

    @skip.setter
    def skip(self, value: set[str]) -> None:
        self.global_cfg.skip = value



def _expand_path(value: str, base: Path | None = None) -> Path:
    if base is None:
        base = Path.cwd()
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _merge_config_table(base: dict[str, Any], override: dict[str, Any], *, context: str) -> dict[str, Any]:
    """Shallow-merge two TOML tables, with special handling for nested `env` tables."""
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key == "env":
            if value is None:
                continue
            if not isinstance(value, dict):
                raise TypeError(f"{context}.env: expected table, got {type(value).__name__}")
            base_env = merged.get("env", {})
            if base_env is None:
                base_env = {}
            if not isinstance(base_env, dict):
                raise TypeError(f"{context}.env: base value is not a table")
            env_merged = dict(base_env)
            env_merged.update(value)
            merged["env"] = env_merged
            continue
        merged[key] = value
    return merged


def _validate_user_override_keys(user_table: dict[str, Any], *, allowed: set[str], context: str) -> None:
    unknown = sorted(key for key in user_table.keys() if key not in allowed)
    if unknown:
        names_str = ", ".join(unknown)
        raise ValueError(f"Unknown key(s) in {context}: {names_str}")


def load_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    repo_root = path.parent
    if not isinstance(data, dict):
        raise TypeError(f"{path}: expected TOML table at top-level")

    global_data = data.get("global", {})
    if global_data is None:
        global_data = {}
    if not isinstance(global_data, dict):
        raise TypeError(f"{path}: [global] must be a table")

    windows_section = data.get("windows", {})
    if windows_section is None:
        windows_section = {}
    if not isinstance(windows_section, dict):
        raise TypeError(f"{path}: [windows] must be a table")

    # Optional local overrides (gitignored).
    user_path = repo_root / "build.user.toml"
    if user_path.exists():
        user_data = tomllib.loads(user_path.read_text(encoding="utf-8"))
        if user_data is None:
            user_data = {}
        if not isinstance(user_data, dict):
            raise TypeError(f"{user_path}: expected TOML table at top-level")

        user_global = user_data.get("global", {})
        if user_global is None:
            user_global = {}
        if not isinstance(user_global, dict):
            raise TypeError(f"{user_path}: [global] must be a table")

        allowed_global_keys = {
            "src_root",
            "build_root",
            "prefix_base",
            "prefix_layout",
            "build_types",
            "preferred_repo_order",
            "cxx_standard",
            "cxx_extensions",
            "use_libcxx",
            "use_lld",
            "static_default",
            "pic",
            "jobs",
            "debug_suffix",
            "asan_suffix",
            "env",
            "no_update",
            # Group toggles
            "build_gl_stack",
            "build_imageio_stack",
            "build_exr_stack",
            "build_gtest",
            "build_libjxl",
            "build_libuhdr",
            "build_ocio",
            "build_libraw",
            "build_libheif",
            "build_aom",
            "build_libde265",
            "build_x265",
            "build_kvazaar",
            "build_webp",
            "build_ptex",
            "build_pybind11",
            "build_ffmpeg",
            "build_oiio",
            "build_qt6",
            "build_dng_sdk",
            "openimageio_patch_png_include",
            # Repo-specific switches
            "openjpeg_build_codec",
            "ocio_build_apps",
            "libjxl_enable_tools",
            "libraw_enable_examples",
            "libraw_enable_openmp",
            "xz_use_autotools",
            "lcms2_use_autotools",
            # Toolchain overrides
            "cc",
            "cxx",
            "ld",
            "ar",
            "ranlib",
        }
        _validate_user_override_keys(user_global, allowed=allowed_global_keys, context=f"{user_path}:[global]")
        global_data = _merge_config_table(global_data, user_global, context=f"{user_path}:[global]")

        user_windows = user_data.get("windows", {})
        if user_windows is None:
            user_windows = {}
        if not isinstance(user_windows, dict):
            raise TypeError(f"{user_path}: [windows] must be a table")

        allowed_windows_keys = {
            "generator",
            "vs_generator",
            "install_prefix",
            "asan_prefix",
            "debug_postfix",
            "build_ffmpeg",
            "msvc_runtime",
            "python_wrappers",
            "clangcl_extra_flags",
            "clangcl_extra_flags_append",
            "env",
        }
        _validate_user_override_keys(user_windows, allowed=allowed_windows_keys, context=f"{user_path}:[windows]")
        windows_section = _merge_config_table(windows_section, user_windows, context=f"{user_path}:[windows]")

    src_root = _expand_path(global_data.get("src_root", ".."), repo_root)
    build_root = _expand_path(global_data.get("build_root", "./_build"), repo_root)
    prefix_base = global_data.get("prefix_base")
    if isinstance(prefix_base, str):
        prefix_base = os.path.expandvars(prefix_base)
        prefix_base = os.path.expanduser(prefix_base)
        prefix_base = prefix_base.strip() or None

    prefix_layout_raw = global_data.get("prefix_layout", "suffix")
    prefix_layout = str(prefix_layout_raw).strip().lower().replace("_", "-")
    if prefix_layout in {"suffix", "legacy", "legacy-suffix"}:
        prefix_layout = "suffix"
    elif prefix_layout in {"by-build-type", "by-buildtype", "per-build-type", "per-buildtype"}:
        prefix_layout = "by-build-type"
    else:
        raise ValueError(f"Invalid [global].prefix_layout={prefix_layout_raw!r} (expected 'suffix' or 'by-build-type')")

    build_types = global_data.get("build_types", ["Debug", "Release", "ASAN"])
    build_types = [v.capitalize() if v.lower() != "asan" else "ASAN" for v in build_types]

    preferred_repo_order_raw = global_data.get("preferred_repo_order", [])
    if preferred_repo_order_raw is None:
        preferred_repo_order_raw = []
    if not isinstance(preferred_repo_order_raw, list) or any(not isinstance(item, str) for item in preferred_repo_order_raw):
        raise TypeError("[global].preferred_repo_order must be a list[str]")
    preferred_repo_order = [item.strip() for item in preferred_repo_order_raw if item.strip()]

    openjpeg_build_codec = global_data.get("openjpeg_build_codec")
    if isinstance(openjpeg_build_codec, str) and not openjpeg_build_codec.strip():
        openjpeg_build_codec = None

    windows_env = {str(k): str(v) for k, v in windows_section.get("env", {}).items()}
    global_cfg = GlobalConfig(
        repo_root=repo_root,
        src_root=src_root,
        build_root=build_root,
        prefix_base=prefix_base,
        prefix_layout=prefix_layout,
        build_types=build_types,
        preferred_repo_order=preferred_repo_order,
        cxx_standard=int(global_data.get("cxx_standard", 20)),
        cxx_extensions=bool(global_data.get("cxx_extensions", False)),
        use_libcxx=bool(global_data.get("use_libcxx", True)),
        use_lld=bool(global_data.get("use_lld", True)),
        static_default=bool(global_data.get("static_default", True)),
        pic=bool(global_data.get("pic", True)),
        jobs=int(global_data.get("jobs", 0)),
        debug_suffix=str(global_data.get("debug_suffix", "d")),
        asan_suffix=str(global_data.get("asan_suffix", "a")),
        env={str(k): str(v) for k, v in global_data.get("env", {}).items()},
        windows={str(k): v for k, v in windows_section.items()},
        windows_env=windows_env,
        no_update=bool(global_data.get("no_update", True)),
        build_gl_stack=bool(global_data.get("build_gl_stack", True)),
        build_imageio_stack=bool(global_data.get("build_imageio_stack", True)),
        build_exr_stack=bool(global_data.get("build_exr_stack", True)),
        build_gtest=bool(global_data.get("build_gtest", False)),
        build_libjxl=bool(global_data.get("build_libjxl", True)),
        build_libuhdr=bool(global_data.get("build_libuhdr", True)),
        build_ocio=bool(global_data.get("build_ocio", True)),
        build_libraw=bool(global_data.get("build_libraw", True)),
        build_libheif=bool(global_data.get("build_libheif", True)),
        build_aom=bool(global_data.get("build_aom", True)),
        build_libde265=bool(global_data.get("build_libde265", True)),
        build_x265=bool(global_data.get("build_x265", True)),
        build_kvazaar=bool(global_data.get("build_kvazaar", True)),
        build_webp=bool(global_data.get("build_webp", True)),
        build_ptex=bool(global_data.get("build_ptex", True)),
        build_pybind11=bool(global_data.get("build_pybind11", True)),
        build_ffmpeg=bool(global_data.get("build_ffmpeg", True)),
        build_oiio=bool(global_data.get("build_oiio", True)),
        build_qt6=bool(global_data.get("build_qt6", False)),
        build_dng_sdk=bool(global_data.get("build_dng_sdk", False)),
        openimageio_patch_png_include=bool(global_data.get("openimageio_patch_png_include", True)),
        openjpeg_build_codec=openjpeg_build_codec,
        ocio_build_apps=str(global_data.get("ocio_build_apps", "OFF")),
        libjxl_enable_tools=str(global_data.get("libjxl_enable_tools", "ON")),
        libraw_enable_examples=str(global_data.get("libraw_enable_examples", "ON")),
        libraw_enable_openmp=str(global_data.get("libraw_enable_openmp", "OFF")),
        xz_use_autotools=bool(global_data.get("xz_use_autotools", False)),
        lcms2_use_autotools=bool(global_data.get("lcms2_use_autotools", False)),
        cc=global_data.get("cc"),
        cxx=global_data.get("cxx"),
        ld=global_data.get("ld"),
        ar=global_data.get("ar"),
        ranlib=global_data.get("ranlib"),
    )

    repos: list[RepoConfig] = []
    for entry in data.get("repos", []):
        repos.append(
            RepoConfig(
                name=str(entry["name"]),
                dir=str(entry.get("dir") or entry["name"]),
                dir_candidates=list(entry.get("dir_candidates", [])),
                url=entry.get("url"),
                enabled=bool(entry.get("enabled", True)),
                build_system=entry.get("build_system"),
                ref=entry.get("ref"),
                ref_type=str(entry.get("ref_type", "branch")),
                deps=list(entry.get("deps", [])),
                source_subdir=entry.get("source_subdir"),
                cmake_args=list(entry.get("cmake_args", [])),
                cxx_standard=entry.get("cxx_standard"),
                shared=entry.get("shared"),
                optional=bool(entry.get("optional", False)),
                group=entry.get("group"),
            )
        )

    known_repos = {repo.name for repo in repos}
    unknown_order = sorted(name for name in global_cfg.preferred_repo_order if name not in known_repos)
    if unknown_order:
        names_str = ", ".join(unknown_order)
        raise ValueError(f"Unknown repo name(s) in [global].preferred_repo_order: {names_str}")

    return Config(global_cfg=global_cfg, repos=repos)
