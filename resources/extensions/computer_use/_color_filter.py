"""Remove a screen colour filter that desktop composition applied to a capture.

Colour-temperature tools such as f.lux, and Windows Night light on some systems,
can tint the composed desktop instead of the display output, so every screen
capture carries the tint although applications drew their own colours. Such a
filter maps each pixel's sRGB values through one 3x3 matrix. A window's own
rendering from before composition, compared with the captured pixels at the same
place, measures that matrix, and its inverse restores the drawn colours.

A capture changes only when the references prove a filter: a reference that the
capture shows unchanged vetoes any correction, and a matrix must explain every
reference that can vote, invert and only dim colours. Anything less leaves the
capture unchanged.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from PIL import Image

# Rows are output channels: seen[c] = sum(matrix[c][k] * drawn[k]) on 0-255 sRGB values.
type Matrix = tuple[tuple[float, float, float], ...]
type _Color = tuple[int, int, int]
type _Pairs = list[tuple[_Color, _Color, int]]

IDENTITY: Matrix = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
# A matching pixel lies within this many levels on every channel (rounding only).
_TOLERANCE = 2.0
# Pixels this dark match any dimming filter and carry no evidence.
_INFORMATIVE = 48
# A reference votes with at least this many informative pixels and distinct drawn colours.
_MIN_PIXELS = 200
_MIN_COLORS = 8
# Distinct colours a fitted matrix must explain: a few colours fit some matrix by chance.
_FIT_COLORS = 24
_MAX_SAMPLES = 40_000
_MAX_PAIRS = 4000
# Share of a reference's informative pixels shown unchanged that proves there is no filter.
_CLEAN_SHARE = 0.3
# Share of every voting reference's informative pixels a filter matrix must explain.
_FILTER_SHARE = 0.6
# Colour spread a fit needs before it may extrapolate to colours the references lack.
_MIN_SPREAD = 1.0
_FIT_LIMITS = (32.0, 12.0, 5.0, _TOLERANCE)


@dataclass(frozen=True)
class Reference:
    """A window as it drew itself and the capture's pixels at the same place, same size."""

    drawn: Image.Image
    seen: Image.Image


def measure(references: Iterable[Reference], known: Matrix | None = None) -> Matrix | None:
    """The filter matrix the references prove, or ``None`` to leave the capture unchanged.

    *known* is a matrix proven earlier. It is reused only when it explains every
    voting reference now, so references with too few colours to fit a matrix
    can still confirm it.
    """
    votes: list[_Pairs] = []
    for reference in references:
        pairs = _pairs(reference)
        if _weight(pairs) < _MIN_PIXELS or len({drawn for drawn, _, _ in pairs}) < _MIN_COLORS:
            continue  # too little evidence to vote, for example a window that drew nothing
        if _share(IDENTITY, pairs) >= _CLEAN_SHARE:
            return None
        votes.append(pairs)
    if not votes:
        return None
    candidates = [fitted for pairs in votes if (fitted := _fit(pairs)) is not None]
    if known is not None:
        candidates.append(known)
    for candidate in candidates:
        if _proven(candidate, votes):
            # Refit over every reference's colours, which pins the matrix more exactly.
            refined = _fit([pair for pairs in votes for pair in pairs], candidate)
            return refined if refined is not None and _proven(refined, votes) else candidate
    return None


def _proven(matrix: Matrix, votes: Sequence[_Pairs]) -> bool:
    return _plausible(matrix) and all(_share(matrix, pairs) >= _FILTER_SHARE for pairs in votes)


def remove(image: Image.Image, matrix: Matrix) -> Image.Image:
    """*image* with the colours *matrix* produced restored to the drawn ones."""
    inverse = _inverse(matrix)
    if inverse is None:
        raise ValueError("A filter matrix must be invertible.")
    coefficients = tuple(value for row in inverse for value in (*row, 0.0))
    return image.convert("RGB", matrix=coefficients)


def _pairs(reference: Reference) -> _Pairs:
    """Distinct informative (drawn, seen) colour pairs with pixel counts, on a regular grid."""
    drawn, seen = reference.drawn.convert("RGB"), reference.seen.convert("RGB")
    if drawn.size != seen.size:
        raise ValueError("A reference needs equally sized images.")
    width, height = drawn.size
    step = max(1, math.ceil(math.sqrt(width * height / _MAX_SAMPLES)))
    if step > 1:
        size = (max(1, width // step), max(1, height // step))
        drawn = drawn.resize(size, Image.Resampling.NEAREST)
        seen = seen.resize(size, Image.Resampling.NEAREST)
    a, b = drawn.tobytes(), seen.tobytes()
    counts = Counter(zip(a[0::3], a[1::3], a[2::3], b[0::3], b[1::3], b[2::3], strict=True))
    pairs = [
        ((key[0], key[1], key[2]), (key[3], key[4], key[5]), count)
        for key, count in counts.items()
        if max(key[0], key[1], key[2]) >= _INFORMATIVE
    ]
    if len(pairs) > _MAX_PAIRS:
        pairs.sort()
        pairs = pairs[:: math.ceil(len(pairs) / _MAX_PAIRS)]
    return pairs


def _weight(pairs: _Pairs) -> int:
    return sum(count for _, _, count in pairs)


def _error(matrix: Matrix, drawn: _Color, seen: _Color) -> float:
    return max(
        abs(row[0] * drawn[0] + row[1] * drawn[1] + row[2] * drawn[2] - value)
        for row, value in zip(matrix, seen, strict=True)
    )


def _share(matrix: Matrix, pairs: _Pairs) -> float:
    """The share of informative pixels that *matrix* maps onto their captured colour."""
    total = _weight(pairs)
    if not total:
        return 0.0
    matched = sum(
        count for drawn, seen, count in pairs if _error(matrix, drawn, seen) <= _TOLERANCE
    )
    return matched / total


def _fit(pairs: _Pairs, start: Matrix | None = None) -> Matrix | None:
    """A least-squares matrix over the pairs it explains, trimming the others step by step.

    From *start*, it refits once over the pairs *start* explains.

    ``None`` when it explains too few distinct colours, or colours that do not
    span enough of the colour space to determine all nine coefficients, for
    example a window of grays only.
    """
    matrix = start
    for limit in _FIT_LIMITS if start is None else (_TOLERANCE,):
        chosen = (
            pairs
            if matrix is None
            else [pair for pair in pairs if _error(matrix, pair[0], pair[1]) <= limit]
        )
        gram = [[0.0] * 3 for _ in range(3)]
        sums = [[0.0] * 3 for _ in range(3)]
        for drawn, seen, count in chosen:
            for i in range(3):
                weighted = drawn[i] * count
                for j in range(3):
                    gram[i][j] += weighted * drawn[j]
                    sums[j][i] += weighted * seen[j]
        inverse = _inverse(_freeze(gram))
        if inverse is None:
            return None
        matrix = _freeze(
            [
                [sum(inverse[i][k] * sums[c][k] for k in range(3)) for i in range(3)]
                for c in range(3)
            ]
        )
    if matrix is None:
        return None
    explained = {drawn for drawn, seen, _ in pairs if _error(matrix, drawn, seen) <= _TOLERANCE}
    if len(explained) < _FIT_COLORS:
        return None
    spread = [
        [sum(color[i] * color[j] for color in explained) / 65025 for j in range(3)]
        for i in range(3)
    ]
    return matrix if _smallest_eigenvalue(spread) >= _MIN_SPREAD else None


def _plausible(matrix: Matrix) -> bool:
    """An invertible filter that dims colours and visibly differs from no filter."""
    if _inverse(matrix) is None or abs(_determinant(matrix)) < 1e-3:
        return False
    if any(row[c] <= 0 or not 0.1 <= sum(row) <= 1.02 for c, row in enumerate(matrix)):
        return False
    return any(
        abs(value - (1.0 if c == k else 0.0)) * 255 >= _TOLERANCE
        for c, row in enumerate(matrix)
        for k, value in enumerate(row)
    )


def _freeze(rows: Sequence[Sequence[float]]) -> Matrix:
    return tuple((float(row[0]), float(row[1]), float(row[2])) for row in rows)


def _determinant(m: Sequence[Sequence[float]]) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _inverse(m: Matrix) -> Matrix | None:
    determinant = _determinant(m)
    scale = max(abs(value) for row in m for value in row)
    if not scale or abs(determinant) <= 1e-12 * scale**3:
        return None
    return _freeze(
        [
            [
                (
                    m[(j + 1) % 3][(i + 1) % 3] * m[(j + 2) % 3][(i + 2) % 3]
                    - m[(j + 1) % 3][(i + 2) % 3] * m[(j + 2) % 3][(i + 1) % 3]
                )
                / determinant
                for j in range(3)
            ]
            for i in range(3)
        ]
    )


def _smallest_eigenvalue(m: Sequence[Sequence[float]]) -> float:
    """The smallest eigenvalue of a symmetric 3x3 matrix (trigonometric closed form)."""
    off = m[0][1] ** 2 + m[0][2] ** 2 + m[1][2] ** 2
    q = (m[0][0] + m[1][1] + m[2][2]) / 3
    if off == 0:
        return min(m[0][0], m[1][1], m[2][2])
    p = math.sqrt(((m[0][0] - q) ** 2 + (m[1][1] - q) ** 2 + (m[2][2] - q) ** 2 + 2 * off) / 6)
    shifted = [[(m[i][j] - (q if i == j else 0.0)) / p for j in range(3)] for i in range(3)]
    r = max(-1.0, min(1.0, _determinant(shifted) / 2))
    return q + 2 * p * math.cos(math.acos(r) / 3 + 2 * math.pi / 3)


__all__ = ["IDENTITY", "Matrix", "Reference", "measure", "remove"]
