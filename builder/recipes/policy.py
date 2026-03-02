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
    if not bool(getattr(cfg, "build_cpython", True)):
        return False
    if builder.platform.os == "windows":
        return True
    return bool(getattr(cfg, "cpython_ref", None))
