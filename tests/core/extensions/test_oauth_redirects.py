"""OAuthRedirects: a browser redirect completes the one sign-in awaiting its state, once."""

from __future__ import annotations

import asyncio

import pytest

from core.extensions.oauth_redirects import OAuthRedirects


@pytest.mark.asyncio
async def test_a_redirect_completes_its_pending_sign_in_once_and_only_before_expiry() -> None:
    now = [0.0]
    redirects = OAuthRedirects(ttl_seconds=10, clock=lambda: now[0])
    completed = redirects.expect("first")
    expiring = redirects.expect("second")

    with pytest.raises(ValueError, match="already awaited"):
        redirects.expect("first")
    assert redirects.deliver({"state": "unknown", "code": "x"}) is False
    assert redirects.deliver({"code": "x"}) is False
    assert redirects.deliver({"state": "first", "code": "abc"}) is True
    # The state is spent: a replayed redirect completes nothing.
    assert redirects.deliver({"state": "first", "code": "abc"}) is False
    assert await completed == {"state": "first", "code": "abc"}

    now[0] = 11
    assert redirects.deliver({"state": "second", "code": "late"}) is False
    await asyncio.sleep(0)
    assert expiring.cancelled()

    discarded = redirects.expect("third")
    redirects.discard("third")
    await asyncio.sleep(0)
    assert discarded.cancelled()
    assert redirects.deliver({"state": "third", "code": "abc"}) is False
