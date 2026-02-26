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
