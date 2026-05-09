"""Tests for token estimation utilities.

Verifies the 4-chars/token heuristic, including edge cases like empty
strings, exact divisions, remainders, and multi-byte (CJK) characters.
"""

from core.utils.tokens import estimate_tokens

# ----- Empty input -----


def test_estimate_tokens_returns_zero_for_empty_string():
    """An empty string produces a token estimate of 0."""
    # Arrange
    text = ""

    # Act
    count, is_estimate = estimate_tokens(text)

    # Assert
    assert count == 0
    assert is_estimate is True


# ----- Simple ASCII text -----


def test_estimate_tokens_simple_ascii_text():
    """Plain ASCII text is estimated by dividing character count by 4."""
    # Arrange
    text = "Hello, world!"  # 13 characters

    # Act
    count, is_estimate = estimate_tokens(text)

    # Assert
    assert count == 4  # ceil(13 / 4) = 4
    assert is_estimate is True


def test_estimate_tokens_always_returns_estimate_flag():
    """The boolean return value is always True, signalling an estimate."""
    # Arrange
    text = "abc"

    # Act
    _, is_estimate = estimate_tokens(text)

    # Assert
    assert is_estimate is True


# ----- Non-evenly-divisible text (rounds up) -----


def test_estimate_tokens_rounds_up_on_remainder():
    """A length not evenly divisible by 4 still rounds up."""
    # Arrange
    text = "a" * 5  # 5 chars → ceil(5/4) = 2 tokens

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 2


def test_estimate_tokens_exact_division():
    """A length evenly divisible by 4 produces an exact token count."""
    # Arrange
    text = "a" * 8  # 8 chars → 8/4 = 2 tokens

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 2


def test_estimate_tokens_one_char_rounds_up():
    """Even a single character counts as one token (ceil)."""
    # Arrange
    text = "x"  # 1 char → ceil(1/4) = 1 token

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 1


# ----- Unicode text (CJK characters) -----


def test_estimate_tokens_cjk_characters():
    """CJK characters are counted by Python str length (code points)."""
    # Arrange
    text = "你好世界"  # 4 CJK characters → ceil(4/4) = 1 token

    # Act
    count, is_estimate = estimate_tokens(text)

    # Assert
    assert count == 1
    assert is_estimate is True


def test_estimate_tokens_mixed_unicode_and_ascii():
    """Mixed Unicode and ASCII text is estimated by total character count."""
    # Arrange
    text = "Hello世界!"  # 8 characters → ceil(8/4) = 2 tokens

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 2


def test_estimate_tokens_emoji():
    """Emoji are counted by code-point length, not byte length."""
    # Arrange
    text = "🎉🎊"  # 2 characters → ceil(2/4) = 1 token

    # Act
    count, _ = estimate_tokens(text)

    # Assert
    assert count == 1
