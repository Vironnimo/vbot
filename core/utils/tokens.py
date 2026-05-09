"""Token estimation utilities.

Provides heuristic-based token counting for cases where a provider does
not report actual usage.  The estimate is deliberately conservative —
it uses a simple characters-per-token ratio and signals to consumers
that the number is approximate, not exact.

Usage::

    count, is_estimate = estimate_tokens("Hello, world!")
"""

import math

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> tuple[int, bool]:
    """Estimate the number of tokens in *text* using a character heuristic.

    Divides the character count by ``CHARS_PER_TOKEN`` (4 chars/token) and
    rounds up so that any remainder counts as a full token.

    Args:
        text: The string to estimate token count for.

    Returns:
        A ``(estimated_count, True)`` tuple where the boolean always
        signals that the count is an estimate, not a precise measurement.
    """
    if not text:
        return 0, True
    return math.ceil(len(text) / CHARS_PER_TOKEN), True
