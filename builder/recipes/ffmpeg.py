from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    if builder.platform.os == "windows":
        if builder._ffmpeg_enabled() and not builder.dry_run:
            print(
                "[skip] ffmpeg: native build step is disabled on Windows; "
                "prebuilt FFmpeg is consumed via FFmpeg_ROOT/FFMPEG_ROOT or <src_root>/ffmpeg",
                flush=True,
            )
        return False
    if not imageio_enabled(builder):
        return False
    return bool(builder._ffmpeg_enabled())
