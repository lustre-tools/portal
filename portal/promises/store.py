"""JSON persistence for tracked changes. No domain logic lives here.

Layout under the promises directory:

    changes/<num>.json    harvest snapshot, follow-up candidates, AI results
    overrides/<num>.json  decisions made by hand: status, notes, linked
                          follow-ups, manual items. NEVER written by the
                          machine paths (harvest, classify, judge).
    archive/<num>.json    untracked changes (tracking again restores them)
    ledger.jsonl          one line per Claude call: when, what, what it cost

The web app, the scheduled refresh and the CLI can all write at once, so
every read-modify-write holds a per-file lock and lands via a temp file
and a rename; files keep the owner they had (the CLI runs as root, the
service as its own user).

The document schema is the one gerrit-followup used, so its data can be
imported as-is; see ``portal-promises import``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from portal.fsutil import owner_to_keep

SCHEMA_VERSION = 1

_lock = threading.Lock()


def _empty_change_doc(number: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "change_number": number,
        "added_at": None,
        "added_by": None,
        "harvest": None,
        "linked_changes": {},
        "candidates": {},
        "classifications": {},
        "adjudications": {},
        "spend": {},
    }


def _empty_overrides_doc() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "overrides": {},
        "manual_items": {},
        "next_manual_id": 1,
    }


class Store:
    """Reads tolerate missing or corrupt files; writes are atomic."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        for sub in ("", "changes", "overrides", "archive"):
            path = self.root / sub
            if path.is_dir():
                continue
            # Created by root (a CLI run), a directory would be root's and
            # the service could not write into it: give it the parent's owner.
            owner = owner_to_keep(str(path))
            path.mkdir(parents=True, exist_ok=True)
            if owner:
                with contextlib.suppress(OSError):
                    os.chown(path, *owner)

    # -- paths --------------------------------------------------------

    def _change_path(self, number: int) -> Path:
        return self.root / "changes" / f"{int(number)}.json"

    def _overrides_path(self, number: int) -> Path:
        return self.root / "overrides" / f"{int(number)}.json"

    def _archive_path(self, number: int) -> Path:
        return self.root / "archive" / f"{int(number)}.json"

    # -- primitives ---------------------------------------------------

    @contextlib.contextmanager
    def _file_lock(self, path: Path):
        lock_path = path.with_suffix(path.suffix + ".lock")
        owner = owner_to_keep(str(path))
        with open(lock_path, "w") as f:
            if owner:
                with contextlib.suppress(OSError):
                    os.fchown(f.fileno(), *owner)
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _load(self, path: Path, default: Any) -> Any:
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def _save_locked(self, path: Path, data: Any) -> None:
        owner = owner_to_keep(str(path))
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            if owner:
                with contextlib.suppress(OSError):
                    os.fchown(fd, *owner)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- change documents ---------------------------------------------

    def tracked_changes(self) -> list[int]:
        out = []
        for p in (self.root / "changes").glob("*.json"):
            try:
                out.append(int(p.stem))
            except ValueError:
                continue
        return sorted(out)

    def has_change(self, number: int) -> bool:
        return self._change_path(number).exists()

    def load_change(self, number: int) -> dict:
        doc = self._load(self._change_path(number), None)
        if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
            return _empty_change_doc(number)
        return doc

    def mutate_change(self, number: int, fn, create: bool = True) -> dict:
        """Read-modify-write under the locks; ``fn(doc)`` edits in place.

        ``create=False``: when the change is not tracked (the file is gone
        -- untracked while a job was still running), return the empty
        document WITHOUT writing, so a job cannot bring it back.
        """
        path = self._change_path(number)
        with _lock, self._file_lock(path):
            raw = self._load(path, None)
            doc = raw
            if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
                doc = _empty_change_doc(number)
            if raw is None and not create:
                return doc
            fn(doc)
            self._save_locked(path, doc)
            return doc

    def archive_change(self, number: int) -> bool:
        """Untrack: move the document to archive/ (decisions stay put)."""
        src = self._change_path(number)
        with _lock, self._file_lock(src):
            if not src.exists():
                return False
            os.replace(src, self._archive_path(number))
            return True

    def unarchive_change(self, number: int) -> bool:
        dst = self._change_path(number)
        with _lock, self._file_lock(dst):
            src = self._archive_path(number)
            if not src.exists():
                return False
            os.replace(src, dst)
            return True

    # -- decisions made by hand ---------------------------------------

    def load_overrides(self, number: int) -> dict:
        doc = self._load(self._overrides_path(number), None)
        if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
            return _empty_overrides_doc()
        for key, default in (("overrides", {}), ("manual_items", {}), ("next_manual_id", 1)):
            doc.setdefault(key, default)
        return doc

    def mutate_overrides(self, number: int, fn) -> dict:
        path = self._overrides_path(number)
        with _lock, self._file_lock(path):
            doc = self._load(path, None)
            if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
                doc = _empty_overrides_doc()
            for key, default in (("overrides", {}), ("manual_items", {}), ("next_manual_id", 1)):
                doc.setdefault(key, default)
            fn(doc)
            self._save_locked(path, doc)
            return doc

    # -- what Claude has cost -----------------------------------------

    def record_spend(self, number: int, stage: str, usage: dict) -> None:
        """Log one Claude call, and add it to the change's running total."""
        entry = {
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "change": int(number),
            "stage": stage,
            "usd": round(float(usage.get("usd") or 0.0), 6),
            "model": usage.get("model", ""),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "turns": usage.get("turns", 0),
        }
        ledger = self.root / "ledger.jsonl"
        with _lock, self._file_lock(ledger):
            owner = owner_to_keep(str(ledger))
            fd = os.open(ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                if owner:
                    with contextlib.suppress(OSError):
                        os.fchown(fd, *owner)
                os.write(fd, (json.dumps(entry) + "\n").encode())
            finally:
                os.close(fd)

        def add(doc: dict) -> None:
            spend = doc.setdefault("spend", {})
            spend[stage] = round(spend.get(stage, 0.0) + entry["usd"], 6)
            spend["calls"] = spend.get("calls", 0) + 1

        self.mutate_change(number, add, create=False)

    def spend_since(self, since_iso: str) -> float:
        """Total Claude spend in the ledger from ``since_iso`` on."""
        total = 0.0
        try:
            with open(self.root / "ledger.jsonl") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("at", "") >= since_iso:
                        total += float(e.get("usd") or 0.0)
        except FileNotFoundError:
            pass
        return total
