"""Cross-check the alignment solver against exhaustive enumeration.

Run with ``python -I`` so that the script's directory is not put on
``sys.path``; the project path is inserted explicitly.

    python3 -I scripts/brute_force_check.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.matching import Layer, align  # noqa: E402


def enumerate_optimal(left, right, penalty, confirmed):
    """Brute force over every order-preserving matching.

    Returns (best_cost, number_of_optimal_matchings, one_optimal_matching) for
    matchings that contain all confirmed pairs, or (None, 0, None) if none do.
    """

    n, m = len(left), len(right)
    left_index = {layer.layer_no: i for i, layer in enumerate(left)}
    right_index = {layer.layer_no: j for j, layer in enumerate(right)}
    required = {(left_index[a], right_index[b]) for a, b in confirmed}

    best = None
    count = 0
    example = None

    def cost(pairs):
        unmatched = (n - len(pairs)) + (m - len(pairs))
        return penalty * unmatched + sum(
            abs(left[i].thickness_cm - right[j].thickness_cm) for i, j in pairs
        )

    def visit(next_left, next_right, pairs):
        nonlocal best, count, example
        if required.issubset(set(pairs)):
            value = cost(pairs)
            if best is None or value < best:
                best, count, example = value, 1, list(pairs)
            elif value == best:
                count += 1
        for i in range(next_left, n):
            for j in range(next_right, m):
                if left[i].lithology == right[j].lithology:
                    pairs.append((i, j))
                    visit(i + 1, j + 1, pairs)
                    pairs.pop()

    visit(0, 0, [])
    return best, count, example


def build_log(count, rng, lithologies):
    layers = []
    depth = 0
    for number in range(1, count + 1):
        thickness = rng.randint(1, 6)
        layers.append(
            Layer(number, depth, depth + thickness, rng.choice(lithologies))
        )
        depth += thickness
    return layers


def main() -> int:
    rng = random.Random(20261119)
    checked = 0
    for _ in range(5000):
        left = build_log(rng.randint(1, 5), rng, ["S", "M", "L"])
        right = build_log(rng.randint(1, 5), rng, ["S", "M", "L"])
        penalty = rng.choice([1, 2, 3, 5, 8, 13])

        confirmed = []
        if rng.random() < 0.6:
            used_left: set[int] = set()
            used_right: set[int] = set()
            for _ in range(rng.randint(1, 2)):
                a = rng.randint(1, len(left))
                b = rng.randint(1, len(right))
                if a in used_left or b in used_right:
                    continue
                used_left.add(a)
                used_right.add(b)
                confirmed.append((a, b))

        result = align(left, right, penalty, confirmed)
        best, count, example = enumerate_optimal(left, right, penalty, confirmed)

        left_index = {layer.layer_no: i for i, layer in enumerate(left)}
        right_index = {layer.layer_no: j for j, layer in enumerate(right)}
        ordered = sorted((left_index[a], right_index[b]) for a, b in confirmed)
        infeasible = any(
            x1 >= x2 or y1 >= y2 for (x1, y1), (x2, y2) in zip(ordered, ordered[1:])
        ) or any(
            left[left_index[a]].lithology != right[right_index[b]].lithology
            for a, b in confirmed
        )

        if infeasible:
            assert result.status == "pending_review", (result, left, right, confirmed)
            checked += 1
            continue

        unique = best is not None and count == 1 and example is not None
        # An empty matching is never a publishable alignment: there is no
        # correspondence to report.
        if unique and example:
            assert result.status == "aligned", (result, left, right, penalty, confirmed)
            assert result.total_cost == best
            assert result.pairs == example
        else:
            assert result.status == "pending_review", (result, best, count)

        checked += 1

    print(f"brute-force cross-check passed on {checked} random cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
