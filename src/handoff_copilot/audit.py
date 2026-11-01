"""An append-only audit log: one row per read, model call, draft and write.

Rows record who acted, which records, which field *names* came back and a hash of any
prompt. They never hold field values, and `append` refuses text that looks like an
email address or phone number. Each row's hash covers the one before it, and SQLite
triggers refuse UPDATE and DELETE, so an edit shows up in `verify`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from .privacy import contains_contact

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  records TEXT NOT NULL,
  fields TEXT NOT NULL,
  prompt_sha256 TEXT,
  detail TEXT NOT NULL,
  prev TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""

GENESIS = "0" * 64
COLUMNS = ("at", "actor", "action", "records", "fields", "prompt_sha256", "detail")


class AuditError(ValueError):
    pass


def _digest(prev: str, row: Mapping) -> str:
    payload = json.dumps({k: row[k] for k in COLUMNS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + payload).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path = ":memory:", clock=None) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        self.clock = clock or (lambda: datetime.now(UTC))

    def append(
        self,
        actor: str,
        action: str,
        *,
        records: Mapping[str, object] | None = None,
        fields: Iterable[str] = (),
        prompt_sha256: str | None = None,
        detail: str = "",
    ) -> str:
        row = {
            "at": self.clock().isoformat(),
            "actor": actor,
            "action": action,
            "records": json.dumps(dict(records or {}), sort_keys=True),
            "fields": json.dumps(sorted(fields)),
            "prompt_sha256": prompt_sha256,
            "detail": detail,
        }
        checked = ("actor", "action", "records", "fields", "detail")
        if any(contains_contact(str(row[k])) for k in checked):
            raise AuditError(f"refusing to log a contact value in {action!r}")
        last = self.db.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last[0] if last else GENESIS
        digest = _digest(prev, row)
        self.db.execute(
            "INSERT INTO audit (at, actor, action, records, fields, prompt_sha256, detail, "
            "prev, hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (*(row[k] for k in COLUMNS), prev, digest),
        )
        self.db.commit()
        return digest

    def rows(self) -> list[dict]:
        columns = ", ".join(COLUMNS)
        cur = self.db.execute(f"SELECT seq, {columns}, prev, hash FROM audit ORDER BY seq")  # noqa: S608
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]

    def verify(self) -> tuple[bool, int | None]:
        """(True, None) if the chain holds; otherwise (False, first bad seq)."""
        prev = GENESIS
        for row in self.rows():
            if row["prev"] != prev or _digest(prev, row) != row["hash"]:
                return False, row["seq"]
            prev = row["hash"]
        return True, None
