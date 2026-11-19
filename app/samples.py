"""Placement checks for pending-inspection core samples.

A sample is taken from one continuous core interval and is labelled with the
lithology observed on the core.  Before it may be sent for inspection, the
whole interval must lie inside a *single* layer of the hole's current log and
that layer must carry the same lithology as the label.  A layer boundary is
shared by its two neighbours, so an interval that ends exactly at a boundary
still belongs to the upper layer, and one that starts exactly at the boundary
belongs to the lower layer.

``locate_sample`` is the single source of truth for that rule: registration and
revision acceptance both call it, so a stored active sample is valid for the
current revision by construction.
"""

from __future__ import annotations

from typing import Any

from .matching import Layer

# Reason codes returned to callers alongside the offending sample number.
CROSSES_LAYER_BOUNDARY = "crosses_layer_boundary"
LIES_IN_GAP = "lies_in_gap"
LITHOLOGY_MISMATCH = "lithology_mismatch"


def locate_sample(
    layers: list[Layer],
    top_cm: int,
    bottom_cm: int,
    lithology: str,
) -> tuple[Layer | None, str | None]:
    """Locate ``[top_cm, bottom_cm]`` in one same-lithology layer.

    Returns ``(layer, None)`` when the interval is contained in a single layer
    whose lithology equals the label, otherwise ``(None, reason)`` where
    ``reason`` is one of the module-level reason codes.  Layers are expected to
    be sorted and non-overlapping.  Boundary contact is not an overlap.
    """

    for layer in layers:
        if layer.top_cm <= top_cm and bottom_cm <= layer.bottom_cm:
            # Layers never overlap, so this is the only possible container.
            if layer.lithology == lithology:
                return layer, None
            return None, LITHOLOGY_MISMATCH

    overlapping = sum(
        1
        for layer in layers
        if top_cm < layer.bottom_cm and bottom_cm > layer.top_cm
    )
    if overlapping >= 2:
        return None, CROSSES_LAYER_BOUNDARY
    return None, LIES_IN_GAP


def reconcile_placements(layers: list[Layer], active: list[dict[str, Any]]) -> None:
    """Recompute each active sample's layer in ``layers`` in place.

    All active samples have already been accepted by ``locate_sample`` against
    this exact layer set; this only refreshes the recorded ``layer_no`` so it
    describes the current revision even when the revision renumbered layers.
    Mutates the supplied dictionaries (the layer rows of the transaction).
    """

    for sample in active:
        layer, reason = locate_sample(
            layers, sample["top_cm"], sample["bottom_cm"], sample["lithology"]
        )
        if reason is not None or layer is None:
            # Unreachable: the revision checked every active sample under the
            # same lock and transaction before calling this.
            raise AssertionError(f"active sample {sample['sample_no']} is not placeable: {reason}")
        sample["layer_no"] = layer.layer_no
