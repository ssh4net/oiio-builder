from __future__ import annotations

import re

STAMP_REVISION = "2"


def patch_source(builder, src_dir) -> None:
    if builder.platform.os != "windows":
        return
    cmake_file = src_dir / "src" / "wrappers" / "python" / "CMakeLists.txt"
    if not cmake_file.exists():
        return
    text = cmake_file.read_text(encoding="utf-8")
    begin = "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_BEGIN"
    end = "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_END"
    replacement = (
        "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_BEGIN\n"
        "target_link_libraries (PyOpenEXR PRIVATE OpenEXR::OpenEXR pybind11::headers)\n"
        "if(TARGET Python3::Module)\n"
        "  target_link_libraries (PyOpenEXR PRIVATE Python3::Module)\n"
        "elseif(TARGET Python3::Python)\n"
        "  target_link_libraries (PyOpenEXR PRIVATE Python3::Python)\n"
        "else()\n"
        "  target_link_libraries (PyOpenEXR PRIVATE ${Python3_LIBRARIES})\n"
        "endif()\n"
        "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_END"
    )
    if begin in text and end in text:
        start = text.index(begin)
        stop = text.index(end, start) + len(end)
        text = text[:start] + replacement + text[stop:]
    else:
        pattern = r'target_link_libraries\s*\(\s*PyOpenEXR\s+PRIVATE\s+"?\$\{Python3_LIBRARIES\}"?\s+OpenEXR::OpenEXR\s+pybind11::headers\s*\)'
        if re.search(pattern, text):
            text = re.sub(pattern, replacement, text, count=1)
    cmake_file.write_text(text, encoding="utf-8")
