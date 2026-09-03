from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from builder.recipes import nativefiledialog_extended


_WAYLAND_PROTOCOL_LOOKUP = (
    "    set(NFD_WAYLAND_PROTOCOL_XDG_FOREIGN "
    "${CMAKE_CURRENT_SOURCE_DIR}/../3ps/wayland-protocols/unstable/xdg-foreign/"
    "xdg-foreign-unstable-v1.xml)\n"
)


class NativeFileDialogExtendedRecipeTests(unittest.TestCase):
    def _cmake_file(self, root: Path, contents: str) -> Path:
        cmake_file = root / "src" / "CMakeLists.txt"
        cmake_file.parent.mkdir()
        cmake_file.write_text(contents, encoding="utf-8")
        return cmake_file

    def test_skips_releases_without_wayland_protocol_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmake_file = self._cmake_file(
                Path(tmp),
                "set(TARGET_NAME nfd)\n"
                "# nativefiledialog-extended v1.3.0 has no Wayland backend\n",
            )

            nativefiledialog_extended._patch_wayland_protocol_fallback(Path(tmp))

            self.assertNotIn(
                "OIIO_BUILDER_WAYLAND_PROTOCOLS_FALLBACK",
                cmake_file.read_text(encoding="utf-8"),
            )

    def test_patches_and_repatches_wayland_protocol_lookup_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmake_file = self._cmake_file(Path(tmp), _WAYLAND_PROTOCOL_LOOKUP)

            nativefiledialog_extended._patch_wayland_protocol_fallback(Path(tmp))
            patched = cmake_file.read_text(encoding="utf-8")
            self.assertIn("OIIO_BUILDER_WAYLAND_PROTOCOLS_FALLBACK", patched)
            self.assertIn("pkg_get_variable", patched)

            nativefiledialog_extended._patch_wayland_protocol_fallback(Path(tmp))
            self.assertEqual(patched, cmake_file.read_text(encoding="utf-8"))

    def test_rejects_an_unrecognized_wayland_protocol_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._cmake_file(
                Path(tmp),
                "set(NFD_WAYLAND_PROTOCOL_XDG_FOREIGN /new/upstream/location.xml)\n",
            )

            with self.assertRaisesRegex(RuntimeError, "no longer matches upstream source"):
                nativefiledialog_extended._patch_wayland_protocol_fallback(Path(tmp))


if __name__ == "__main__":
    unittest.main()
