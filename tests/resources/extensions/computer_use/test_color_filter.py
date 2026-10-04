"""Computer Use: removing a screen colour filter that desktop composition applied.

A reference pairs a window's own rendering (``drawn``) with the captured pixels at
the same place (``seen``). The filter is simulated exactly as composition applies
it: one 3x3 matrix on 8-bit sRGB values, rounded. The contract: a proven filter is
removed to within rounding, and every capture without proof stays unchanged.
"""

from __future__ import annotations

import random

import pytest
from PIL import Image, ImageChops

from resources.extensions.computer_use._color_filter import Matrix, Reference, measure, remove

# f.lux at 2900 K as measured on Windows 11 (2026-10).
FLUX: Matrix = (
    (0.8804, 0.0986, 0.0210),
    (-0.0121, 0.7197, 0.0194),
    (-0.0030, -0.0196, 0.4606),
)
# The same tool at a warmer setting: same kind of filter, different white point.
WARMER: Matrix = (
    (0.8500, 0.1200, 0.0300),
    (-0.0150, 0.6400, 0.0250),
    (-0.0040, -0.0250, 0.3500),
)


def patches(colors: list[tuple[int, int, int]], size: int = 6) -> Image.Image:
    columns = 32
    rows = -(-len(colors) // columns)
    image = Image.new("RGB", (columns * size, rows * size), (32, 32, 32))
    for index, color in enumerate(colors):
        x, y = (index % columns) * size, (index // columns) * size
        image.paste(color, (x, y, x + size, y + size))
    return image


def colorful(seed: int, count: int = 400) -> Image.Image:
    generator = random.Random(seed)
    colors = [(value, value, value) for value in range(0, 256, 8)]
    colors += [
        (generator.randrange(256), generator.randrange(256), generator.randrange(256))
        for _ in range(count - len(colors))
    ]
    return patches(colors)


def grays() -> Image.Image:
    return patches([(value, value, value) for value in range(0, 256, 4)] * 4)


def filtered(image: Image.Image, matrix: Matrix) -> Image.Image:
    return image.convert("RGB", matrix=tuple(v for row in matrix for v in (*row, 0.0)))


def covered(image: Image.Image, share: float, seed: int) -> Image.Image:
    """*image* with its top *share* replaced by unrelated content, like a window on top."""
    result = image.copy()
    height = round(image.height * share)
    result.paste(colorful(seed).resize((image.width, height)), (0, 0))
    return result


def max_error(first: Image.Image, second: Image.Image) -> int:
    extrema = ImageChops.difference(first, second).getextrema()
    return max(int(channel[1]) for channel in extrema if isinstance(channel, tuple))


def test_proven_filter_is_removed_to_rounding() -> None:
    window, taskbar = colorful(1), colorful(2, count=120)
    references = [
        Reference(window, covered(filtered(window, FLUX), 0.2, seed=3)),
        Reference(taskbar, filtered(taskbar, FLUX)),
    ]

    matrix = measure(references)

    assert matrix is not None
    # Colours no reference showed come back as exactly as the filter itself allows:
    # it clips and compresses some levels of saturated colours.
    unseen = colorful(4)
    assert (
        max_error(remove(filtered(unseen, FLUX), matrix), remove(filtered(unseen, FLUX), FLUX)) <= 1
    )
    generator = random.Random(7)
    moderate = patches(
        [
            (
                generator.randrange(40, 216),
                generator.randrange(40, 216),
                generator.randrange(40, 216),
            )
            for _ in range(800)
        ]
    )
    assert max_error(remove(filtered(moderate, FLUX), matrix), moderate) <= 1
    # A reference with too few colours for a fit confirms a matrix proven earlier.
    assert measure([Reference(grays(), filtered(grays(), FLUX))], known=matrix) == matrix


@pytest.mark.parametrize(
    ("references", "known"),
    [
        pytest.param([Reference(colorful(1), colorful(1))], FLUX, id="unchanged-capture"),
        pytest.param(
            [Reference(colorful(1), covered(colorful(1), 0.6, seed=5))],
            FLUX,
            id="unchanged-capture-mostly-covered",
        ),
        pytest.param(
            [
                Reference(colorful(1), filtered(colorful(1), FLUX)),
                Reference(colorful(2), colorful(2)),
            ],
            None,
            id="one-unchanged-reference-vetoes",
        ),
        pytest.param(
            [Reference(grays(), filtered(grays(), FLUX))], None, id="grays-cannot-prove-a-matrix"
        ),
        pytest.param(
            [Reference(grays(), filtered(grays(), WARMER))], FLUX, id="stale-matrix-not-confirmed"
        ),
        pytest.param([Reference(colorful(1), colorful(6))], FLUX, id="unrelated-content"),
        pytest.param(
            [
                Reference(
                    patches([(200, 40, 40), (40, 200, 40), (40, 40, 200), (220, 220, 60)] * 50),
                    patches([(150, 60, 30), (30, 120, 90), (60, 30, 110), (170, 150, 40)] * 50),
                )
            ],
            None,
            id="few-colours-fit-by-chance",
        ),
        pytest.param(
            [
                Reference(
                    colorful(1), filtered(colorful(1), ((1.1, 0, 0), (0, 1.1, 0), (0, 0, 1.1)))
                )
            ],
            None,
            id="brightening-is-no-filter",
        ),
        pytest.param(
            [
                Reference(
                    Image.new("RGB", (400, 300)), filtered(colorful(1), FLUX).resize((400, 300))
                )
            ],
            FLUX,
            id="window-drew-nothing",
        ),
    ],
)
def test_capture_without_proof_stays_unchanged(
    references: list[Reference], known: Matrix | None
) -> None:
    assert measure(references, known=known) is None
