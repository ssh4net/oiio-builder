from __future__ import annotations

from pathlib import Path


STAMP_REVISION = "1"


def patch_source(_builder, src_dir: Path) -> None:
    # clang-cl defines _MSC_VER but doesn't provide MSVC's SVML intrinsic
    # `_mm_pow_ps()` (used by OCIO for a precise SIMD pow()).
    # Gate this path to MSVC-only by excluding clang.
    cpu_file = src_dir / "src" / "OpenColorIO" / "ops" / "fixedfunction" / "FixedFunctionOpCPU.cpp"
    if not cpu_file.exists():
        return

    original = cpu_file.read_text(encoding="utf-8", errors="replace")
    text = original

    needle = "#if (_MSC_VER >= 1920) && (OCIO_USE_AVX)"
    replacement = "#if (_MSC_VER >= 1920) && !defined(__clang__) && (OCIO_USE_AVX)"
    text = text.replace(needle, replacement)

    if text != original:
        cpu_file.write_text(text, encoding="utf-8")

