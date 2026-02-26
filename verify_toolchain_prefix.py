#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Iterable


_CXX_STDLIB_LIBCXX = "libc++"
_CXX_STDLIB_LIBSTDCXX = "libstdc++"
_CXX_STDLIB_MIXED = "mixed"
_CXX_STDLIB_UNKNOWN = "unknown"
_CXX_STDLIB_NOT_CPP = "not-cpp"

_COMPILER_CLANG = "clang"
_COMPILER_GCC = "gcc"
_COMPILER_MIXED = "mixed"
_COMPILER_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ArtifactProbe:
    path: Path
    kind: str  # archive, shared, executable, other
    stdlib: str
    compiler: str
    notes: tuple[str, ...] = ()


def _which(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if not candidate:
            continue
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _iter_output_lines(cmd: list[str], *, max_lines: int) -> Iterable[str]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    assert proc.stdout is not None
    lines_yielded = 0
    try:
        for raw_line in proc.stdout:
            yield raw_line.rstrip("\n")
            lines_yielded += 1
            if lines_yielded >= max_lines:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def _run_capture(cmd: list[str]) -> tuple[int, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def _detect_shared_needed_libs(path: Path, *, readelf: str | None, objdump: str | None) -> list[str]:
    if readelf:
        code, out = _run_capture([readelf, "-d", str(path)])
        if code == 0:
            return re.findall(r"Shared library: \\[(.*?)\\]", out)
    if objdump:
        code, out = _run_capture([objdump, "-p", str(path)])
        if code == 0:
            needed: list[str] = []
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("NEEDED"):
                    parts = stripped.split()
                    if len(parts) >= 2:
                        needed.append(parts[1])
            return needed
    return []


def _classify_stdlib_from_needed(needed: list[str]) -> str:
    libs = {name.strip() for name in needed if name and name.strip()}
    has_libcxx = any(name.startswith("libc++.so") for name in libs) or "libc++.so.1" in libs
    has_libstdcxx = any(name.startswith("libstdc++.so") for name in libs) or "libstdc++.so.6" in libs
    if has_libcxx and has_libstdcxx:
        return _CXX_STDLIB_MIXED
    if has_libcxx:
        return _CXX_STDLIB_LIBCXX
    if has_libstdcxx:
        return _CXX_STDLIB_LIBSTDCXX
    return _CXX_STDLIB_UNKNOWN


def _detect_stdlib_from_nm(path: Path, *, nm: str, max_lines: int) -> str:
    markers: set[str] = set()
    saw_any_std = False
    saw_any_cxx = False

    def scan(cmd: list[str]) -> None:
        nonlocal saw_any_std, saw_any_cxx, markers
        for line in _iter_output_lines(cmd, max_lines=max_lines):
            if "std::" in line:
                saw_any_std = True
                saw_any_cxx = True
            if "operator new" in line or "operator delete" in line:
                saw_any_cxx = True
            if "typeinfo for" in line or "vtable for" in line:
                saw_any_cxx = True
            if "std::__1::" in line:
                markers.add(_CXX_STDLIB_LIBCXX)
            if "std::__cxx11::" in line or "__gnu_cxx::" in line or "GLIBCXX_" in line:
                markers.add(_CXX_STDLIB_LIBSTDCXX)
            if len(markers) > 1:
                return

    # Prefer undefined symbols (usually much smaller output).
    scan([nm, "-u", "-C", str(path)])
    if not markers and not saw_any_cxx:
        # Fall back to scanning defined symbols (some archives are self-contained).
        scan([nm, "-C", str(path)])

    if len(markers) > 1:
        return _CXX_STDLIB_MIXED
    if len(markers) == 1:
        return next(iter(markers))
    if saw_any_std:
        # If we see std:: but no libc++ inline namespace, assume libstdc++.
        return _CXX_STDLIB_LIBSTDCXX
    return _CXX_STDLIB_NOT_CPP if not saw_any_cxx else _CXX_STDLIB_UNKNOWN


def _detect_compiler_from_comment(path: Path, *, objdump: str | None, readelf: str | None, strings: str | None) -> str:
    text = ""
    if readelf:
        # readelf typically doesn't handle archives, but works for ELF objects/shared libs/executables.
        code, out = _run_capture([readelf, "-p", ".comment", str(path)])
        if code == 0:
            text = out
    if not text and objdump:
        code, out = _run_capture([objdump, "-s", "-j", ".comment", str(path)])
        if code == 0:
            text = out
    if not text and strings:
        # Last resort: scan printable strings for compiler idents.
        code, out = _run_capture([strings, "-a", "-n", "10", str(path)])
        if code == 0:
            text = out

    lowered = text.lower()
    compact = lowered.replace("\n", "")
    hits: set[str] = set()
    if "clang version" in lowered or "clang version" in compact or "apple clang version" in lowered or "apple clang version" in compact:
        hits.add(_COMPILER_CLANG)
    if (
        re.search(r"\bgcc:\s*\(", lowered)
        or re.search(r"\bgcc version\b", lowered)
        or re.search(r"\bgcc:\s*\(", compact)
        or re.search(r"\bgcc version\b", compact)
    ):
        hits.add(_COMPILER_GCC)

    if len(hits) > 1:
        return _COMPILER_MIXED
    if len(hits) == 1:
        return next(iter(hits))
    return _COMPILER_UNKNOWN


def _artifact_kind(path: Path) -> str:
    name = path.name
    if name.endswith(".a"):
        return "archive"
    if name.endswith(".so") or ".so." in name:
        return "shared"
    if path.parent.name == "bin":
        return "executable"
    return "other"


def _iter_artifacts(prefix: Path) -> list[Path]:
    artifacts: list[Path] = []
    lib_dir = prefix / "lib"
    if lib_dir.is_dir():
        for entry in sorted(lib_dir.iterdir()):
            if entry.is_file() and (entry.name.endswith(".a") or entry.name.endswith(".so") or ".so." in entry.name):
                artifacts.append(entry)
    bin_dir = prefix / "bin"
    if bin_dir.is_dir():
        for entry in sorted(bin_dir.iterdir()):
            if entry.is_file():
                try:
                    mode = entry.stat().st_mode
                except OSError:
                    continue
                if mode & 0o111:
                    artifacts.append(entry)
    return artifacts


def _probe_prefix(prefix: Path, *, expect_stdlib: str | None, expect_compiler: str | None, strict: bool) -> int:
    nm = _which(["llvm-nm", "nm"])
    if not nm:
        print("[error] Missing tool: nm (or llvm-nm)", file=sys.stderr)
        return 2
    readelf = _which(["readelf", "llvm-readelf"])
    objdump = _which(["objdump", "llvm-objdump"])
    strings = _which(["strings"])

    artifacts = _iter_artifacts(prefix)
    if not artifacts:
        print(f"[warn] {prefix}: no artifacts found under lib/ or bin/")
        return 0

    probes: list[ArtifactProbe] = []
    for path in artifacts:
        kind = _artifact_kind(path)
        notes: list[str] = []
        if kind in {"shared", "executable"}:
            needed = _detect_shared_needed_libs(path, readelf=readelf, objdump=objdump)
            stdlib = _classify_stdlib_from_needed(needed)
            if needed:
                notes.append("needed=" + ",".join(sorted(set(needed))))
            if stdlib == _CXX_STDLIB_UNKNOWN:
                stdlib = _detect_stdlib_from_nm(path, nm=nm, max_lines=25_000)
        elif kind == "archive":
            stdlib = _detect_stdlib_from_nm(path, nm=nm, max_lines=25_000)
        else:
            stdlib = _CXX_STDLIB_UNKNOWN

        compiler = _detect_compiler_from_comment(path, objdump=objdump, readelf=readelf, strings=strings)
        probes.append(ArtifactProbe(path=path, kind=kind, stdlib=stdlib, compiler=compiler, notes=tuple(notes)))

    rel = lambda p: str(p.relative_to(prefix))
    width = max(len(rel(probe.path)) for probe in probes)
    print()
    print(f"=== Toolchain Probe: {prefix} ===")
    for probe in probes:
        line = f"{rel(probe.path):<{width}}  kind={probe.kind:<10} stdlib={probe.stdlib:<9} compiler={probe.compiler}"
        if probe.notes:
            line += "  " + " ".join(probe.notes)
        print(line)

    def _count(attr: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for probe in probes:
            value = getattr(probe, attr)
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    std_counts = _count("stdlib")
    comp_counts = _count("compiler")
    print()
    print("Summary:")
    print("  stdlib:   " + ", ".join(f"{k}={v}" for k, v in std_counts.items()))
    print("  compiler: " + ", ".join(f"{k}={v}" for k, v in comp_counts.items()))

    def _expect_violation(actual: str, expected: str | None) -> bool:
        if expected is None:
            return False
        if actual in {_CXX_STDLIB_NOT_CPP, _CXX_STDLIB_UNKNOWN, _COMPILER_UNKNOWN}:
            return strict
        return actual != expected

    failed = 0
    if expect_stdlib:
        mismatched = [probe for probe in probes if _expect_violation(probe.stdlib, expect_stdlib)]
        if mismatched:
            failed = 1
            print()
            print(f"[error] Expected stdlib={expect_stdlib}, but found:")
            for probe in mismatched:
                print(f"  {rel(probe.path)}: stdlib={probe.stdlib}")

    if expect_compiler:
        mismatched = [probe for probe in probes if _expect_violation(probe.compiler, expect_compiler)]
        if mismatched:
            failed = 1
            print()
            print(f"[error] Expected compiler={expect_compiler}, but found:")
            for probe in mismatched:
                print(f"  {rel(probe.path)}: compiler={probe.compiler}")

    return 2 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe a prefix to infer libc++ vs libstdc++ and clang vs gcc usage (best-effort).",
    )
    parser.add_argument("prefix", nargs="+", help="Install prefix path(s), e.g. developer/install/Release")
    parser.add_argument(
        "--expect-stdlib",
        choices=[_CXX_STDLIB_LIBCXX, _CXX_STDLIB_LIBSTDCXX],
        help="Fail if detected stdlib differs (unknown/not-cpp is allowed unless --strict).",
    )
    parser.add_argument(
        "--expect-compiler",
        choices=[_COMPILER_CLANG, _COMPILER_GCC],
        help="Fail if detected compiler differs (unknown is allowed unless --strict).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat unknown stdlib/compiler as failures when using --expect-*.",
    )
    args = parser.parse_args()

    status = 0
    for raw in args.prefix:
        prefix = Path(raw).expanduser().resolve()
        if not prefix.is_dir():
            print(f"[error] Not a directory: {prefix}", file=sys.stderr)
            return 2
        rc = _probe_prefix(prefix, expect_stdlib=args.expect_stdlib, expect_compiler=args.expect_compiler, strict=args.strict)
        status = max(status, rc)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
