from __future__ import annotations


def _dng_sdk_requested(builder) -> bool:
    return any(repo.name == "dng-sdk" for repo in builder.repos)


def cmake_args(builder, _ctx) -> list[str]:
    cfg = builder.config.global_cfg

    build_python = True
    build_wheel = True
    if builder.platform.os == "windows":
        wrappers_enabled, reason = builder._windows_python_wrappers_enabled()
        build_python = wrappers_enabled
        build_wheel = wrappers_enabled
        note_attr = "_openmeta_python_note_printed"
        if not wrappers_enabled and not getattr(builder, note_attr, False):
            if reason == "forced-off":
                print(
                    "[note] OpenMeta: OPENMETA_BUILD_PYTHON=OFF OPENMETA_BUILD_WHEEL=OFF (windows.python_wrappers=off)",
                    flush=True,
                )
            else:
                print(
                    "[note] OpenMeta: OPENMETA_BUILD_PYTHON=OFF OPENMETA_BUILD_WHEEL=OFF "
                    "(windows.python_wrappers=auto with static CRT). "
                    "Set windows.python_wrappers=on (or windows.msvc_runtime=dynamic) to enable bindings.",
                    flush=True,
                )
            setattr(builder, note_attr, True)

    build_shared = not cfg.static_default
    build_static = not build_shared
    use_libcxx = builder.platform.os in {"linux", "macos"} and bool(cfg.use_libcxx)

    return [
        f"-DOPENMETA_BUILD_STATIC={'ON' if build_static else 'OFF'}",
        f"-DOPENMETA_BUILD_SHARED={'ON' if build_shared else 'OFF'}",
        f"-DOPENMETA_BUILD_PYTHON={'ON' if build_python else 'OFF'}",
        f"-DOPENMETA_BUILD_WHEEL={'ON' if build_wheel else 'OFF'}",
        f"-DOPENMETA_WITH_DNG_SDK_ADAPTER={'ON' if _dng_sdk_requested(builder) else 'OFF'}",
        f"-DOPENMETA_USE_LIBCXX={'ON' if use_libcxx else 'OFF'}",
    ]
