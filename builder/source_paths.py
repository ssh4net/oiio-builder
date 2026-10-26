from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .config import Config
from .git_ops import resolve_repo_dir


SOURCE_PACKAGE_PATH = Path("share/cmake/OIIOBuilderSources/OIIOBuilderSourcesConfig.cmake")


def discover_source_paths(config: Config, resolved_paths: Mapping[str, Path]) -> dict[str, str]:
    """Locate existing configured source roots, independently of build selection."""
    sources: dict[str, str] = {}
    for repo in config.repos:
        path = resolved_paths.get(repo.name)
        if path is None:
            path = resolve_repo_dir(config.global_cfg.src_root, repo.dir, repo.dir_candidates)
        if path.is_dir():
            # Keep mapped drives and symlink spellings rather than resolving them
            # to UNC or physical paths. The registry describes this host's paths.
            sources[repo.name] = path.absolute().as_posix()
    return dict(sorted(sources.items()))


def is_source_registry_only_share(directory: Path) -> bool:
    """Recognize metadata-only share trees without hiding other prefix content."""
    package = SOURCE_PACKAGE_PATH.relative_to("share")
    try:
        if not (directory / package).is_file():
            return False
        allowed = {package, *package.parents}
        return all(path.relative_to(directory) in allowed for path in directory.rglob("*"))
    except OSError:
        return False


def _cmake_literal(value: str) -> str:
    delimiter = "="
    while f"]{delimiter}]" in value:
        delimiter += "="
    return f"[{delimiter}[{value}]{delimiter}]"


def source_registry_files(sources: Mapping[str, str]) -> dict[Path, str]:
    assignments = [
        "set(OIIOBuilderSources_REPOSITORIES "
        + " ".join(_cmake_literal(name) for name in sources) + ")",
    ]
    assignments.extend(
        f"set({_cmake_literal('OIIOBuilderSources_SOURCE_DIR_' + name)} {_cmake_literal(path)})"
        for name, path in sources.items()
    )
    template = (Path(__file__).parent / "cmake" / "OIIOBuilderSourcesConfig.cmake.in").read_text(encoding="utf-8")
    return {
        Path(".oiio-builder/source-paths.json"): json.dumps(
            {"schema": 1, "sources": dict(sources)}, indent=2, sort_keys=True,
        ) + "\n",
        SOURCE_PACKAGE_PATH: template.replace("@SOURCE_ASSIGNMENTS@", "\n".join(assignments)),
    }
