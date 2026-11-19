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
from .samples import LIES_IN_GAP, locate_sample, reconcile_placements

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

CREATE TABLE IF NOT EXISTS samples (
    sample_no      TEXT    PRIMARY KEY,
    hole_id        TEXT    NOT NULL,
    top_cm         INTEGER NOT NULL,
    bottom_cm      INTEGER NOT NULL,
    lithology      TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    -- The registration revision is a record only (kept after withdrawal, so
    -- the lifecycle is auditable).  layer_no always describes the current
    -- revision: every accepted revision re-points active samples atomically.
    registered_revision INTEGER,
    layer_no       INTEGER,
    registered_at  TEXT    NOT NULL,
    withdrawn_at   TEXT,
    withdrawn_revision INTEGER
);

CREATE INDEX IF NOT EXISTS samples_by_hole_status
    ON samples (hole_id, status);
"""


class ConflictError(Exception):
    """A revision number lower than or equal to one already stored."""


class NotFoundError(Exception):
    pass


class SampleRejectedError(Exception):
    """A sample cannot be registered against the hole's current revision."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class SampleConflictError(ConflictError):
    """The sample number already has an active registration."""

    def __init__(self, message: str, active_status: str) -> None:
        super().__init__(message)
        self.active_status = active_status


class RevisionRejectedError(ConflictError):
    """A lockstep revision would invalidate one or more active samples.

    ``conflicts`` lists ``{"sample_no", "reason"}`` for every active sample of
    the hole that the candidate revision would not accept.  Nothing is written
    when this is raised: the previous revision and all sample states survive.
    """

    def __init__(self, message: str, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.conflicts = conflicts


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

    @staticmethod
    def _layers_json(layers: list[Layer]) -> str:
        return json.dumps(
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

    @staticmethod
    def _insert_revision(
        conn: sqlite3.Connection,
        hole_id: str,
        revision: int,
        layers_json: str,
        layer_count: int,
        created_at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO revisions (hole_id, revision, layers_json, layer_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (hole_id, revision, layers_json, layer_count, created_at),
        )

    def upsert_revision(
        self,
        hole_id: str,
        revision: int,
        layers: list[Layer],
        *,
        invalidate_alignment_results: bool = True,
    ) -> dict[str, Any]:
        """Accept a complete revision for one hole.

        Before anything is written, every active sample of the hole is checked
        against the new layer set.  If any sample would no longer sit inside a
        single layer of its labelled lithology the whole upload is rejected
        with ``RevisionRejectedError`` and the previous revision plus every
        sample state are left untouched.

        ``invalidate_alignment_results`` defaults to true so this path keeps
        its alignment semantics.  Nothing passes ``False`` today; it stays
        explicit so a future path that re-versions a log without changing its
        geometry can avoid invalidating alignments that are still correct.
        """

        layers_json = self._layers_json(layers)
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

            conflicts = self._sample_conflicts(conn, hole_id, layers)
            if conflicts:
                raise RevisionRejectedError(
                    "revision would leave active samples outside a single "
                    "same-lithology layer",
                    conflicts,
                )

            self._insert_revision(
                conn, hole_id, revision, layers_json, len(layers), now
            )
            # Each surviving sample is re-pointed at its layer in this revision
            # in the same transaction as the insert, so a query can never see
            # the new revision paired with an old layer number.  The original
            # registration revision is kept for audit; the layer number always
            # belongs to the current revision.
            active_rows = conn.execute(
                "SELECT sample_no, top_cm, bottom_cm, lithology FROM samples "
                "WHERE hole_id = ? AND status = 'active' ORDER BY sample_no",
                (hole_id,),
            ).fetchall()
            active = [dict(row) for row in active_rows]
            reconcile_placements(layers, active)
            conn.executemany(
                "UPDATE samples SET layer_no = ? WHERE sample_no = ?",
                [(item["layer_no"], item["sample_no"]) for item in active],
            )
            if invalidate_alignment_results:
                # A newly accepted revision makes every published result that
                # used an older revision of this hole stale immediately.
                # Results that mention this exact revision cannot exist yet
                # (revision numbers are consumed once), so the comparison is
                # unambiguous.
                cursor = conn.execute(
                    "UPDATE results SET invalidated_at = ? "
                    "WHERE invalidated_at IS NULL AND ("
                    "  (left_hole_id = ? AND left_revision < ?) OR "
                    "  (right_hole_id = ? AND right_revision < ?)"
                    ")",
                    (now, hole_id, revision, hole_id, revision),
                )
                superseded = cursor.rowcount
            else:
                superseded = 0
            active_samples = conn.execute(
                "SELECT COUNT(*) AS n FROM samples WHERE hole_id = ? AND status = 'active'",
                (hole_id,),
            ).fetchone()["n"]
            return {
                "hole_id": hole_id,
                "revision": revision,
                "layer_count": len(layers),
                "superseded_results": superseded,
                # Active samples were all re-checked against this revision and
                # remain valid; their recorded alignment targets moved with the
                # revision even when their layer numbers changed.
                "active_samples": active_samples,
                "sample_conflicts": 0,
            }

    @staticmethod
    def _sample_conflicts(
        conn: sqlite3.Connection, hole_id: str, layers: list[Layer]
    ) -> list[dict[str, Any]]:
        """Active samples of ``hole_id`` that ``layers`` would not accept."""

        rows = conn.execute(
            "SELECT sample_no, top_cm, bottom_cm, lithology FROM samples "
            "WHERE hole_id = ? AND status = 'active' ORDER BY sample_no",
            (hole_id,),
        ).fetchall()
        conflicts: list[dict[str, Any]] = []
        for row in rows:
            _, reason = locate_sample(
                layers, row["top_cm"], row["bottom_cm"], row["lithology"]
            )
            if reason is not None:
                conflicts.append({"sample_no": row["sample_no"], "reason": reason})
        return conflicts

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

    # ----------------------------------------------------------------- samples

    def _load_current(
        self, conn: sqlite3.Connection, hole_id: str
    ) -> tuple[int, list[Layer]]:
        row = conn.execute(
            "SELECT MAX(revision) AS latest FROM revisions WHERE hole_id = ?",
            (hole_id,),
        ).fetchone()
        latest = row["latest"]
        if latest is None:
            raise NotFoundError(f"hole {hole_id!r} has no revisions")
        return latest, self._load_layers(conn, hole_id, latest)

    @staticmethod
    def _sample_row(row: sqlite3.Row, current_revision: int | None) -> dict[str, Any]:
        # A withdrawn registration no longer has a place in the current log, so
        # the response reports no layer rather than a historical one.
        layer_no = row["layer_no"] if row["status"] == "active" else None
        return {
            "sample_no": row["sample_no"],
            "hole_id": row["hole_id"],
            "top_cm": row["top_cm"],
            "bottom_cm": row["bottom_cm"],
            "lithology": row["lithology"],
            "status": row["status"],
            "layer_no": layer_no,
            "registered_revision": row["registered_revision"],
            "current_revision": current_revision,
            "registered_at": row["registered_at"],
            "withdrawn_at": row["withdrawn_at"],
            "withdrawn_revision": row["withdrawn_revision"],
        }

    def _sample_out(
        self, conn: sqlite3.Connection, sample_no: str
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT s.*, "
            "  (SELECT MAX(revision) FROM revisions r WHERE r.hole_id = s.hole_id) "
            "  AS current_revision "
            "FROM samples s WHERE s.sample_no = ?",
            (sample_no,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"sample {sample_no!r} not found")
        return self._sample_row(row, row["current_revision"])

    def register_sample(
        self,
        sample_no: str,
        hole_id: str,
        top_cm: int,
        bottom_cm: int,
        lithology: str,
    ) -> dict[str, Any]:
        """Register one pending-inspection sample against the current log.

        The whole interval must sit inside a single layer of the hole's current
        revision with the same lithology as the label.  A number that already
        has an active registration must be withdrawn first; a withdrawn number
        may be registered again with new depths or label.

        Read, place and insert happen under one ``BEGIN IMMEDIATE`` transaction,
        so a revision accepted concurrently either lands before (this call sees
        it) or after (that revision re-checks this sample in its own
        transaction).  There is no window in which a stored active sample is
        checked against a log that is no longer current.
        """

        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT status FROM samples WHERE sample_no = ?", (sample_no,)
            ).fetchone()
            if existing is not None and existing["status"] == "active":
                raise SampleConflictError(
                    f"sample {sample_no!r} is already registered; withdraw it first",
                    active_status="active",
                )

            latest, layers = self._load_current(conn, hole_id)
            layer, reason = locate_sample(layers, top_cm, bottom_cm, lithology)
            if layer is None:
                raise SampleRejectedError(
                    f"sample {sample_no!r} does not fit the current log of "
                    f"{hole_id}: {reason}",
                    reason or LIES_IN_GAP,
                )

            now = _now()
            if existing is None:
                conn.execute(
                    "INSERT INTO samples (sample_no, hole_id, top_cm, bottom_cm, "
                    "lithology, status, registered_revision, layer_no, registered_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        sample_no,
                        hole_id,
                        top_cm,
                        bottom_cm,
                        lithology,
                        latest,
                        layer.layer_no,
                        now,
                    ),
                )
            else:
                # Re-registration of a withdrawn number: replace the data but
                # reset its lifecycle.
                conn.execute(
                    "UPDATE samples SET hole_id = ?, top_cm = ?, bottom_cm = ?, "
                    "lithology = ?, status = 'active', registered_revision = ?, "
                    "layer_no = ?, registered_at = ?, withdrawn_at = NULL, "
                    "withdrawn_revision = NULL WHERE sample_no = ?",
                    (
                        hole_id,
                        top_cm,
                        bottom_cm,
                        lithology,
                        latest,
                        layer.layer_no,
                        now,
                        sample_no,
                    ),
                )
            return self._sample_out(conn, sample_no)

    def withdraw_sample(self, sample_no: str) -> dict[str, Any]:
        """Withdraw an active registration.  Withdrawing again raises 404."""

        with self._transaction() as conn:
            row = conn.execute(
                "SELECT hole_id, status FROM samples WHERE sample_no = ?",
                (sample_no,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"sample {sample_no!r} not found")
            if row["status"] != "active":
                raise NotFoundError(f"sample {sample_no!r} is not registered")
            latest = conn.execute(
                "SELECT MAX(revision) AS latest FROM revisions WHERE hole_id = ?",
                (row["hole_id"],),
            ).fetchone()["latest"]
            conn.execute(
                "UPDATE samples SET status = 'withdrawn', withdrawn_at = ?, "
                "withdrawn_revision = ? WHERE sample_no = ?",
                (_now(), latest, sample_no),
            )
            return self._sample_out(conn, sample_no)

    def get_sample(self, sample_no: str) -> dict[str, Any]:
        """Fetch one sample.

        ``layer_no`` and ``current_revision`` are read inside one statement so
        they always describe the same committed state: a concurrent revision
        can be either wholly before or wholly after this snapshot.
        """

        with self._read() as conn:
            row = conn.execute(
                "SELECT s.*, "
                "  (SELECT MAX(revision) FROM revisions r WHERE r.hole_id = s.hole_id) "
                "  AS current_revision "
                "FROM samples s WHERE s.sample_no = ?",
                (sample_no,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"sample {sample_no!r} not found")
            return self._sample_row(row, row["current_revision"])

    def list_samples(
        self,
        hole_id: str | None = None,
        status: str | None = "active",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if hole_id is not None:
            clauses.append("s.hole_id = ?")
            params.append(hole_id)
        if status is not None:
            clauses.append("s.status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT s.*, "
                "  (SELECT MAX(revision) FROM revisions r WHERE r.hole_id = s.hole_id) "
                "  AS current_revision "
                f"FROM samples s {where} "
                "ORDER BY s.registered_at DESC, s.sample_no LIMIT ?",
                params,
            ).fetchall()
            return [self._sample_row(row, row["current_revision"]) for row in rows]

    def verify_samples(self, hole_id: str | None = None) -> dict[str, Any]:
        """Re-check active samples against their hole's current revision.

        Used by the test-suite and as an operational safety net.  The database
        cannot get into a state where this reports conflicts, because
        registration and every sample-driven re-versioning re-check under the
        same transaction; a conflict here would mean a writer bypassed those
        checks.
        """

        clauses = ["status = 'active'"]
        params: list[Any] = []
        if hole_id is not None:
            clauses.append("hole_id = ?")
            params.append(hole_id)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM samples WHERE {' AND '.join(clauses)} ORDER BY sample_no",
                params,
            ).fetchall()
            cache: dict[str, tuple[int, list[Layer]]] = {}
            checked = 0
            conflicts: list[dict[str, Any]] = []
            for row in rows:
                hole = row["hole_id"]
                if hole not in cache:
                    cache[hole] = self._load_current(conn, hole)
                revision, layers = cache[hole]
                layer, reason = locate_sample(
                    layers, row["top_cm"], row["bottom_cm"], row["lithology"]
                )
                checked += 1
                if reason is not None:
                    conflicts.append({"sample_no": row["sample_no"], "reason": reason})
                elif layer is not None and layer.layer_no != row["layer_no"]:
                    # The recorded layer must describe the current revision;
                    # a mismatch means a writer bypassed the transactional
                    # re-check and is reported rather than repaired silently.
                    conflicts.append(
                        {"sample_no": row["sample_no"], "reason": "stale_placement"}
                    )
        return {
            "status": "ok" if not conflicts else "conflicts",
            "checked": checked,
            "conflicts": conflicts,
        }

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
