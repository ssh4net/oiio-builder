from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)


def _posix_line_ending_files(src_dir: Path) -> list[Path]:
    paths = [
        src_dir / "png.h",
        src_dir / "pngconf.h",
        src_dir / "pnglibconf.h.prebuilt",
        src_dir / "pngusr.dfa",
        src_dir / "libpng-config.in",
        src_dir / "libpng.pc.in",
        src_dir / "scripts" / "macro.lst",
    ]
    for root in (src_dir / "scripts", src_dir / "scripts" / "pnglibconf", src_dir / "scripts" / "cmake"):
        if not root.is_dir():
            continue
        paths.extend(
            sorted(
                path
                for path in root.iterdir()
                if path.is_file()
                and (
                    path.suffix.lower() in {".awk", ".c", ".dfa", ".in", ".lst"}
                    or path.name.endswith(".cmake.in")
                )
            )
        )
    return paths


def pre_build(builder, _repo, ctx, _env) -> None:
    builder._normalize_posix_shell_scripts("libpng", _posix_line_ending_files(ctx.src_dir))
    builder._ensure_png16_include_alias(ctx.install_prefix)


def post_install(builder, install_prefix, _build_type: str) -> None:
    builder._ensure_png16_include_alias(install_prefix)
