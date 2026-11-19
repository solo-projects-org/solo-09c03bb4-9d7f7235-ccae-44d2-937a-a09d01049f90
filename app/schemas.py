from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Reason codes shared with ``app.samples``; kept as a literal so OpenAPI shows
# the exhaustive set.
RejectionReason = Literal["crosses_layer_boundary", "lies_in_gap", "lithology_mismatch"]


class LayerIn(BaseModel):
    """One lithological interval, in integer centimetres below the collar."""

    layer_no: int = Field(..., description="Stratum number within the hole")
    top_cm: int = Field(..., ge=0, description="Top depth, integer centimetres")
    bottom_cm: int = Field(..., gt=0, description="Bottom depth, integer centimetres")
    lithology: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Lithology code; compared after trimming surrounding whitespace",
    )

    @model_validator(mode="after")
    def _check_interval(self) -> "LayerIn":
        if self.bottom_cm <= self.top_cm:
            raise ValueError("bottom_cm must be greater than top_cm")
        if not self.lithology.strip():
            raise ValueError("lithology must not be blank")
        return self


class UploadRequest(BaseModel):
    """A complete revision of one borehole's layered log."""

    hole_id: str = Field(..., min_length=1, max_length=64)
    revision: int = Field(..., ge=1, description="Strictly increasing revision number per hole")
    layers: list[LayerIn] = Field(
        default_factory=list,
        max_length=2000,
        description="Complete log replacing any earlier revision; must be depth-ordered",
    )

    @model_validator(mode="after")
    def _check_layers(self) -> "UploadRequest":
        ordered = sorted(self.layers, key=lambda layer: (layer.top_cm, layer.bottom_cm))
        previous: LayerIn | None = None
        for layer in ordered:
            if previous is not None and layer.top_cm < previous.bottom_cm:
                raise ValueError(
                    f"layers overlap or are not ordered: {previous.layer_no} and {layer.layer_no}"
                )
            previous = layer
        seen: set[int] = set()
        for layer in self.layers:
            if layer.layer_no in seen:
                raise ValueError(f"duplicate layer_no {layer.layer_no}")
            seen.add(layer.layer_no)
        return self


class MatchSpecIn(BaseModel):
    """A pair of strata asserted to correspond, and thus required in the answer."""

    left_layer_no: int
    right_layer_no: int


class ComputeRequest(BaseModel):
    left_hole_id: str = Field(..., min_length=1, max_length=64)
    right_hole_id: str = Field(..., min_length=1, max_length=64)
    left_revision: int | None = Field(
        default=None, ge=1, description="Omit to use the hole's current revision"
    )
    right_revision: int | None = Field(
        default=None, ge=1, description="Omit to use the hole's current revision"
    )
    skip_penalty: int = Field(..., gt=0, description="Positive integer penalty per skipped layer")
    confirmed_matches: list[MatchSpecIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_request(self) -> "ComputeRequest":
        if self.left_hole_id == self.right_hole_id:
            raise ValueError("left_hole_id and right_hole_id must differ")
        seen_left: set[int] = set()
        seen_right: set[int] = set()
        for match in self.confirmed_matches:
            if match.left_layer_no in seen_left:
                raise ValueError(f"left layer {match.left_layer_no} is confirmed more than once")
            if match.right_layer_no in seen_right:
                raise ValueError(f"right layer {match.right_layer_no} is confirmed more than once")
            seen_left.add(match.left_layer_no)
            seen_right.add(match.right_layer_no)
        return self


class HoleOut(BaseModel):
    hole_id: str
    current_revision: int
    layers: list[LayerIn]


class UploadResult(BaseModel):
    hole_id: str
    revision: int
    layer_count: int
    superseded_results: int = Field(
        ..., description="Number of published results invalidated by this revision"
    )
    active_samples: int = Field(
        ...,
        description="Active samples of this hole, all re-checked against the new revision",
    )
    sample_conflicts: int = Field(
        default=0, description="Always 0: a revision with conflicts is rejected whole"
    )


class SampleCreate(BaseModel):
    """Register one pending-inspection core sample."""

    sample_no: str = Field(..., min_length=1, max_length=64)
    hole_id: str = Field(..., min_length=1, max_length=64)
    top_cm: int = Field(..., ge=0, description="Core run start depth, integer centimetres")
    bottom_cm: int = Field(..., gt=0, description="Core run end depth, integer centimetres")
    lithology: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Lithology written on the sample label",
    )

    @model_validator(mode="after")
    def _check_interval(self) -> "SampleCreate":
        if self.bottom_cm <= self.top_cm:
            raise ValueError("bottom_cm must be greater than top_cm")
        if not self.lithology.strip():
            raise ValueError("lithology must not be blank")
        return self


class SampleOut(BaseModel):
    sample_no: str
    hole_id: str
    top_cm: int
    bottom_cm: int
    lithology: str
    status: Literal["active", "withdrawn"]
    layer_no: int | None = Field(
        default=None,
        description="Current layer containing the interval; null once withdrawn",
    )
    registered_revision: int | None = Field(
        default=None, description="Revision current when the sample was registered"
    )
    current_revision: int | None = Field(
        default=None, description="Hole's current revision, from the same snapshot"
    )
    registered_at: str
    withdrawn_at: str | None = None
    withdrawn_revision: int | None = None


class SampleConflict(BaseModel):
    sample_no: str
    reason: RejectionReason


class SampleRejection(BaseModel):
    """A registration that the current log cannot satisfy."""

    detail: str
    reason: RejectionReason


class RevisionConflict(BaseModel):
    """A revision rejected whole because of active samples."""

    detail: str
    conflicts: list[SampleConflict]


class VerifySamplesOut(BaseModel):
    status: Literal["ok", "conflicts"]
    checked: int
    conflicts: list[dict[str, str]]


class MatchOut(BaseModel):
    left_layer_no: int
    right_layer_no: int
    thickness_difference_cm: int
    confirmed: bool


class ComputeResult(BaseModel):
    result_id: str
    status: Literal["aligned", "pending_review"]
    left_hole_id: str
    right_hole_id: str
    left_revision: int
    right_revision: int
    skip_penalty: int
    matches: list[MatchOut]
    # Null whenever there is no unique alignment (pending_review): the API
    # deliberately publishes no correspondence at all in that case.
    unmatched_left_layer_nos: list[int] | None
    unmatched_right_layer_nos: list[int] | None
    total_cost: int | None = None
    review_reasons: list[str] = Field(default_factory=list)
    created_at: str
    is_current: bool | None = None
    invalidated_at: str | None = None
