"""FastAPI application for borehole stratum correspondence."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .matching import Layer
from .schemas import (
    ComputeRequest,
    ComputeResult,
    HoleOut,
    RevisionConflict,
    SampleCreate,
    SampleOut,
    SampleRejection,
    UploadRequest,
    UploadResult,
    VerifySamplesOut,
)
from .store import (
    ConflictError,
    NotFoundError,
    RevisionRejectedError,
    SampleConflictError,
    SampleRejectedError,
    Store,
)

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "data/boreholes.sqlite3")


def create_app(db_path: str | None = None) -> FastAPI:
    resolved_path = db_path or DEFAULT_DB_PATH

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The database is opened here rather than at import time so that
        # importing the module (for example from tests) has no side effects,
        # and so a restart simply reopens the same file.
        store = Store(resolved_path)
        app.state.store = store
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="Borehole Stratum Alignment API",
        version="1.0.0",
        description=(
            "Upload complete layered logs per borehole revision, register core "
            "samples whose core run must sit inside one same-lithology layer of "
            "the current log, then compute the minimum-cost order-preserving "
            "correspondence between two current logs. A revision that would "
            "strand an active sample is rejected whole. A pending_review result "
            "intentionally carries no unique alignment."
        ),
        lifespan=lifespan,
    )

    def get_store(request: Request) -> Store:
        return request.app.state.store

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
        if isinstance(exc, RevisionRejectedError):
            return JSONResponse(
                status_code=409,
                content={"detail": str(exc), "conflicts": exc.conflicts},
            )
        if isinstance(exc, SampleConflictError):
            return JSONResponse(
                status_code=409,
                content={"detail": str(exc), "sample_status": exc.active_status},
            )
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SampleRejectedError)
    async def _sample_rejected(_: Request, exc: SampleRejectedError) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"detail": str(exc), "reason": exc.reason}
        )

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/api/holes/revisions",
        response_model=UploadResult,
        status_code=201,
        tags=["uploads"],
        summary="Upload a complete revision of one borehole's layered log",
        description=(
            "The layer set replaces the whole log for that revision. Layers must be "
            "depth-ordered and non-overlapping. A revision number strictly greater "
            "than the hole's current revision is required. Before the revision is "
            "accepted every active sample of the hole is re-checked against it; if "
            "any sample would cross a layer boundary, fall in a gap or no longer "
            "match its label lithology the upload is rejected whole (409 with the "
            "offending sample numbers) and the previous revision plus all sample "
            "states are left unchanged. Otherwise accepting a revision immediately "
            "invalidates every published result that used an older revision of this "
            "hole, and active samples stay valid even if their layer numbers change."
        ),
        responses={
            409: {
                "model": RevisionConflict,
                "description": "Revision rejected whole; conflicts lists every offending sample",
            },
        },
    )
    def upload_revision(payload: UploadRequest, store: Store = Depends(get_store)) -> dict[str, Any]:
        layers = [
            Layer(
                layer_no=item.layer_no,
                top_cm=item.top_cm,
                bottom_cm=item.bottom_cm,
                lithology=item.lithology.strip(),
            )
            for item in payload.layers
        ]
        return store.upsert_revision(payload.hole_id, payload.revision, layers)

    @app.post(
        "/api/samples",
        response_model=SampleOut,
        status_code=201,
        tags=["samples"],
        summary="Register a pending-inspection core sample",
        description=(
            "The core run [top_cm, bottom_cm] must lie wholly inside one layer of "
            "the hole's current revision and that layer must have the lithology "
            "written on the label. Touching a single boundary is allowed; spanning "
            "a boundary, falling in an unlogged gap, or a label mismatch is "
            "rejected with 422 and a machine-readable reason. A sample number with "
            "an active registration must be withdrawn before it can be registered "
            "again (409). layer_no, registered_revision and current_revision are "
            "read from the same committed snapshot."
        ),
        responses={
            404: {"description": "Hole has no revisions"},
            409: {"description": "Sample number already has an active registration"},
            422: {
                "model": SampleRejection,
                "description": "Interval does not fit one same-lithology layer",
            },
        },
    )
    def register_sample(payload: SampleCreate, store: Store = Depends(get_store)) -> dict[str, Any]:
        return store.register_sample(
            sample_no=payload.sample_no,
            hole_id=payload.hole_id,
            top_cm=payload.top_cm,
            bottom_cm=payload.bottom_cm,
            lithology=payload.lithology.strip(),
        )

    @app.delete(
        "/api/samples/{sample_no}",
        response_model=SampleOut,
        tags=["samples"],
        summary="Withdraw an active sample registration",
        description=(
            "The sample number becomes free to register again after the log is "
            "corrected and the sample relabelled. Only an active registration can "
            "be withdrawn; repeating the call returns 404."
        ),
        responses={404: {"description": "No such sample or it is already withdrawn"}},
    )
    def withdraw_sample(sample_no: str, store: Store = Depends(get_store)) -> dict[str, Any]:
        return store.withdraw_sample(sample_no)

    @app.get(
        "/api/samples",
        response_model=list[SampleOut],
        tags=["samples"],
        summary="List samples, by default only active ones",
    )
    def list_samples(
        store: Store = Depends(get_store),
        hole_id: str | None = Query(default=None),
        status: Literal["active", "withdrawn", "all"] = Query(
            default="active",
            description="'all' lists both states",
        ),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return store.list_samples(
            hole_id=hole_id,
            status=None if status == "all" else status,
            limit=limit,
        )

    # Registered before /api/samples/{sample_no} so that "verify" is not
    # captured as a sample number.
    @app.get(
        "/api/samples/verify",
        response_model=VerifySamplesOut,
        tags=["samples"],
        summary="Re-check every active sample against its hole's current revision",
        description=(
            "Operational safety net: registration and every sample-driven revision "
            "change re-check in the same transaction, so a healthy database always "
            "reports status=ok."
        ),
    )
    def verify_samples(
        store: Store = Depends(get_store),
        hole_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        return store.verify_samples(hole_id=hole_id)

    @app.get(
        "/api/samples/{sample_no}",
        response_model=SampleOut,
        tags=["samples"],
        summary="Fetch a sample registration",
        description=(
            "layer_no and current_revision are read from the same committed "
            "snapshot. For an active sample the layer always belongs to the "
            "current revision."
        ),
        responses={404: {"description": "No such sample"}},
    )
    def get_sample(sample_no: str, store: Store = Depends(get_store)) -> dict[str, Any]:
        return store.get_sample(sample_no)

    @app.get(
        "/api/holes/{hole_id}",
        response_model=HoleOut,
        tags=["uploads"],
        summary="Show the current revision and its layers",
        responses={404: {"description": "Hole has no revisions"}},
    )
    def get_hole(hole_id: str, store: Store = Depends(get_store)) -> dict[str, Any]:
        latest = store.latest_revision(hole_id)
        if latest is None:
            raise NotFoundError(f"hole {hole_id!r} has no revisions")
        layers = store.layers_for(hole_id, latest)
        return {
            "hole_id": hole_id,
            "current_revision": latest,
            "layers": [
                {
                    "layer_no": layer.layer_no,
                    "top_cm": layer.top_cm,
                    "bottom_cm": layer.bottom_cm,
                    "lithology": layer.lithology,
                }
                for layer in layers
            ],
        }

    @app.post(
        "/api/alignments",
        response_model=ComputeResult,
        status_code=201,
        tags=["alignment"],
        summary="Compute the optimal correspondence between two current logs",
        description=(
            "Objective: sum of absolute thickness differences over matched pairs, "
            "plus skip_penalty for every unmatched layer in either hole. Matches must "
            "have equal lithology, must not cross, and every confirmed pair is forced. "
            "If the confirmed pairs are infeasible, no layer can be matched, or more "
            "than one distinct optimal correspondence exists, the result is "
            "pending_review and contains no unique alignment. Omit a revision to use "
            "that hole's current revision (useful after a restart)."
        ),
        responses={
            404: {"description": "Hole/revision or confirmed layer not found"},
            409: {"description": "Requested revision is no longer current"},
        },
    )
    def compute(payload: ComputeRequest, store: Store = Depends(get_store)) -> dict[str, Any]:
        left_revision = payload.left_revision
        right_revision = payload.right_revision
        if left_revision is None:
            left_revision = store.latest_revision(payload.left_hole_id)
            if left_revision is None:
                raise NotFoundError(f"hole {payload.left_hole_id!r} has no revisions")
        if right_revision is None:
            right_revision = store.latest_revision(payload.right_hole_id)
            if right_revision is None:
                raise NotFoundError(f"hole {payload.right_hole_id!r} has no revisions")
        return store.compute(
            left_hole_id=payload.left_hole_id,
            right_hole_id=payload.right_hole_id,
            left_revision=left_revision,
            right_revision=right_revision,
            skip_penalty=payload.skip_penalty,
            confirmed=[
                (match.left_layer_no, match.right_layer_no)
                for match in payload.confirmed_matches
            ],
        )

    @app.get(
        "/api/alignments/{result_id}",
        response_model=ComputeResult,
        tags=["alignment"],
        summary="Fetch a result by id",
        description=(
            "Stored results are immutable. If a later revision of either hole was "
            "accepted, is_current is false and invalidated_at is set."
        ),
    )
    def get_result(result_id: str, store: Store = Depends(get_store)) -> dict[str, Any]:
        return store.get_result(result_id)

    @app.get(
        "/api/alignments",
        response_model=list[ComputeResult],
        tags=["alignment"],
        summary="List recent results, optionally filtered by hole",
    )
    def list_results(
        store: Store = Depends(get_store),
        hole_id: str | None = Query(default=None, description="Match either hole"),
        current_only: bool = Query(
            default=True,
            description="Exclude results invalidated by a newer revision",
        ),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return store.list_results(hole_id=hole_id, current_only=current_only, limit=limit)

    return app


app = create_app()
