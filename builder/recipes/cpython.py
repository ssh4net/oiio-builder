from __future__ import annotations


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    if not bool(getattr(cfg, "build_cpython", True)):
        return False
    # Windows: build CPython by default to provide import/static libs for
    # Debug/Release extension-module builds.
    if builder.platform.os == "windows":
        return True
    # Linux/macOS: keep system Python by default; build CPython only when
    # the user explicitly overrides the CPython ref/version.
    return bool(getattr(cfg, "cpython_ref", None))

