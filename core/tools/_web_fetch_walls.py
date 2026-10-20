"""Pages that stand in for the requested page: bot checks, login walls and
script-only shells.

Some hosts answer HTTP 200 with a challenge or login page instead of content
(Reddit's DataDome check, Reddit's login redirect, Cloudflare's interstitial).
Reporting those as success would hand the Agent useless boilerplate disguised
as page text. Rules stay narrow and host-scoped on purpose: a generic heuristic
would flag real pages that merely link to a login (X profiles and tweets do
exactly that while carrying readable content).
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

WALL_GUIDANCE = "Try another source."

_CHALLENGE_TITLE_MARKERS: frozenset[str] = frozenset(
    {
        "just a moment...",
        "access denied",
        "verify you are human",
        "attention required! | cloudflare",
    }
)
_REDDIT_HOST_SUFFIX = "reddit.com"
_REDDIT_CHALLENGE_MARKER = "prove your humanity"
_REDDIT_LOGIN_PATH_PREFIX = "/login"
# A line demanding JavaScript, as script-only apps show without it: "You need to
# enable JavaScript to run this app." A page explaining how to enable it does not
# start a line that way.
_SCRIPT_DEMAND = re.compile(
    r"^[#>*\s]*(?:(?:you (?:need|have) to|please,?) )?(?:enable|turn on) javascript"
    r"|^[#>*\s]*(?:this (?:app|site|page|website) )?(?:requires|needs) javascript"
    r"|^[#>*\s]*javascript (?:is required|must be enabled)",
    re.I | re.M,
)


def bot_wall(url: str, metadata: dict[str, str], text: str) -> str | None:
    """Return a failure message when the page is a bot check or login wall.

    ``None`` means the page carries real content (or claims to). Matching is
    deliberately narrow — a host-scoped challenge marker, a host-scoped login
    path, one exact well-known challenge title — so pages that merely link to
    a login form are never flagged.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    title = metadata.get("title", "").strip().lower()

    generic_wall = title in {"access denied", "verify you are human"}
    wall_text = re.sub(r"^[#>*\s]+", "", text).strip().lower()
    wall_evidence = wall_text == title or bool(
        re.search(
            r"verify (?:that )?you are (?:a )?human|you (?:have been|are) blocked"
            r"|you (?:do not|don't) have permission|request (?:was )?blocked",
            wall_text,
        )
    )
    if (
        title in _CHALLENGE_TITLE_MARKERS
        and len(text) < 4000
        and (not generic_wall or (len(text) < 600 and wall_evidence))
    ):
        return f"Blocked by a bot check at {url}; no readable content. {WALL_GUIDANCE}"

    # A localized reCAPTCHA interstitial can answer HTTP 200. Require its title
    # and opening access-check sentence about this host, not a mere CAPTCHA
    # mention or an embedded widget on a page with useful content.
    if (
        len(text) < 4000
        and re.fullmatch(r"ihr browser wird geprüft\s*[-–—]\s*recaptcha", title)
        and re.match(
            rf"ihr browser wird geprüft,\s+bevor sie auf {re.escape(host)} zugreifen können",
            re.sub(r"^[#>*\s]+", "", wall_text.removeprefix(title)),
        )
    ):
        return f"Blocked by a bot check at {url}; no readable content. {WALL_GUIDANCE}"

    if host == _REDDIT_HOST_SUFFIX or host.endswith("." + _REDDIT_HOST_SUFFIX):
        if path.startswith(_REDDIT_LOGIN_PATH_PREFIX):
            return f"Blocked by a login wall at {url}; no readable content. {WALL_GUIDANCE}"
        if _REDDIT_CHALLENGE_MARKER in text.lower():
            return f"Blocked by a bot check at {url}; no readable content. {WALL_GUIDANCE}"

    return None


def is_script_shell(main: str) -> bool:
    """Whether a page's main view is empty, a JavaScript demand or a browser check."""
    return not main.strip() or (
        len(main) < 600
        and bool(
            _SCRIPT_DEMAND.search(main)
            or re.match(
                "checking your browser|verify (?:that )?you are (?:a )?human",
                re.sub(r"^[#>*\s]+", "", main),
                re.I,
            )
        )
    )


__all__ = ["WALL_GUIDANCE", "bot_wall", "is_script_shell"]
