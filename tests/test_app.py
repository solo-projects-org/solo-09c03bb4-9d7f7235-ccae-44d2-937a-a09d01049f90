"""Tests for the alignment algorithm and the HTTP API."""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.matching import Layer, align


def L(no: int, top: int, bottom: int, lith: str) -> Layer:
    return Layer(no, top, bottom, lith)


# --------------------------------------------------------------------- engine


def test_simple_monotone_match():
    left = [L(1, 0, 100, "S"), L(2, 100, 220, "M"), L(3, 220, 300, "L")]
    right = [L(1, 0, 110, "S"), L(2, 110, 225, "M"), L(3, 225, 310, "L")]
    result = align(left, right, 20, [])
    assert result.status == "aligned"
    assert result.pairs == [(0, 0), (1, 1), (2, 2)]
    assert result.total_cost == 10 + 5 + 5


def test_lithology_mismatch_prevents_match():
    result = align([L(1, 0, 100, "S")], [L(1, 0, 100, "M")], 5, [])
    assert result.status == "pending_review"
    assert any("no_layers_matched" in reason for reason in result.reasons)


def test_skip_when_thickness_difference_exceeds_penalty():
    left = [L(1, 0, 100, "S"), L(2, 100, 200, "M")]
    right = [L(1, 0, 100, "S"), L(2, 100, 300, "M")]

    cheap = align(left, right, 150, [])
    assert cheap.status == "aligned"
    assert cheap.pairs == [(0, 0), (1, 1)]
    assert cheap.total_cost == 100

    # Pairing both layers costs 100; pairing only the first and skipping the
    # two deep layers costs 2 * 10 = 20, so the skip wins.
    expensive = align(left, right, 10, [])
    assert expensive.status == "aligned"
    assert expensive.pairs == [(0, 0)]
    assert expensive.unmatched_left == [1]
    assert expensive.unmatched_right == [1]
    assert expensive.total_cost == 20


def test_confirmed_pair_must_be_kept_even_when_costly():
    left = [L(1, 0, 100, "S"), L(2, 100, 400, "S")]
    right = [L(1, 0, 100, "S"), L(2, 100, 300, "S")]
    result = align(left, right, 1, [(2, 2)])
    assert result.status == "aligned"
    assert (1, 1) in result.pairs
    assert result.total_cost == 100


def test_confirmed_lithology_mismatch_is_pending():
    left = [L(1, 0, 100, "S")]
    right = [L(1, 0, 100, "M")]
    result = align(left, right, 5, [(1, 1)])
    assert result.status == "pending_review"
    assert any(reason.startswith("confirmed_lithology_mismatch") for reason in result.reasons)


def test_confirmed_crossing_is_pending():
    left = [L(1, 0, 100, "S"), L(2, 100, 200, "S"), L(3, 200, 300, "S")]
    right = [L(1, 0, 100, "S"), L(2, 100, 200, "S"), L(3, 200, 300, "S")]
    result = align(left, right, 5, [(1, 2), (2, 1)])
    assert result.status == "pending_review"
    assert any(reason.startswith("confirmed_matches_cross") for reason in result.reasons)


def test_tied_optimal_is_pending():
    left = [L(1, 0, 100, "S")]
    right = [L(1, 0, 100, "S"), L(2, 100, 200, "S")]
    # The single left layer can match either right layer at equal cost, so
    # there is no unique correspondence.
    result = align(left, right, 10, [])
    assert result.status == "pending_review"
    assert result.reasons == ["multiple_optimal_alignments"]
    assert result.pairs == []


def test_confirmed_pair_resolves_tie():
    left = [L(1, 0, 100, "S")]
    right = [L(1, 0, 100, "S"), L(2, 100, 200, "S")]
    result = align(left, right, 10, [(1, 2)])
    assert result.status == "aligned"
    assert result.pairs == [(0, 1)]


def test_matches_are_order_preserving():
    left = [L(1, 0, 100, "S"), L(2, 100, 250, "M"), L(3, 250, 400, "L")]
    right = [L(1, 0, 90, "S"), L(2, 90, 240, "M"), L(3, 240, 390, "L")]
    result = align(left, right, 20, [])
    assert result.status == "aligned"
    assert result.pairs == [(0, 0), (1, 1), (2, 2)]
    left_indices = [i for i, _ in result.pairs]
    right_indices = [j for _, j in result.pairs]
    assert left_indices == sorted(left_indices)
    assert right_indices == sorted(right_indices)
    assert len(set(right_indices)) == len(right_indices)


# ------------------------------------------------------------------------ API


@pytest.fixture()
def client():
    directory = tempfile.mkdtemp()
    app = create_app(os.path.join(directory, "test.sqlite3"))
    with TestClient(app) as test_client:
        yield test_client


def upload(client, hole, revision, layers):
    response = client.post(
        "/api/holes/revisions",
        json={"hole_id": hole, "revision": revision, "layers": layers},
    )
    return response


def layer(no, top, bottom, lith):
    return {"layer_no": no, "top_cm": top, "bottom_cm": bottom, "lithology": lith}


def test_upload_and_compute_flow(client):
    assert upload(client, "ZK-1", 1, [layer(1, 0, 100, "S"), layer(2, 100, 220, "M")]).status_code == 201
    assert upload(client, "ZK-2", 1, [layer(1, 0, 110, "S"), layer(2, 110, 230, "M")]).status_code == 201

    response = client.post(
        "/api/alignments",
        json={
            "left_hole_id": "ZK-1",
            "right_hole_id": "ZK-2",
            "skip_penalty": 30,
            "confirmed_matches": [{"left_layer_no": 1, "right_layer_no": 1}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "aligned"
    assert body["left_revision"] == 1
    assert [m["left_layer_no"] for m in body["matches"]] == [1, 2]
    assert body["matches"][0]["confirmed"] is True
    assert body["total_cost"] == 10
    assert body["unmatched_left_layer_nos"] == []
    assert body["unmatched_right_layer_nos"] == []


def test_upload_rejects_overlapping_or_unsorted_layers(client):
    response = upload(
        client,
        "ZK-1",
        1,
        [layer(1, 0, 200, "S"), layer(2, 100, 300, "M")],
    )
    assert response.status_code == 422

    response = upload(
        client,
        "ZK-1",
        1,
        [layer(1, 0, 100, "S"), layer(2, 100, 200, "M"), layer(3, 50, 60, "L")],
    )
    assert response.status_code == 422


def test_upload_requires_increasing_revision(client):
    assert upload(client, "ZK-1", 2, [layer(1, 0, 100, "S")]).status_code == 201
    assert upload(client, "ZK-1", 2, [layer(1, 0, 100, "S")]).status_code == 409
    assert upload(client, "ZK-1", 1, [layer(1, 0, 100, "S")]).status_code == 409


def test_compute_rejects_stale_revision(client):
    assert upload(client, "ZK-1", 1, [layer(1, 0, 100, "S")]).status_code == 201
    assert upload(client, "ZK-2", 1, [layer(1, 0, 100, "S")]).status_code == 201
    assert upload(client, "ZK-2", 2, [layer(1, 0, 120, "S")]).status_code == 201

    response = client.post(
        "/api/alignments",
        json={
            "left_hole_id": "ZK-1",
            "right_hole_id": "ZK-2",
            "left_revision": 1,
            "right_revision": 1,
            "skip_penalty": 10,
        },
    )
    assert response.status_code == 409


def test_revision_invalidates_previous_result(client):
    upload(client, "ZK-1", 1, [layer(1, 0, 100, "S")])
    upload(client, "ZK-2", 1, [layer(1, 0, 100, "S")])
    response = client.post(
        "/api/alignments",
        json={"left_hole_id": "ZK-1", "right_hole_id": "ZK-2", "skip_penalty": 10},
    )
    assert response.status_code == 201
    result_id = response.json()["result_id"]

    current = client.get(f"/api/alignments/{result_id}").json()
    assert current["is_current"] is True

    upload(client, "ZK-2", 2, [layer(1, 0, 130, "S")])
    stale = client.get(f"/api/alignments/{result_id}").json()
    assert stale["is_current"] is False
    assert stale["invalidated_at"] is not None

    listed = client.get("/api/alignments", params={"hole_id": "ZK-1"}).json()
    assert listed == []
    all_results = client.get(
        "/api/alignments", params={"hole_id": "ZK-1", "current_only": False}
    ).json()
    assert [item["result_id"] for item in all_results] == [result_id]


def test_recompute_current_after_revision(client):
    upload(client, "ZK-1", 1, [layer(1, 0, 100, "S")])
    upload(client, "ZK-2", 1, [layer(1, 0, 100, "S")])
    upload(client, "ZK-2", 2, [layer(1, 0, 140, "S")])

    response = client.post(
        "/api/alignments",
        json={"left_hole_id": "ZK-1", "right_hole_id": "ZK-2", "skip_penalty": 100},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "aligned"
    assert body["right_revision"] == 2
    assert body["total_cost"] == 40


def test_confirmed_mismatch_returns_pending_without_alignment(client):
    upload(client, "ZK-1", 1, [layer(1, 0, 100, "S")])
    upload(client, "ZK-2", 1, [layer(1, 0, 100, "M")])
    response = client.post(
        "/api/alignments",
        json={
            "left_hole_id": "ZK-1",
            "right_hole_id": "ZK-2",
            "skip_penalty": 10,
            "confirmed_matches": [{"left_layer_no": 1, "right_layer_no": 1}],
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_review"
    assert body["matches"] == []
    assert body["unmatched_left_layer_nos"] is None
    assert body["unmatched_right_layer_nos"] is None
    assert body["total_cost"] is None
    assert any("confirmed_lithology_mismatch" in r for r in body["review_reasons"])


def test_unknown_confirmed_layer_is_404(client):
    upload(client, "ZK-1", 1, [layer(1, 0, 100, "S")])
    upload(client, "ZK-2", 1, [layer(1, 0, 100, "S")])
    response = client.post(
        "/api/alignments",
        json={
            "left_hole_id": "ZK-1",
            "right_hole_id": "ZK-2",
            "skip_penalty": 10,
            "confirmed_matches": [{"left_layer_no": 9, "right_layer_no": 1}],
        },
    )
    assert response.status_code == 404


def test_results_survive_restart(client):
    upload(client, "ZK-1", 1, [layer(1, 0, 100, "S")])
    upload(client, "ZK-2", 1, [layer(1, 0, 100, "S")])
    response = client.post(
        "/api/alignments",
        json={"left_hole_id": "ZK-1", "right_hole_id": "ZK-2", "skip_penalty": 10},
    )
    result_id = response.json()["result_id"]

    # Simulate a restart with a brand new app instance over the same file.
    db_path = client.app.state.store.path
    fresh = create_app(db_path)
    with TestClient(fresh) as restarted:
        fetched = restarted.get(f"/api/alignments/{result_id}")
        assert fetched.status_code == 200
        assert fetched.json()["result_id"] == result_id

        recomputed = restarted.post(
            "/api/alignments",
            json={"left_hole_id": "ZK-1", "right_hole_id": "ZK-2", "skip_penalty": 10},
        )
        assert recomputed.status_code == 201


def test_interleaved_compute_cannot_publish_stale_revision(client, monkeypatch):
    upload(client, "ZK-1", 1, [layer(1, 0, 100, "S")])
    upload(client, "ZK-2", 1, [layer(1, 0, 100, "S")])

    from app import store as store_module

    original_align = store_module.align
    state = {"calls": 0}

    def slow_align(*args, **kwargs):
        state["calls"] += 1
        # While this computation is in flight, a new revision of ZK-2 arrives.
        upload(client, "ZK-2", 2, [layer(1, 0, 150, "S")])
        return original_align(*args, **kwargs)

    monkeypatch.setattr(store_module, "align", slow_align)
    response = client.post(
        "/api/alignments",
        json={
            "left_hole_id": "ZK-1",
            "right_hole_id": "ZK-2",
            "left_revision": 1,
            "right_revision": 1,
            "skip_penalty": 10,
        },
    )
    assert state["calls"] == 1
    assert response.status_code == 409
    # Nothing stale was published.
    assert client.get("/api/alignments", params={"hole_id": "ZK-2"}).json() == []

    # A later computation against the now-current revisions succeeds.
    monkeypatch.setattr(store_module, "align", original_align)
    retried = client.post(
        "/api/alignments",
        json={"left_hole_id": "ZK-1", "right_hole_id": "ZK-2", "skip_penalty": 10},
    )
    assert retried.status_code == 201
    assert retried.json()["right_revision"] == 2


def test_openapi_available(client):
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/api/holes/revisions" in paths
    assert "/api/alignments" in paths
