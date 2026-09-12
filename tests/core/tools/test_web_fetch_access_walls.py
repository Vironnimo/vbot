"""Web fetch: access walls behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.tools.web_fetch import (
    _FetchResult,
)
from tests.core.tools.web_fetch_helpers import (
    assert_failure_envelope,
    assert_success_envelope,
    install_http_get,
    make_context,
    make_result,
    web_fetch_arguments,
    web_fetch_handler,
)
from tests.core.tools.web_fetch_helpers import (
    stub_dns_resolution as stub_dns_resolution,
)
from tests.core.tools.web_fetch_helpers import (
    stub_http_session as stub_http_session,
)


@pytest.mark.asyncio
async def test_web_fetch_handler_reddit_challenge_page_signals_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://www.reddit.com/r/Python/"
    html = """
    <html>
      <head><title>Reddit - Prove your humanity</title></head>
      <body>
        <h1>Prove your humanity</h1>
        <p>Complete the challenge below and let us know you're a real person.</p>
      </body>
    </html>
    """

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False
    assert "web_search" in error["message"]


@pytest.mark.asyncio
async def test_web_fetch_handler_reddit_login_wall_signals_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    requested = "https://old.reddit.com/r/Python/"
    login_url = (
        "https://old.reddit.com/login/?reason=lor2"
        "&dest=https%3A%2F%2Fold.reddit.com%2Fr%2FPython%2F"
    )
    login_html = """
    <html>
      <head><title>Welcome to Reddit</title></head>
      <body>
        <p>Log in or sign up to personalize your feed.</p>
      </body>
    </html>
    """

    def responder(request_url: str) -> _FetchResult:
        if request_url == requested:
            return make_result(
                status_code=302,
                headers={"Location": login_url},
                url=request_url,
            )
        return make_result(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=login_html,
            url=login_url,
        )

    install_http_get(monkeypatch, responder)

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(requested))

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False
    assert "web_search" in error["message"]


@pytest.mark.asyncio
async def test_web_fetch_handler_challenge_title_signals_not_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/guarded"
    html = """
    <html>
      <head><title>Just a moment...</title></head>
      <body><p>Verifying you are human.</p></body>
    </html>
    """

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    error = assert_failure_envelope(result, "request_error")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_web_fetch_handler_challenge_page_raw_output_returns_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://www.reddit.com/r/Python/"
    html = "<html><head><title>Reddit - Prove your humanity</title></head></html>"

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200, headers={"Content-Type": "text/html"}, text=html, url=url
        ),
    )

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments(url, "raw"),
    )

    data = assert_success_envelope(result)
    assert data["content"] == html


@pytest.mark.asyncio
async def test_web_fetch_handler_similar_title_is_not_a_challenge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/essay"
    html = """
    <html>
      <head><title>Just a moment of joy</title></head>
      <body><p>An essay about patience.</p></body>
    </html>
    """

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    assert isinstance(data["content"], str)
    assert "An essay about patience." in data["content"]


@pytest.mark.asyncio
async def test_web_fetch_handler_login_path_on_other_hosts_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://example.com/login/"
    html = """
    <html>
      <head><title>Sign in</title></head>
      <body><p>Welcome back. Enter your credentials.</p></body>
    </html>
    """

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    assert isinstance(data["content"], str)
    assert "Welcome back." in data["content"]


@pytest.mark.asyncio
async def test_web_fetch_handler_tweet_shaped_page_with_login_links_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = "https://x.com/ThePSF/status/2090064027216998893"
    html = """
    <html>
      <head>
        <title>Python Software Foundation on X: "#PyPI runs on zero cost" / X</title>
        <meta property="og:description" content="#PyPI runs on zero cost" />
      </head>
      <body>
        <a href="/i/jf/onboarding/web?mode=login">Log in</a>
        <a href="/i/jf/onboarding/web?mode=signup">Sign up</a>
        <p>#PyPI runs on zero cost</p>
      </body>
    </html>
    """

    install_http_get(
        monkeypatch,
        lambda _url: make_result(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=html,
            url=url,
        ),
    )

    result = await web_fetch_handler(make_context(workspace), web_fetch_arguments(url))

    data = assert_success_envelope(result)
    assert isinstance(data["content"], str)
    assert "#PyPI runs on zero cost" in data["content"]


@pytest.mark.asyncio
async def test_web_fetch_handler_validation_error_signals_not_retryable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = await web_fetch_handler(
        make_context(workspace),
        web_fetch_arguments("ftp://example.com"),
    )

    error = assert_failure_envelope(result, "validation_error")
    assert error["retryable"] is False
