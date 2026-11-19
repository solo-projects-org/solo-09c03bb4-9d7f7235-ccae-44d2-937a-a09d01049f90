"""Order-preserving stratum alignment with forced correspondences.

For a correspondence M (a set of matched layer pairs), the objective is::

    sum(|thickness_l - thickness_r| for (l, r) in M)
    + skip_penalty * (unmatched_left + unmatched_right)

Only layers of equal lithology may be matched, matched indices must increase
on both sides (no crossings), and confirmed pairs are forced into M.

Dropping the constant term ``skip_penalty * (N_l + N_r)`` turns this into a
minimum-weight chain problem with pair weight ``|dt| - 2 * skip_penalty``.

*Distinct correspondences* are counted as distinct chains (sets of matched
pairs).  Different interleavings of the same skipped layers are the same
correspondence and are correctly counted once.

The DP keeps, for every prefix rectangle, the minimum chain weight, the exact
number of cells attaining it, and whether any of those cells already has more
than one optimal chain.  Counts are cell counts (bounded by the grid size),
so the 2-D inclusion-exclusion used to combine prefixes is exact.  This lets
us decide uniqueness without enumerating correspondences.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass, field

INF = 1 << 62


@dataclass(frozen=True)
class Layer:
    layer_no: int
    top_cm: int
    bottom_cm: int
    lithology: str

    @property
    def thickness_cm(self) -> int:
        return self.bottom_cm - self.top_cm


@dataclass
class Alignment:
    status: str
    # Global layer indices (0-based into the depth-ordered input lists).
    pairs: list[tuple[int, int]] = field(default_factory=list)
    unmatched_left: list[int] = field(default_factory=list)
    unmatched_right: list[int] = field(default_factory=list)
    total_cost: int | None = None
    reasons: list[str] = field(default_factory=list)


@dataclass
class _Chain:
    weight: int
    # Number of distinct optimal chains, capped at 2; includes the empty chain.
    count: int
    # One optimal chain (0-based indices into the supplied index lists).
    pairs: list[tuple[int, int]]


def _best_chain(
    left_indices: list[int],
    right_indices: list[int],
    left: list[Layer],
    right: list[Layer],
    skip_penalty: int,
) -> _Chain:
    """Optimal order-preserving chain inside one anchor-free rectangle.

    Returns the minimum reduced weight (<= 0, the empty chain competes), the
    number of distinct optimal chains capped at 2, and one optimal chain.
    """

    n = len(left_indices)
    m = len(right_indices)
    if n == 0 or m == 0:
        return _Chain(0, 1, [])

    width = m + 1
    # Prefix state of the previous row over columns 1..j:
    #   prev_best  minimum reduced weight over cells in the rectangle
    #   prev_count number of cells attaining it
    #   prev_multi whether any of those cells has >= 2 optimal chains
    #   prev_arg   one attaining cell, encoded as i * width + j
    # An empty rectangle (no rows or no columns) has best 0, zero cells, and
    # therefore contributes only the empty chain.
    prev_best = [0] * width
    prev_count = [0] * width
    prev_multi = [False] * width
    prev_arg = [-1] * width

    cell_predecessors: list[array] = []

    # The empty chain (weight 0) is always a candidate for the whole grid.
    global_best = 0
    global_cells = 0
    global_multi = False
    global_end: int | None = None

    for i in range(1, n + 1):
        layer_l = left[left_indices[i - 1]]
        thickness_l = layer_l.thickness_cm

        cur_best = [0] * width
        cur_count = [0] * width
        cur_multi = [False] * width
        cur_arg = [-1] * width
        cell_predecessor = array("i", [-1]) * width

        for j in range(1, m + 1):
            layer_r = right[right_indices[j - 1]]

            p_best = prev_best[j - 1]
            p_count = prev_count[j - 1]
            p_multi = prev_multi[j - 1]

            cell_weight = INF
            cell_multi = False
            cell_predecessor[j] = -1
            if layer_l.lithology == layer_r.lithology:
                pair_weight = abs(thickness_l - layer_r.thickness_cm) - 2 * skip_penalty
                if p_best < 0:
                    # Extending an optimal (negative) prefix always beats
                    # starting a new chain here.
                    cell_weight = pair_weight + p_best
                    prefix_count = p_count       # cells attaining p_best
                    prefix_multi = p_multi
                    cell_predecessor[j] = prev_arg[j - 1]
                else:
                    # Only the empty chain extends optimally.  Zero-weight
                    # prefix cells start different chains; they are counted
                    # in ``p_count`` and the empty chain is added explicitly.
                    cell_weight = pair_weight
                    prefix_count = p_count + 1
                    prefix_multi = p_multi
                cell_multi = prefix_multi or prefix_count >= 2

                encoded = i * width + j
                if cell_weight < global_best:
                    global_best = cell_weight
                    global_cells = 1
                    global_multi = cell_multi
                    global_end = encoded
                elif cell_weight == global_best:
                    global_cells += 1
                    global_multi = global_multi or cell_multi

            # 2-D prefix minimum with exact cell counts.  The previous row's
            # rectangle (rows 1..i-1, cols 1..j) and the current row's prefix
            # (row i, cols 1..j-1) are disjoint, while the previous row's
            # prefix (rows 1..i-1, cols 1..j-1) is contained in both, so its
            # cells must be subtracted.
            best = prev_best[j]
            count = prev_count[j]
            multi = prev_multi[j]
            arg = prev_arg[j]

            left_best = cur_best[j - 1]
            if left_best < best:
                best = left_best
                count = cur_count[j - 1]
                multi = cur_multi[j - 1]
                arg = cur_arg[j - 1]
            elif left_best == best:
                count += cur_count[j - 1]
                multi = multi or cur_multi[j - 1]

            if prev_best[j - 1] == best:
                count -= prev_count[j - 1]

            # The new cell (i, j) is disjoint from all of the above.
            if cell_weight < best:
                best = cell_weight
                count = 1
                multi = cell_multi
                arg = i * width + j
            elif cell_weight == best:
                count += 1
                # Only the new cell's own flag matters here; ``multi`` from the
                # prefix could belong to the subtractive overlap above.
                multi = multi or cell_multi
                if arg == -1:
                    arg = i * width + j

            if count >= 2:
                multi = True

            cur_best[j] = best
            cur_count[j] = count
            cur_multi[j] = multi
            cur_arg[j] = arg

        cell_predecessors.append(cell_predecessor)
        prev_best, prev_count, prev_multi, prev_arg = (
            cur_best,
            cur_count,
            cur_multi,
            cur_arg,
        )

    # Total optimal chains: cells attaining the global minimum plus the empty
    # chain when it ties.
    if global_best < 0:
        total = global_cells
    else:
        total = global_cells + 1
    multiple = global_multi or total >= 2

    pairs: list[tuple[int, int]] = []
    if not multiple and global_end is not None:
        cell = global_end
        while cell != -1:
            i, j = divmod(cell, width)
            pairs.append((i - 1, j - 1))
            cell = cell_predecessors[i - 1][j]
        pairs.reverse()

    return _Chain(global_best, 2 if multiple else 1, pairs)


def _pending(reasons: list[str]) -> Alignment:
    return Alignment(status="pending_review", reasons=reasons)


def align(
    left: list[Layer],
    right: list[Layer],
    skip_penalty: int,
    confirmed: list[tuple[int, int]],
) -> Alignment:
    """Compute the optimal correspondence between two depth-ordered logs.

    ``confirmed`` holds pairs of ``layer_no`` values that must be matched.
    Raises ``KeyError`` if a confirmed layer does not exist.
    """

    if skip_penalty <= 0:
        raise ValueError("skip_penalty must be positive")

    left_by_no = {layer.layer_no: (idx, layer) for idx, layer in enumerate(left)}
    right_by_no = {layer.layer_no: (idx, layer) for idx, layer in enumerate(right)}

    anchors: list[tuple[int, int]] = []
    reasons: list[str] = []
    for left_no, right_no in confirmed:
        i, layer_l = left_by_no[left_no]
        j, layer_r = right_by_no[right_no]
        if layer_l.lithology != layer_r.lithology:
            reasons.append(
                "confirmed_lithology_mismatch: "
                f"left layer {left_no} is {layer_l.lithology!r} but "
                f"right layer {right_no} is {layer_r.lithology!r}"
            )
        anchors.append((i, j))

    anchors.sort()
    for (i1, j1), (i2, j2) in zip(anchors, anchors[1:]):
        if i2 <= i1 or j2 <= j1:
            reasons.append(
                "confirmed_matches_cross: "
                f"left {left[i1].layer_no}->right {right[j1].layer_no} and "
                f"left {left[i2].layer_no}->right {right[j2].layer_no} "
                "cannot both be order-preserving"
            )

    if reasons:
        return _pending(reasons)

    n = len(left)
    m = len(right)
    matches: list[tuple[int, int]] = []
    multiple = False
    previous_i = -1
    previous_j = -1

    for i, j in anchors:
        segment = _best_chain(
            list(range(previous_i + 1, i)),
            list(range(previous_j + 1, j)),
            left,
            right,
            skip_penalty,
        )
        if segment.count > 1:
            multiple = True
        matches.extend(
            (previous_i + 1 + a, previous_j + 1 + b) for a, b in segment.pairs
        )
        matches.append((i, j))
        previous_i, previous_j = i, j

    tail = _best_chain(
        list(range(previous_i + 1, n)),
        list(range(previous_j + 1, m)),
        left,
        right,
        skip_penalty,
    )
    if tail.count > 1:
        multiple = True
    matches.extend((previous_i + 1 + a, previous_j + 1 + b) for a, b in tail.pairs)

    if multiple:
        return _pending(["multiple_optimal_alignments"])
    if not matches:
        return _pending(["no_layers_matched"])

    matched_left = {i for i, _ in matches}
    matched_right = {j for _, j in matches}
    unmatched_left = [i for i in range(n) if i not in matched_left]
    unmatched_right = [j for j in range(m) if j not in matched_right]

    total_cost = skip_penalty * (len(unmatched_left) + len(unmatched_right))
    total_cost += sum(
        abs(left[i].thickness_cm - right[j].thickness_cm) for i, j in matches
    )

    return Alignment(
        status="aligned",
        pairs=matches,
        unmatched_left=unmatched_left,
        unmatched_right=unmatched_right,
        total_cost=total_cost,
    )
