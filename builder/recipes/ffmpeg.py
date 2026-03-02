from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    if builder.platform.os == "windows":
        if builder._windows_ffmpeg_native_build_enabled():
            return bool(builder._ffmpeg_enabled()) and imageio_enabled(builder)
        if builder._ffmpeg_enabled() and not builder.dry_run:
            print(
                "[skip] ffmpeg: native build step is disabled on Windows; "
                "run from an MSYS2 shell (MSYSTEM set) to build from source, "
                "otherwise prebuilt FFmpeg is consumed via FFmpeg_ROOT/FFMPEG_ROOT or <src_root>/ffmpeg",
                flush=True,
            )
        return False
    if not imageio_enabled(builder):
        return False
    return bool(builder._ffmpeg_enabled())
