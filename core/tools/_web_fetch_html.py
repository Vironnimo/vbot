"""Readable HTML extraction and Markdown rendering."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString, PageElement

_STRIP_TAGS: frozenset[str] = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "object",
        "embed",
        "canvas",
        "map",
        "source",
        "template",
    }
)


_JUNK_PATTERNS: re.Pattern[str] = re.compile(
    r"cookie[-_]?(?:bar|banner|consent|notice|popup|overlay)"
    r"|gdpr|consent[-_]?(?:bar|banner|modal)"
    r"|ad[-_]?(?:banner|slot|wrapper|container|unit)"
    r"|popup[-_]?overlay|modal[-_]?backdrop"
    r"|newsletter[-_]?(?:signup|popup|modal)",
    re.IGNORECASE,
)


_MULTI_SPACE: re.Pattern[str] = re.compile(r"[ \t]+")


_MULTI_NEWLINE: re.Pattern[str] = re.compile(r"\n{3,}")


_BLOCK_NAMES: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "section",
        "blockquote",
        "figcaption",
        "dt",
        "dd",
        "header",
        "footer",
        "nav",
        "aside",
        "main",
        "article",
        "figure",
    }
)


_HEADING_NAMES: frozenset[str] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


_CHILD_BLOCK_NAMES: frozenset[str] = (
    _BLOCK_NAMES | _HEADING_NAMES | frozenset({"ul", "ol", "table", "pre"})
)


_SELF_RENDERED_TAGS: frozenset[str] = _HEADING_NAMES | frozenset({"a", "img", "pre", "tr", "li"})


# Link targets that must never surface as Markdown links: fragment-only and
# javascript: URIs. URL schemes are case-insensitive, so the prefix check is
# lower-cased — a mixed-case "JaVaScRiPt:" would otherwise slip through.
_SKIP_LINK_PREFIXES: tuple[str, ...] = ("#", "javascript:")


def _attr_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(str(part) for part in value)
    return str(value)


def _is_live(node: object) -> bool:
    """Return True if a tag has not been decomposed or detached."""
    return isinstance(node, Tag) and node.attrs is not None


def _has_junk_attr(node: object) -> bool:
    """Return True if a tag class/id matches known junk patterns."""
    if not isinstance(node, Tag):
        return False

    for attr in ("class", "id"):
        text = _attr_to_text(node.get(attr)).strip()
        if not text:
            continue
        if _JUNK_PATTERNS.search(text):
            return True
    return False


def _strip_noise(root: BeautifulSoup | Tag) -> None:
    """Remove non-text elements from the soup tree in-place."""
    for comment in list(root.find_all(string=lambda t: isinstance(t, Comment))):
        comment.extract()

    for tag_name in _STRIP_TAGS:
        for tag in list(root.find_all(tag_name)):
            if _is_live(tag):
                tag.decompose()

    for tag in list(root.select("[hidden]")):
        if _is_live(tag):
            tag.decompose()
    for tag in list(root.select('[aria-hidden="true"]')):
        if _is_live(tag):
            tag.decompose()

    junk = [node for node in root.find_all(True) if _is_live(node) and _has_junk_attr(node)]
    for tag in junk:
        if _is_live(tag):
            tag.decompose()


def _heading_prefix(tag_name: str) -> str:
    """Map h1-h6 to markdown-style heading prefixes."""
    if tag_name and tag_name[0] == "h" and len(tag_name) == 2 and tag_name[1].isdigit():
        level = int(tag_name[1])
        if 1 <= level <= 6:
            return "#" * level + " "
    return ""


def _render_inline(node: Tag, base_url: str, include_links: bool) -> str:
    """Render inline content preserving links, code, images, and breaks."""
    parts: list[str] = []

    for child in node.children:
        if isinstance(child, NavigableString):
            text = str(child).replace("\r", "").replace("\n", " ")
            text = _MULTI_SPACE.sub(" ", text)
            parts.append(text)
            continue

        if not isinstance(child, Tag):
            continue

        child_name = child.name

        if child_name == "a":
            inner = _render_inline(child, base_url, include_links).strip()
            if not include_links:
                if inner:
                    parts.append(inner)
                continue
            href = _attr_to_text(child.get("href", "")).strip()
            if href and not href.lower().startswith(_SKIP_LINK_PREFIXES):
                absolute = urljoin(base_url, href)
                if inner and inner != absolute:
                    parts.append(f"[{inner}]({absolute})")
                elif inner:
                    parts.append(inner)
                else:
                    parts.append(absolute)
            elif inner:
                parts.append(inner)
            continue

        if child_name == "code":
            if child.parent and child.parent.name == "pre":
                continue
            inner = child.get_text().strip()
            if inner:
                parts.append(f"`{inner}`")
            continue

        if child_name == "img":
            alt = _attr_to_text(child.get("alt", "")).strip()
            src = _attr_to_text(child.get("src", "")).strip()
            if alt and (src or not include_links):
                if include_links and src:
                    parts.append(f"![{alt}]({urljoin(base_url, src)})")
                else:
                    parts.append(alt)
            continue

        if child_name == "br":
            parts.append("\n")
            continue

        if child_name in _STRIP_TAGS:
            continue

        parts.append(_render_inline(child, base_url, include_links))

    result = "".join(parts)
    result = _MULTI_SPACE.sub(" ", result)
    return result.strip()


def _render_inline_oneline(node: Tag, base_url: str, include_links: bool) -> str:
    """Render inline content on one line by collapsing line breaks."""
    text = _render_inline(node, base_url, include_links).replace("\n", " ")
    return _MULTI_SPACE.sub(" ", text).strip()


def _render_tag(tag: Tag, base_url: str, include_links: bool) -> str:
    """Render a single tag to plain text with lightweight markdown hints."""
    name = tag.name

    if name in _HEADING_NAMES:
        text = _render_inline_oneline(tag, base_url, include_links)
        return f"\n\n{_heading_prefix(name)}{text}\n" if text else ""

    if name == "a":
        inner = _render_inline(tag, base_url, include_links)
        if not include_links:
            return inner or ""
        href = _attr_to_text(tag.get("href", "")).strip()
        if href and not href.lower().startswith(_SKIP_LINK_PREFIXES):
            absolute = urljoin(base_url, href)
            if inner and inner != absolute:
                return f"[{inner}]({absolute})"
            if inner:
                return inner
            return absolute
        return inner or ""

    if name == "img":
        alt = _attr_to_text(tag.get("alt", "")).strip()
        src = _attr_to_text(tag.get("src", "")).strip()
        if alt and include_links and src:
            return f"![{alt}]({urljoin(base_url, src)})"
        if alt:
            return alt
        return ""

    if name == "pre":
        code = tag.get_text()
        return f"\n\n```\n{code.strip()}\n```\n" if code.strip() else ""

    if name == "code":
        if tag.parent and tag.parent.name == "pre":
            return ""
        text = tag.get_text()
        return f"`{text.strip()}`" if text.strip() else ""

    if name == "li":
        text = _render_inline_oneline(tag, base_url, include_links)
        return f"\n- {text}" if text else ""

    if name == "tr":
        cells = tag.find_all(["td", "th"], recursive=False)
        rendered = [
            _render_inline_oneline(cell, base_url, include_links)
            for cell in cells
            if isinstance(cell, Tag)
        ]
        if any(rendered_cell for rendered_cell in rendered):
            return f"\n| {' | '.join(rendered)} |"
        return ""

    if name in _BLOCK_NAMES:
        text = _render_inline(tag, base_url, include_links)
        if text:
            prefix = "> " if name == "blockquote" else ""
            return f"\n\n{prefix}{text}"
        return ""

    if name == "hr":
        return "\n\n---\n"
    if name == "br":
        return "\n"

    return ""


def _tree_to_text(root: BeautifulSoup | Tag, base_url: str, include_links: bool) -> str:
    """Walk the DOM tree and produce clean text."""
    parts: list[str] = []

    def _walk(node: PageElement | BeautifulSoup) -> None:
        if isinstance(node, NavigableString):
            text = _MULTI_SPACE.sub(" ", str(node))
            if text.strip():
                parts.append(text)
            return

        if isinstance(node, BeautifulSoup):
            for child in node.children:
                if isinstance(child, (PageElement, BeautifulSoup)):
                    _walk(child)
            return

        if not isinstance(node, Tag):
            return

        name = node.name

        if name in _SELF_RENDERED_TAGS:
            rendered = _render_tag(node, base_url, include_links)
            if rendered:
                parts.append(rendered)
            return

        if name in _BLOCK_NAMES:
            has_child_blocks = any(
                isinstance(child, Tag) and child.name in _CHILD_BLOCK_NAMES
                for child in node.children
            )
            if not has_child_blocks:
                rendered = _render_tag(node, base_url, include_links)
                if rendered:
                    parts.append(rendered)
                return
            for child in node.children:
                _walk(child)
            return

        if name == "code":
            rendered = _render_tag(node, base_url, include_links)
            if rendered:
                parts.append(rendered)
            return

        if name == "table":
            for row in node.find_all("tr"):
                if not isinstance(row, Tag):
                    continue
                if row.find_parent("table") is not node:
                    continue
                rendered = _render_tag(row, base_url, include_links)
                if rendered:
                    parts.append(rendered)
            parts.append("\n")
            return

        if name in ("ul", "ol"):
            for list_item in node.find_all("li", recursive=False):
                if not isinstance(list_item, Tag):
                    continue
                rendered = _render_tag(list_item, base_url, include_links)
                if rendered:
                    parts.append(rendered)
            parts.append("\n")
            return

        if name in ("hr", "br"):
            rendered = _render_tag(node, base_url, include_links)
            if rendered:
                parts.append(rendered)
            return

        for child in node.children:
            _walk(child)

    _walk(root)
    return "".join(parts)


def _extract_metadata(soup: BeautifulSoup) -> dict[str, str]:
    """Extract title and description metadata from the document."""
    metadata: dict[str, str] = {}

    title_tag = soup.find("title")
    if isinstance(title_tag, Tag):
        title = title_tag.get_text(strip=True)
        if title:
            metadata["title"] = title

    if "title" not in metadata:
        og_title_tag = soup.find("meta", attrs={"property": "og:title"})
        if isinstance(og_title_tag, Tag):
            og_title = _attr_to_text(og_title_tag.get("content", "")).strip()
            if og_title:
                metadata["title"] = og_title

    description = ""
    description_tag = soup.find("meta", attrs={"name": "description"})
    if isinstance(description_tag, Tag):
        description = _attr_to_text(description_tag.get("content", "")).strip()

    if not description:
        og_description_tag = soup.find("meta", attrs={"property": "og:description"})
        if isinstance(og_description_tag, Tag):
            og_description = _attr_to_text(og_description_tag.get("content", "")).strip()
            if og_description:
                description = og_description

    if description:
        metadata["description"] = description

    return metadata


def extract_content(html: str, url: str, include_links: bool = True) -> tuple[str, dict[str, str]]:
    """Convert HTML to clean text while preserving textual information."""
    soup = BeautifulSoup(html, "html.parser")
    metadata = _extract_metadata(soup)

    body_candidate = soup.find("body")
    body: BeautifulSoup | Tag = body_candidate if isinstance(body_candidate, Tag) else soup
    _strip_noise(body)

    text = _tree_to_text(body, url, include_links)
    text = _MULTI_NEWLINE.sub("\n\n", text).strip()

    return text, metadata
