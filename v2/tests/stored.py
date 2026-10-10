"""Helpers for tests that look at or edit stored snapshot rows directly."""

from __future__ import annotations

import json

from officesentry import store


def unpack(db, kind: str | None = None) -> None:
    """Turn packed objects back into one row of JSON text per object, as stored before packing, so a test can
    edit rows with SQL (``json_set``, delete by object id). Readers take both formats."""
    sql = "SELECT run_id, kind, object_id, data FROM objects WHERE typeof(data) = 'blob'"
    rows = db.execute(sql + (" AND kind = ?" if kind else ""), (kind,) if kind else ()).fetchall()
    for row in rows:
        objects = [json.loads(line) for line in store.pack_lines(row["data"])]
        db.execute(
            "DELETE FROM objects WHERE run_id = ? AND kind = ? AND object_id = ?",
            (row["run_id"], row["kind"], row["object_id"]),
        )
        db.executemany(
            "INSERT INTO objects (run_id, kind, object_id, data) VALUES (?, ?, ?, ?)",
            [(row["run_id"], row["kind"], object_id, json.dumps(data, sort_keys=True)) for object_id, data in objects],
        )
