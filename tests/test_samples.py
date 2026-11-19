"""Tests for pending-inspection sample registration.

The invariant under test: every active registration sits inside exactly one
layer of its hole's *current* revision with matching lithology, no matter how
registrations, withdrawals and revision uploads interleave, and after a
restart.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client():
    directory = tempfile.mkdtemp()
    app = create_app(os.path.join(directory, "samples.sqlite3"))
    with TestClient(app) as test_client:
        yield test_client


def layer(no, top, bottom, lith):
    return {"layer_no": no, "top_cm": top, "bottom_cm": bottom, "lithology": lith}


def upload(client, hole, revision, layers):
    return client.post(
        "/api/holes/revisions",
        json={"hole_id": hole, "revision": revision, "layers": layers},
    )


def register(client, sample_no, hole, top, bottom, lith):
    return client.post(
        "/api/samples",
        json={
            "sample_no": sample_no,
            "hole_id": hole,
            "top_cm": top,
            "bottom_cm": bottom,
            "lithology": lith,
        },
    )


GOOD_LOG = [layer(1, 0, 100, "S"), layer(2, 100, 200, "M"), layer(3, 200, 300, "L")]


def test_register_and_query_returns_layer_and_revision(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    response = register(client, "S-1", "ZK-1", 10, 60, "S")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "active"
    assert body["layer_no"] == 1
    assert body["registered_revision"] == 1
    assert body["current_revision"] == 1

    fetched = client.get("/api/samples/S-1")
    assert fetched.status_code == 200
    # Placement and revision come from the committed snapshot.
    assert fetched.json()["layer_no"] == 1
    assert fetched.json()["current_revision"] == 1


def test_registration_rejections_are_specific(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201

    crossing = register(client, "S-1", "ZK-1", 50, 150, "S")
    assert crossing.status_code == 422
    assert crossing.json()["reason"] == "crosses_layer_boundary"

    gap = register(client, "S-2", "ZK-1", 305, 320, "L")
    assert gap.status_code == 422
    assert gap.json()["reason"] == "lies_in_gap"

    mismatch = register(client, "S-3", "ZK-1", 110, 150, "S")
    assert mismatch.status_code == 422
    assert mismatch.json()["reason"] == "lithology_mismatch"

    # None of the rejected registrations exists.
    assert client.get("/api/samples").json() == []
    for sample_no in ("S-1", "S-2", "S-3"):
        assert client.get(f"/api/samples/{sample_no}").status_code == 404


def test_interval_may_touch_one_boundary(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    # Ends exactly at 100: the boundary belongs to both neighbours, but the
    # interval is still wholly inside layer 1.
    assert register(client, "S-1", "ZK-1", 60, 100, "S").status_code == 201
    # Starts exactly at 100: wholly inside layer 2.
    assert register(client, "S-2", "ZK-1", 100, 140, "M").status_code == 201
    # A zero-length core run is not meaningful and is a schema-level 422.
    assert register(client, "S-3", "ZK-1", 250, 250, "L").status_code == 422


def test_unknown_hole_or_empty_log_cannot_register(client):
    response = register(client, "S-1", "ZK-9", 0, 10, "S")
    assert response.status_code == 404


def test_duplicate_number_requires_withdrawal(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    assert register(client, "S-1", "ZK-1", 10, 60, "S").status_code == 201

    duplicate = register(client, "S-1", "ZK-1", 10, 60, "S")
    assert duplicate.status_code == 409
    assert duplicate.json()["sample_status"] == "active"

    assert client.delete("/api/samples/S-1").status_code == 200
    assert client.delete("/api/samples/S-1").status_code == 404

    # After withdrawal the number is free again, with a different label.
    again = register(client, "S-1", "ZK-1", 110, 150, "M")
    assert again.status_code == 201, again.text
    assert again.json()["layer_no"] == 2
    assert again.json()["status"] == "active"


def test_revision_rejected_whole_when_a_sample_would_cross(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    assert register(client, "S-1", "ZK-1", 10, 60, "S").status_code == 201
    assert register(client, "S-2", "ZK-1", 110, 150, "M").status_code == 201
    assert register(client, "S-3", "ZK-1", 210, 250, "L").status_code == 201

    # This revision splits the interval of S-1 and relabels S-2's layer.
    bad = [
        layer(1, 0, 40, "S"),
        layer(2, 40, 100, "S"),
        layer(3, 100, 200, "S"),
        layer(4, 200, 300, "L"),
    ]
    response = upload(client, "ZK-1", 2, bad)
    assert response.status_code == 409, response.text
    conflicts = {item["sample_no"]: item["reason"] for item in response.json()["conflicts"]}
    # Every conflicting active sample is listed, not just the first.
    assert conflicts == {
        "S-1": "crosses_layer_boundary",
        "S-2": "lithology_mismatch",
    }

    # The old revision and every sample state are untouched.
    hole = client.get("/api/holes/ZK-1").json()
    assert hole["current_revision"] == 1
    assert hole["layers"] == GOOD_LOG
    assert client.get("/api/samples/S-1").json()["layer_no"] == 1
    assert client.get("/api/samples/verify").json() == {
        "status": "ok",
        "checked": 3,
        "conflicts": [],
    }


def test_withdrawn_samples_do_not_block_revision(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    assert register(client, "S-1", "ZK-1", 10, 60, "S").status_code == 201
    withdrawn = client.delete("/api/samples/S-1").json()
    assert withdrawn["status"] == "withdrawn"
    # A withdrawn registration has no place in the current log.
    assert withdrawn["layer_no"] is None

    splitting = [
        layer(1, 0, 30, "S"),
        layer(2, 30, 60, "S"),
        layer(3, 60, 100, "S"),
        layer(4, 100, 200, "M"),
        layer(5, 200, 300, "L"),
    ]
    assert upload(client, "ZK-1", 2, splitting).status_code == 201
    assert client.get("/api/holes/ZK-1").json()["current_revision"] == 2


def test_compatible_revision_renumbers_but_keeps_samples(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    assert register(client, "S-1", "ZK-1", 10, 60, "S").status_code == 201

    # Same geometry and lithology, different layer numbers.
    renumbered = [layer(7, 0, 100, "S"), layer(8, 100, 200, "M"), layer(9, 200, 300, "L")]
    response = upload(client, "ZK-1", 2, renumbered)
    assert response.status_code == 201
    assert response.json()["active_samples"] == 1

    sample = client.get("/api/samples/S-1").json()
    assert sample["status"] == "active"
    assert sample["layer_no"] == 7
    assert sample["registered_revision"] == 1
    assert sample["current_revision"] == 2
    assert client.get("/api/samples/verify").json()["status"] == "ok"


def test_revision_without_the_logged_gap_rejects_sample(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    assert register(client, "S-1", "ZK-1", 110, 150, "M").status_code == 201

    # Log a gap from 100 to 120 by moving the second layer down: S-1 now
    # straddles logged rock and a gap.
    gapped = [layer(1, 0, 100, "S"), layer(2, 120, 200, "M"), layer(3, 200, 300, "L")]
    response = upload(client, "ZK-1", 2, gapped)
    assert response.status_code == 409
    assert response.json()["conflicts"] == [
        {"sample_no": "S-1", "reason": "lies_in_gap"}
    ]
    assert client.get("/api/holes/ZK-1").json()["current_revision"] == 1


def test_rejected_revision_number_can_be_reused_after_withdrawal(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    assert register(client, "S-1", "ZK-1", 10, 60, "S").status_code == 201

    splitting = [
        layer(1, 0, 30, "S"),
        layer(2, 30, 60, "S"),
        layer(3, 60, 100, "S"),
        layer(4, 100, 200, "M"),
        layer(5, 200, 300, "L"),
    ]
    assert upload(client, "ZK-1", 2, splitting).status_code == 409

    # Revision 2 was never consumed by the rejected upload.
    assert client.delete("/api/samples/S-1").status_code == 200
    assert upload(client, "ZK-1", 2, splitting).status_code == 201


def test_samples_survive_restart(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    assert register(client, "S-1", "ZK-1", 10, 60, "S").status_code == 201
    db_path = client.app.state.store.path

    fresh = create_app(db_path)
    with TestClient(fresh) as restarted:
        sample = restarted.get("/api/samples/S-1").json()
        assert sample["layer_no"] == 1
        assert sample["current_revision"] == 1
        assert restarted.get("/api/samples/verify").json()["status"] == "ok"
        # A restart does not forget that the number is taken.
        assert register(restarted, "S-1", "ZK-1", 10, 60, "S").status_code == 409
        assert restarted.delete("/api/samples/S-1").status_code == 200


def test_list_filters(client):
    assert upload(client, "ZK-1", 1, GOOD_LOG).status_code == 201
    assert upload(client, "ZK-2", 1, GOOD_LOG).status_code == 201
    assert register(client, "S-1", "ZK-1", 10, 60, "S").status_code == 201
    assert register(client, "S-2", "ZK-2", 10, 60, "S").status_code == 201
    assert client.delete("/api/samples/S-2").status_code == 200

    active = client.get("/api/samples").json()
    assert [item["sample_no"] for item in active] == ["S-1"]
    by_hole = client.get("/api/samples", params={"hole_id": "ZK-2"}).json()
    assert by_hole == []
    everyone = client.get("/api/samples", params={"status": "all"}).json()
    assert {item["sample_no"]: item["status"] for item in everyone} == {
        "S-1": "active",
        "S-2": "withdrawn",
    }


def test_openapi_documents_sample_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/samples" in paths
    assert "/api/samples/{sample_no}" in paths
    assert "delete" in paths["/api/samples/{sample_no}"]
    assert "get" in paths["/api/samples/{sample_no}"]


def test_registration_during_revision_upload_is_serialised(client):
    """Both writers serialise on one BEGIN IMMEDIATE transaction.

    A registration that commits first is re-checked by the revision that
    follows; a revision that commits first makes the registration see the new
    log.  This test drives the two orders explicitly through the store, which
    is the unit that owns the transaction.
    """

    from app.matching import Layer
    from app.store import RevisionRejectedError

    store = client.app.state.store
    store.upsert_revision(
        "ZK-1", 1, [Layer(1, 0, 100, "S"), Layer(2, 100, 200, "M")]
    )
    # Order 1: sample first, then the revision that would strand it.
    store.register_sample("S-1", "ZK-1", 10, 60, "S")
    with pytest.raises(RevisionRejectedError):
        store.upsert_revision(
            "ZK-1",
            2,
            [Layer(1, 0, 40, "S"), Layer(2, 40, 100, "S"), Layer(3, 100, 200, "M")],
        )
    # Rejected whole: no revision 2, sample untouched.
    assert store.latest_revision("ZK-1") == 1
    assert store.get_sample("S-1")["layer_no"] == 1
    assert store.verify_samples()["status"] == "ok"

    # Order 2: revision first, then a registration judged against it.  The
    # sample that was active must still fit the new geometry.
    store.withdraw_sample("S-1")
    store.upsert_revision(
        "ZK-1",
        2,
        [Layer(1, 0, 40, "S"), Layer(2, 40, 100, "S"), Layer(3, 100, 200, "M")],
    )
    from app.store import SampleRejectedError

    with pytest.raises(SampleRejectedError):
        store.register_sample("S-2", "ZK-1", 10, 60, "S")
    accepted = store.register_sample("S-2", "ZK-1", 10, 40, "S")
    assert accepted["layer_no"] == 1
    assert accepted["current_revision"] == 2
    assert store.verify_samples()["status"] == "ok"


def test_many_interleaved_operations_keep_invariant(client):
    """Stress the interleaving rule across several holes and revisions.

    For every step the ``verify`` endpoint must confirm that every active
    sample still sits in one same-lithology layer of the current revision.
    """

    import random

    rng = random.Random(7)
    holes = ["ZK-A", "ZK-B"]
    for hole in holes:
        assert upload(client, hole, 1, GOOD_LOG).status_code == 201

    next_revision = {hole: 1 for hole in holes}
    alive: dict[str, tuple[str, int, int, str]] = {}
    counter = 0

    for _ in range(60):
        action = rng.choice(["register", "withdraw", "upload", "verify"])
        hole = rng.choice(holes)
        if action == "register":
            counter += 1
            no = f"S-{counter}"
            top = rng.randrange(0, 260, 10)
            bottom = top + rng.choice([10, 20, 30])
            lith = rng.choice(["S", "M", "L"])
            response = register(client, no, hole, top, bottom, lith)
            assert response.status_code in (201, 422), response.text
            if response.status_code == 201:
                alive[no] = (hole, top, bottom, lith)
        elif action == "withdraw" and alive:
            no = rng.choice(sorted(alive))
            assert client.delete(f"/api/samples/{no}").status_code == 200
            del alive[no]
        elif action == "upload":
            next_revision[hole] += 1
            # Half the time keep the geometry, otherwise push the top boundary
            # of the middle layer around.
            shift = rng.choice([0, 0, 10, -10])
            candidate = [
                layer(1, 0, 100, "S"),
                layer(2, 100, 200 + shift, "M"),
                layer(3, 200 + shift, 300, "L"),
            ]
            response = upload(client, hole, next_revision[hole], candidate)
            if response.status_code == 409:
                # Rejected whole: revision number was not consumed, the log did
                # not move, and no sample state changed.
                next_revision[hole] -= 1
                assert response.json()["conflicts"]
            else:
                assert response.status_code == 201, response.text
                # Any sample whose stored placement no longer applies must have
                # gone through a withdrawal; everything left active is valid.
                for no, (sample_hole, *_) in list(alive.items()):
                    if sample_hole != hole:
                        continue
                    fetched = client.get(f"/api/samples/{no}").json()
                    assert fetched["status"] == "active"
                    assert fetched["current_revision"] == next_revision[hole]
        else:
            assert client.get("/api/samples/verify").json()["status"] == "ok"

        assert client.get("/api/samples/verify").json()["status"] == "ok"
        for no, (sample_hole, *_rest) in alive.items():
            sample = client.get(f"/api/samples/{no}").json()
            assert sample["status"] == "active"
            assert sample["current_revision"] == next_revision[sample_hole]
