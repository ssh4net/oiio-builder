from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path


def compute_stamp(payload: dict) -> str:
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    return sha256(data).hexdigest()


def read_stamp(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_stamp(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
