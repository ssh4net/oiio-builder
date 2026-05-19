from __future__ import annotations


def _cfg(builder):
    return builder.config.global_cfg


def gl_enabled(builder) -> bool:
    return bool(_cfg(builder).build_gl_stack)


def imageio_enabled(builder) -> bool:
    return bool(_cfg(builder).build_imageio_stack)


def exr_enabled(builder) -> bool:
    return bool(_cfg(builder).build_exr_stack)


def ocio_enabled(builder) -> bool:
    return bool(_cfg(builder).build_ocio)


def qt6_enabled(builder) -> bool:
    return bool(getattr(_cfg(builder), "build_qt6", False))


def cpython_requested(builder) -> bool:
    cfg = _cfg(builder)
    return bool(getattr(cfg, "build_cpython", True))


def ffmpeg_enabled(builder) -> bool:
    cfg = _cfg(builder)
    enabled = bool(cfg.build_ffmpeg)
    if builder.platform.os == "windows":
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


def windows_use_ffmpeg_from_prefix(builder) -> bool:
    if builder.platform.os != "windows":
        return False
    raw = _cfg(builder).windows.get("use_ffmpeg_from_prefix")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"0", "false", "off", "no"}:
        return False
    if value in {"1", "true", "on", "yes"}:
        return True
    return True


def windows_ffmpeg_native_build_enabled(builder) -> bool:
    return builder.platform.os == "windows" and ffmpeg_enabled(builder) and builder._windows_msys2_detected()
