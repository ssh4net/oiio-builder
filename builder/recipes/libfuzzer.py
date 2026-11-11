from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

VERSION = "20.1.8"
ARCHIVE_NAME = f"compiler-rt-{VERSION}.src.tar.xz"
ARCHIVE_ROOT = f"compiler-rt-{VERSION}.src"
URL = f"https://github.com/llvm/llvm-project/releases/download/llvmorg-{VERSION}/{ARCHIVE_NAME}"
SHA256 = "15277402f6fd63397c0917a5c7171cda82d16d226094b828c1ed0f58f73b9c69"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MARKER = ".oiio-builder-libfuzzer-source.json"
WRAPPER = Path(__file__).parent / "wrappers" / "libfuzzer"
STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_libfuzzer)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify_archive(path: Path) -> None:
    if _sha256(path) != SHA256:
        raise RuntimeError(f"libfuzzer: SHA256 mismatch for {path}; expected {SHA256}")


def _download_archive(path: Path) -> None:
    remaining = MAX_ARCHIVE_BYTES
    with urllib.request.urlopen(URL, timeout=60) as response, path.open("wb") as output:
        while chunk := response.read(min(1024 * 1024, remaining + 1)):
            if len(chunk) > remaining:
                raise RuntimeError("libfuzzer: archive download exceeds the size limit")
            output.write(chunk)
            remaining -= len(chunk)


def _extract_archive(archive: Path, destination: Path) -> None:
    # Validate the complete member list before writing; the pinned archive has no links.
    with tarfile.open(archive, "r:xz") as source:
        members = source.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or not name.parts or name.parts[0] != ARCHIVE_ROOT
                    or ".." in name.parts or "\\" in member.name or ":" in member.name
                    or not (member.isdir() or member.isfile())):
                raise RuntimeError(f"libfuzzer: unsafe archive member: {member.name}")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)


def sync_source(builder, repo, path: Path) -> bool:
    if (repo.url, repo.ref, repo.source_subdir) != (URL, VERSION, ".oiio-builder-cmake"):
        raise RuntimeError("libfuzzer: source overrides require a reviewed recipe version/checksum update")
    identity = {"version": VERSION, "sha256": SHA256, "url": URL}
    if path.exists():
        marker = path / MARKER
        if not marker.is_file() or json.loads(marker.read_text()) != identity:
            raise RuntimeError(f"libfuzzer: preserving unmanaged or different-version source at {path}; "
                               "select an empty repo directory")
        for relative in ("lib/fuzzer/FuzzerMain.cpp", "include/fuzzer/FuzzedDataProvider.h", "LICENSE.TXT"):
            if not (path / relative).is_file():
                raise RuntimeError(f"libfuzzer: incomplete source at {path}: missing {relative}")
        return True
    if builder.dry_run:
        print(f"[libfuzzer] acquire {URL}, verify SHA256 {SHA256}, extract to {path}", flush=True)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    supplied = builder._source_prep_env().get("LIBFUZZER_SOURCE_ARCHIVE")
    with tempfile.TemporaryDirectory(prefix=".libfuzzer-", dir=path.parent) as temporary:
        staging = Path(temporary)
        archive = Path(supplied).expanduser() if supplied else staging / ARCHIVE_NAME
        if not supplied:
            print(f"[libfuzzer] download {URL}", flush=True)
            _download_archive(archive)
        _verify_archive(archive)
        _extract_archive(archive, staging / "unpacked")
        extracted = staging / "unpacked" / ARCHIVE_ROOT
        (extracted / MARKER).write_text(json.dumps(identity, indent=2) + "\n")
        extracted.rename(path)
    return True


def patch_source(builder, path: Path) -> None:
    if builder.dry_run:
        return
    destination = path / ".oiio-builder-cmake"
    destination.mkdir(exist_ok=True)
    for source in sorted(WRAPPER.iterdir()):
        target = destination / source.name
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            shutil.copyfile(source, target)


def stamp_payload(_builder, _repo, ctx, payload: dict) -> None:
    root = ctx.src_dir.parent
    digest = hashlib.sha256()
    files = list(WRAPPER.iterdir()) + list((root / "lib/fuzzer").glob("*.*"))
    files += list((root / "include").rglob("*.h")) + [root / "LICENSE.TXT"]
    for path in sorted(files):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    payload["libfuzzer"] = {"version": VERSION, "archive_sha256": SHA256,
                           "source_and_wrapper_sha256": digest.hexdigest()}
