from __future__ import annotations


STAMP_REVISION = "1"


def cmake_args(builder, ctx) -> list[str]:
    # XZ checks Landlock even when its command-line tools are disabled, and
    # rejects sanitizer flags unless sandboxing is explicitly disabled.
    if builder.platform.os == "linux" and ctx.build_type == "ASAN":
        return ["-DXZ_SANDBOX=no"]
    return []


def resolve_build_system(builder, _repo, src_dir) -> str | None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if builder.config.global_cfg.xz_use_autotools or not cmake_lists.exists():
        return "autotools"
    return "cmake"


def autotools_args(_builder, _repo) -> list[str]:
    return ["--disable-nls", "--disable-xz", "--disable-xzdec", "--disable-lzmadec", "--disable-lzmainfo"]
