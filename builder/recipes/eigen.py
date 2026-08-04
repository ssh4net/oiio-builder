from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)


def post_install(builder, install_prefix: Path, _build_type: str) -> None:
    if builder.dry_run:
        return
    if builder.license_profile is None:
        return
    if "EIGEN_MPL2_ONLY" not in builder.license_profile.consumer_compile_definitions:
        return

    candidates = [
        install_prefix / "share" / "eigen3" / "cmake" / "Eigen3Config.cmake",
        install_prefix / "lib" / "cmake" / "eigen3" / "Eigen3Config.cmake",
        install_prefix / "lib64" / "cmake" / "eigen3" / "Eigen3Config.cmake",
    ]
    config_files = [path for path in candidates if path.exists()]
    if not config_files:
        expected = " or ".join(str(path) for path in candidates)
        raise RuntimeError(f"Eigen MPL2-only installed CMake config is missing: {expected}")

    marker = "# oiio-builder: nongpl-static Eigen interface definition"
    anchor = "endif (NOT TARGET Eigen3::Eigen)\n"
    for config_file in config_files:
        original = config_file.read_text(encoding="utf-8")
        if marker in original:
            continue
        if anchor not in original:
            raise RuntimeError(f"Eigen MPL2-only patch no longer matches installed config: {config_file}")
        replacement = (
            f"{anchor}\n"
            f"{marker}\n"
            "if (TARGET Eigen3::Eigen)\n"
            "  set_property(TARGET Eigen3::Eigen APPEND PROPERTY "
            "INTERFACE_COMPILE_DEFINITIONS EIGEN_MPL2_ONLY)\n"
            "endif ()\n"
        )
        config_file.write_text(original.replace(anchor, replacement, 1), encoding="utf-8")
