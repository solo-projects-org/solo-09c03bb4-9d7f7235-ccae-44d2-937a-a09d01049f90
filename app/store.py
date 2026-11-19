"""Persistence layer: SQLite via the standard library.

A single connection guarded by a lock is enough for this service and keeps
the transactional story simple.  WAL mode plus ``BEGIN IMMEDIATE`` gives
serialised writers and lets the compute path read a consistent snapshot.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .matching import Layer, align

SCHEMA = """
CREATE TABLE IF NOT EXISTS revisions (
    hole_id      TEXT    NOT NULL,
    revision     INTEGER NOT NULL,
    layers_json  TEXT    NOT NULL,
    layer_count  INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    PRIMARY KEY (hole_id, revision)
);

CREATE TABLE IF NOT EXISTS results (
    result_id      TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    left_hole_id   TEXT NOT NULL,
    right_hole_id  TEXT NOT NULL,
    left_revision  INTEGER NOT NULL,
    right_revision INTEGER NOT NULL,
    skip_penalty   INTEGER NOT NULL,
    payload_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    invalidated_at TEXT
);

CREATE INDEX IF NOT EXISTS results_by_left
    ON results (left_hole_id, left_revision, invalidated_at);
CREATE INDEX IF NOT EXISTS results_by_right
    ON results (right_hole_id, right_revision, invalidated_at);
"""


class ConflictError(Exception):
    """A revision number lower than or equal to one already stored."""


class NotFoundError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    # ------------------------------------------------------------------ upload

    def upsert_revision(
        self, hole_id: str, revision: int, layers: list[Layer]
    ) -> dict[str, Any]:
        layers_json = json.dumps(
            [
                {
                    "layer_no": layer.layer_no,
                    "top_cm": layer.top_cm,
                    "bottom_cm": layer.bottom_cm,
                    "lithology": layer.lithology,
                }
                for layer in layers
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        now = _now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT MAX(revision) AS latest FROM revisions WHERE hole_id = ?",
                (hole_id,),
            ).fetchone()
            latest = row["latest"]
            if latest is not None and revision <= latest:
                raise ConflictError(
                    f"revision {revision} is not greater than current revision {latest}"
                )

            conn.execute(
                "INSERT INTO revisions (hole_id, revision, layers_json, layer_count, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (hole_id, revision, layers_json, len(layers), now),
            )
            # A newly accepted revision makes every published result that used
            # an older revision of this hole stale immediately.  Results that
            # mention this exact revision cannot exist yet (revision numbers
            # are consumed once), so the revision comparison is unambiguous.
            cursor = conn.execute(
                "UPDATE results SET invalidated_at = ? "
                "WHERE invalidated_at IS NULL AND ("
                "  (left_hole_id = ? AND left_revision < ?) OR "
                "  (right_hole_id = ? AND right_revision < ?)"
                ")",
                (now, hole_id, revision, hole_id, revision),
            )
            return {
                "hole_id": hole_id,
                "revision": revision,
                "layer_count": len(layers),
                "superseded_results": cursor.rowcount,
            }

    def _load_layers(self, conn: sqlite3.Connection, hole_id: str, revision: int) -> list[Layer]:
        row = conn.execute(
            "SELECT layers_json FROM revisions WHERE hole_id = ? AND revision = ?",
            (hole_id, revision),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"hole {hole_id!r} revision {revision} not found")
        raw = json.loads(row["layers_json"])
        layers = [
            Layer(
                layer_no=item["layer_no"],
                top_cm=item["top_cm"],
                bottom_cm=item["bottom_cm"],
                lithology=item["lithology"],
            )
            for item in raw
        ]
        layers.sort(key=lambda layer: (layer.top_cm, layer.bottom_cm))
        return layers

    def layers_for(self, hole_id: str, revision: int) -> list[Layer]:
        with self._read() as conn:
            return self._load_layers(conn, hole_id, revision)

    def latest_revision(self, hole_id: str) -> int | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT MAX(revision) AS latest FROM revisions WHERE hole_id = ?",
                (hole_id,),
            ).fetchone()
            return row["latest"]

    # ----------------------------------------------------------------- compute

    def compute(
        self,
        left_hole_id: str,
        right_hole_id: str,
        left_revision: int,
        right_revision: int,
        skip_penalty: int,
        confirmed: list[tuple[int, int]],
    ) -> dict[str, Any]:
        with self._read() as conn:
            left = self._load_layers(conn, left_hole_id, left_revision)
            right = self._load_layers(conn, right_hole_id, right_revision)
            latest_left = conn.execute(
                "SELECT MAX(revision) AS latest FROM revisions WHERE hole_id = ?",
                (left_hole_id,),
            ).fetchone()["latest"]
            latest_right = conn.execute(
                "SELECT MAX(revision) AS latest FROM revisions WHERE hole_id = ?",
                (right_hole_id,),
            ).fetchone()["latest"]
        self._check_confirmed_exist(left, right, confirmed)
        if latest_left != left_revision or latest_right != right_revision:
            # Refuse early rather than compute against a superseded log; the
            # publication check below is still needed for the interleaving
            # window.
            raise ConflictError(
                "requested revisions are not current: "
                f"left latest={latest_left}, right latest={latest_right}"
            )

        # Released the lock while the (possibly slow) DP runs; currency is
        # re-checked under the publication transaction below.
        outcome = align(left, right, skip_penalty, confirmed)
        confirmed_set = set(confirmed)

        matches = [
            {
                "left_layer_no": left[i].layer_no,
                "right_layer_no": right[j].layer_no,
                "thickness_difference_cm": abs(
                    left[i].thickness_cm - right[j].thickness_cm
                ),
                "confirmed": (left[i].layer_no, right[j].layer_no) in confirmed_set,
            }
            for i, j in outcome.pairs
        ]

        result_id = str(uuid.uuid4())
        created_at = _now()
        payload = {
            "result_id": result_id,
            "status": outcome.status,
            "left_hole_id": left_hole_id,
            "right_hole_id": right_hole_id,
            "left_revision": left_revision,
            "right_revision": right_revision,
            "skip_penalty": skip_penalty,
            "matches": matches if outcome.status == "aligned" else [],
            "unmatched_left_layer_nos": (
                [left[i].layer_no for i in outcome.unmatched_left]
                if outcome.status == "aligned"
                else None
            ),
            "unmatched_right_layer_nos": (
                [right[j].layer_no for j in outcome.unmatched_right]
                if outcome.status == "aligned"
                else None
            ),
            "total_cost": outcome.total_cost,
            "review_reasons": outcome.reasons,
            "created_at": created_at,
            # Freshly published by this call, so it is current.
            "is_current": True,
            "invalidated_at": None,
        }

        # Currency check and publication share one exclusive transaction: a
        # revision accepted while the algorithm runs either lands before this
        # check (we refuse to publish, 409) or lands after the insert (its
        # invalidation UPDATE then marks this row).  Either way a superseded
        # revision's alignment is never stored as current.
        with self._transaction() as tx:
            latest_left = tx.execute(
                "SELECT MAX(revision) AS latest FROM revisions WHERE hole_id = ?",
                (left_hole_id,),
            ).fetchone()["latest"]
            latest_right = tx.execute(
                "SELECT MAX(revision) AS latest FROM revisions WHERE hole_id = ?",
                (right_hole_id,),
            ).fetchone()["latest"]
            if latest_left != left_revision or latest_right != right_revision:
                raise ConflictError(
                    "revisions changed while computing: "
                    f"left latest={latest_left}, right latest={latest_right}"
                )
            tx.execute(
                "INSERT INTO results (result_id, status, left_hole_id, right_hole_id,"
                " left_revision, right_revision, skip_penalty, payload_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result_id,
                    outcome.status,
                    left_hole_id,
                    right_hole_id,
                    left_revision,
                    right_revision,
                    skip_penalty,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                ),
            )
        return payload

    @staticmethod
    def _check_confirmed_exist(
        left: list[Layer], right: list[Layer], confirmed: list[tuple[int, int]]
    ) -> None:
        left_nos = {layer.layer_no for layer in left}
        right_nos = {layer.layer_no for layer in right}
        for left_no, right_no in confirmed:
            if left_no not in left_nos:
                raise NotFoundError(f"left layer {left_no} not found in requested revision")
            if right_no not in right_nos:
                raise NotFoundError(f"right layer {right_no} not found in requested revision")

    # ------------------------------------------------------------------- query

    def get_result(self, result_id: str) -> dict[str, Any]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT payload_json, invalidated_at FROM results WHERE result_id = ?",
                (result_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"result {result_id!r} not found")
            payload = json.loads(row["payload_json"])
            payload["invalidated_at"] = row["invalidated_at"]
            payload["is_current"] = row["invalidated_at"] is None
            return payload

    def list_results(
        self,
        hole_id: str | None = None,
        current_only: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if hole_id is not None:
            clauses.append("(left_hole_id = ? OR right_hole_id = ?)")
            params.extend([hole_id, hole_id])
        if current_only:
            clauses.append("invalidated_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT payload_json, invalidated_at FROM results "
                f"{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        out = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["invalidated_at"] = row["invalidated_at"]
            payload["is_current"] = row["invalidated_at"] is None
            out.append(payload)
        return out
