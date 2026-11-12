from __future__ import annotations

from .policy import imageio_enabled

STAMP_REVISION = "2"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_libuhdr)


def patch_source(builder, src_dir) -> None:
    if builder.dry_run:
        return
    cmake_lists = src_dir / "CMakeLists.txt"
    if not cmake_lists.exists():
        return

    original_text = cmake_lists.read_bytes().decode("utf-8")
    newline = "\r\n" if original_text.partition("\n")[0].endswith("\r") else "\n"
    text = original_text

    # UHDR_SANITIZE_OPTIONS also forces bundled dependencies and disables
    # installation. Recognize our externally instrumented build without those
    # side effects; only the fully static executable policy needs to change.
    asan_marker = "# oiio-builder: recognize the Linux ASAN configuration"
    anchor = "  elseif(DEFINED UHDR_SANITIZE_OPTIONS OR UHDR_BUILD_FUZZERS)"
    replacement = newline.join((
        f"  {asan_marker}",
        "  elseif(DEFINED UHDR_SANITIZE_OPTIONS OR UHDR_BUILD_FUZZERS",
        '      OR (CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_BUILD_TYPE STREQUAL "ASAN"))',
    ))
    if replacement not in text:
        if asan_marker in text or text.count(anchor) != 1:
            raise RuntimeError(f"libultrahdr ASAN static-link patch no longer matches {cmake_lists}")
        text = text.replace(anchor, replacement, 1)

    # libultrahdr forces CMP0091 to OLD, which prevents CMake from honoring
    # CMAKE_MSVC_RUNTIME_LIBRARY and leads to `/MD(d)` libraries being produced
    # even when the build uses `/MT(d)`.
    marker = "# oiio-builder: enforce CMP0091 NEW"
    if marker not in text and "cmake_policy(SET CMP0091 OLD)" in text:
        text = text.replace(
            "cmake_policy(SET CMP0091 OLD)",
            f"{marker}{newline}  cmake_policy(SET CMP0091 NEW)",
            1,
        )

    if text != original_text:
        cmake_lists.write_bytes(text.encode("utf-8"))
