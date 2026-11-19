"""Walk through the sample-registration and revision-swap flow over HTTP.

Standard library only, so it runs against a container without extra installs.
Start the service first (``docker compose up --build -d``) and then:

    python3 -I scripts/swagger_demo.py
    python3 -I scripts/swagger_demo.py http://127.0.0.1:8000

Each step prints the request and the parts of the response that matter.  The
same endpoints are in Swagger UI at ``/docs`` if you prefer clicking.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8000"
# A fresh suffix per run keeps the demo repeatable without deleting data.
RUN = time.strftime("%H%M%S")
H1, H2 = f"ZK-DEMO-{RUN}-1", f"ZK-DEMO-{RUN}-2"
# Sample numbers are globally unique, so they carry the run suffix too.
S = f"S-DEMO-{RUN}"

REV1 = [
    {"layer_no": 1, "top_cm": 0, "bottom_cm": 120, "lithology": "silty_clay"},
    {"layer_no": 2, "top_cm": 120, "bottom_cm": 260, "lithology": "fine_sand"},
    {"layer_no": 3, "top_cm": 260, "bottom_cm": 400, "lithology": "weathered_mudstone"},
]
# Splits the sample interval 30-90 across a new boundary at 60.
SPLIT = [
    {"layer_no": 1, "top_cm": 0, "bottom_cm": 60, "lithology": "silty_clay"},
    {"layer_no": 2, "top_cm": 60, "bottom_cm": 120, "lithology": "silty_clay"},
    {"layer_no": 3, "top_cm": 120, "bottom_cm": 260, "lithology": "fine_sand"},
    {"layer_no": 4, "top_cm": 260, "bottom_cm": 400, "lithology": "weathered_mudstone"},
]
# Same rock, different numbers: the label stays valid and the layer moves.
RENUMBERED = [
    {"layer_no": 11, "top_cm": 0, "bottom_cm": 120, "lithology": "silty_clay"},
    {"layer_no": 12, "top_cm": 120, "bottom_cm": 260, "lithology": "fine_sand"},
    {"layer_no": 13, "top_cm": 260, "bottom_cm": 400, "lithology": "weathered_mudstone"},
]


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def step(number: int, text: str) -> None:
    print(f"\n=== {number}. {text}")


def show(label: str, code: int, body: object) -> None:
    print(f"  {label}: HTTP {code}")
    print("  " + json.dumps(body, ensure_ascii=False, indent=2).replace("\n", "\n  "))


def upload(hole: str, revision: int, layers: list[dict]) -> tuple[int, dict]:
    return call(
        "POST",
        "/api/holes/revisions",
        {"hole_id": hole, "revision": revision, "layers": layers},
    )


def sample(no: str, hole: str, top: int, bottom: int, lith: str) -> tuple[int, dict]:
    return call(
        "POST",
        "/api/samples",
        {
            "sample_no": no,
            "hole_id": hole,
            "top_cm": top,
            "bottom_cm": bottom,
            "lithology": lith,
        },
    )


def main() -> int:
    global BASE
    if len(sys.argv) > 1:
        BASE = sys.argv[1].rstrip("/")

    step(0, f"health check at {BASE}")
    code, body = call("GET", "/health")
    show("GET /health", code, body)
    if code != 200:
        print("service is not reachable; start it with `docker compose up --build -d`")
        return 1

    step(1, "upload the first revision of both holes")
    for hole, layers in ((H2, REV1), (H1, REV1)):
        code, body = upload(hole, 1, layers)
        show(f"POST /api/holes/revisions ({hole})", code, body)
        assert code == 201, body

    step(2, "register a sample wholly inside layer 1 of the current revision")
    code, body = sample(S, H1, 30, 90, "silty_clay")
    show("POST /api/samples", code, body)
    assert code == 201 and body["layer_no"] == 1 and body["current_revision"] == 1, body

    step(3, "three registrations the current log cannot satisfy")
    for args, expected in (
        (( f"{S}-X1", H1, 90, 150, "silty_clay"), "crosses_layer_boundary"),
        ((f"{S}-X2", H1, 400, 420, "weathered_mudstone"), "lies_in_gap"),
        ((f"{S}-X3", H1, 130, 180, "silty_clay"), "lithology_mismatch"),
    ):
        code, body = sample(*args)
        show(f"POST /api/samples {args[0]}", code, body)
        assert code == 422 and body["reason"] == expected, body

    step(4, "upload a revision that splits the registered sample across a boundary")
    code, body = upload(H1, 2, SPLIT)
    show("POST /api/holes/revisions", code, body)
    assert code == 409 and body["conflicts"] == [
        {"sample_no": S, "reason": "crosses_layer_boundary"}
    ], body
    code, body = call("GET", f"/api/holes/{H1}")
    print(f"  current revision after rejection: {body['current_revision']} (unchanged)")

    step(5, "withdraw the sample, then the same revision is accepted")
    code, body = call("DELETE", f"/api/samples/{S}")
    show(f"DELETE /api/samples/{S}", code, body)
    code, body = upload(H1, 2, SPLIT)
    show("POST /api/holes/revisions (retry)", code, body)
    assert code == 201, body

    step(6, "re-register in the new log; the layer number and revision move")
    code, body = sample(S, H1, 30, 60, "silty_clay")
    show("POST /api/samples (re-registered)", code, body)
    assert code == 201 and body["layer_no"] == 1 and body["current_revision"] == 2, body

    step(7, "a compatible revision renumbers layers; the registration survives")
    code, body = upload(H1, 3, RENUMBERED)
    show("POST /api/holes/revisions (renumbered)", code, body)
    assert code == 201 and body["active_samples"] == 1, body
    code, body = call("GET", f"/api/samples/{S}")
    show(f"GET /api/samples/{S}", code, body)
    assert code == 200 and body["layer_no"] == 11 and body["current_revision"] == 3, body
    assert body["status"] == "active"

    step(8, "verify every active sample against its current revision")
    code, body = call("GET", "/api/samples/verify")
    show("GET /api/samples/verify", code, body)
    assert code == 200 and body["status"] == "ok", body

    print(f"\nAll demo steps passed for holes {H1} / {H2}.")
    print("Each run uses fresh hole ids, so it can be repeated on the same volume.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
