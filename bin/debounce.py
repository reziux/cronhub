"""Content-hash only debounce for cronctl registry writes.

Prevents the `sync_*` feedback loop from producing hundreds of identical
writes when one syncer reads, computes identical content, and rewrites. We
compare the content hash before writing against the hash of what's already
on disk — if they match, skip the write (idempotent).

This deliberately does NOT use time-based cooldown: legitimate enable/disable
flows toggle state each call, so a time-based cooldown breaks the test
suite. Idempotent no-change skips are the right primitive.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path

CRONHUB = Path(os.environ.get("CRONHUB", "/mnt/data/cronhub"))
LOCK = CRONHUB / "locks" / "registry.lock"
DEBOUNCE_FILE = LOCK.with_suffix(".debounce")
_deb_tmp = DEBOUNCE_FILE.with_suffix(".tmp")


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def hash_str(s: str) -> str:
    return hash_bytes(s.encode("utf-8"))


def _read_state() -> str:
    if not DEBOUNCE_FILE.exists():
        return ""
    try:
        return DEBOUNCE_FILE.read_text().strip()
    except Exception:
        return ""


def _write_state(h: str) -> None:
    DEBOUNCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _deb_tmp.write_text(h)
    os.replace(_deb_tmp, DEBOUNCE_FILE)


def should_write(content_hash: str, force: bool = False) -> tuple[bool, str]:
    """Return (allow, reason).

    `force=True` bypasses — operator CLI callers may want this when toggling
    many jobs in sequence.
    """
    if force:
        return True, "force"
    last = _read_state()
    if last == content_hash:
        return False, "no-change (idempotent skip — registry.yaml identical to on-disk)"
    return True, "ok"


def mark_written(content_hash: str) -> None:
    _write_state(content_hash)

