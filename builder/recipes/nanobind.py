from __future__ import annotations


def enabled(_builder, _repo) -> bool:
    # Nanobind is used by OpenMeta in addition to the imageio stack.
    return True
