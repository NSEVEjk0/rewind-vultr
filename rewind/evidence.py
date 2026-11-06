from __future__ import annotations
import hashlib
import json
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def bundle(payload: dict) -> dict:
    return {"payload": payload, "sha256": sha256_bytes(canonical(payload))}
