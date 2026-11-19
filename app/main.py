"""FastAPI application for borehole stratum correspondence."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .matching import Layer
from .schemas import ComputeRequest, ComputeResult, HoleOut, UploadRequest, UploadResult
from .store import ConflictError, NotFoundError, Store

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
            "Upload complete layered logs per borehole revision, then compute the "
            "minimum-cost order-preserving correspondence between two current logs. "
            "A pending_review result intentionally carries no unique alignment."
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
        return JSONResponse(status_code=409, content={"detail": str(exc)})

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
            "than the hole's current revision is required. Accepting a revision "
            "immediately invalidates every published result that used an older "
            "revision of this hole."
        ),
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
