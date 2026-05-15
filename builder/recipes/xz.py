from __future__ import annotations


def resolve_build_system(builder, _repo, src_dir) -> str | None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if builder.config.global_cfg.xz_use_autotools or not cmake_lists.exists():
        return "autotools"
    return "cmake"


def autotools_args(_builder, _repo) -> list[str]:
    return ["--disable-nls", "--disable-xz", "--disable-xzdec", "--disable-lzmadec", "--disable-lzmainfo"]
