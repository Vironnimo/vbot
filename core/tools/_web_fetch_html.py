"""Conservative HTML cleanup and readable, structure-preserving extraction."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, Tag
from markdownify import MarkdownConverter

_NOISE = re.compile(
    r"^(?:cookie[-_]?(?:bar|banner|consent|notice|popup|overlay)"
    r"|consent[-_]?(?:bar|banner|modal)|ad[-_]?(?:banner|slot|wrapper|container|unit)"
    r"|popup[-_]?overlay|modal[-_]?backdrop|newsletter[-_]?(?:signup|popup|modal))$",
    re.IGNORECASE,
)
_HIDDEN = re.compile(
    ("(?:^|;)\\s*(?:display\\s*:\\s*none|visibility\\s*:\\s*hidden)\\s*(?:!important\\s*)?(?:;|$)"),
    re.I,
)
_STRIP = {"script", "style", "template", "head", "object", "embed"}
_MAX_HTML_CHARS = 4_000_000
_MAX_NODES = 100_000
_MAX_DEPTH = 200


def _attr(node: Tag, name: str) -> str:
    value = node.get(name, "")
    return " ".join(str(item) for item in value) if isinstance(value, list) else str(value or "")


def _safe_url(value: str, base: str) -> str:
    value = value.strip()
    if not value or value.startswith("#"):
        return ""
    absolute = urljoin(base, value)
    if urlparse(absolute).scheme.lower() not in {"https", "http", "mailto", "tel"}:
        return ""
    return absolute


class _Converter(MarkdownConverter):
    """Keep code fences safe and spanning table cells unambiguous."""

    def __init__(self, *, include_links: bool = True, **options: Any) -> None:
        super().__init__(**options)
        self.include_links = include_links

    def convert_pre(self, el, text, parent_tags):
        code = text.strip("\n")
        if not code:
            return ""
        fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", code)), default=3))
        language = ""
        for node in (el, el.find("code")):
            if isinstance(node, Tag):
                match = re.search(r"(?:language|lang)-([\w+-]+)", _attr(node, "class"))
                if match:
                    language = match[1]
                    break
        return f"\n\n{fence}{language}\n{code}\n{fence}\n\n"

    def convert_td(self, el, text, parent_tags):
        spans = [
            f"{key}={el[key]}" for key in ("rowspan", "colspan") if _attr(el, key) not in {"", "1"}
        ]
        label = f" [{', '.join(spans)}]" if spans else ""
        return " " + text.strip().replace("\n", " / ").replace("|", r"\|") + label + " |"

    convert_th = convert_td

    def convert_br(self, el, text, parent_tags):
        if "pre" in parent_tags:
            return "\n" + text
        return super().convert_br(el, text, parent_tags)

    def convert_img(self, el, text, parent_tags):
        if not self.include_links:
            return _attr(el, "alt")
        return super().convert_img(el, text, parent_tags)


def _metadata(soup: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}
    title = soup.select_one("head > title") or soup.find("title")
    if isinstance(title, Tag) and not title.find_parent("svg"):
        result["title"] = title.get_text(" ", strip=True)[:500]
    for key, selectors in {
        "title": ['meta[property="og:title"]'],
        "description": ['meta[name="description"]', 'meta[property="og:description"]'],
        "published": ['meta[property="article:published_time"]'],
        "author": ['meta[name="author"]'],
    }.items():
        for selector in selectors:
            tag = soup.select_one(selector)
            if isinstance(tag, Tag) and key not in result:
                value = _attr(tag, "content").strip()
                if value:
                    result[key] = value[:1000]
    return result


def _clean(soup: BeautifulSoup, base: str) -> None:
    # Token boundaries matter: thread/download containers and articles about
    # GDPR are content, not ad containers or consent dialogs.
    for node in list(soup.find_all(True)):
        if node.attrs is None:
            continue
        tokens = (_attr(node, "id") + " " + _attr(node, "class")).split()
        hidden = (
            node.has_attr("hidden")
            or _attr(node, "aria-hidden").lower() == "true"
            or _HIDDEN.search(_attr(node, "style"))
        )
        if node.name in _STRIP or hidden or any(_NOISE.fullmatch(token) for token in tokens):
            node.decompose()
            continue
        if node.name in {"canvas", "svg", "iframe"}:
            label = _attr(node, "aria-label") or _attr(node, "title")
            if not label:
                title = node.find("title")
                label = title.get_text(" ", strip=True) if isinstance(title, Tag) else ""
            if not label:
                label = node.get_text(" ", strip=True)[:1000]
            href = _safe_url(_attr(node, "src"), base) if node.name == "iframe" else ""
            node.replace_with(
                f" [Embedded {node.name}: {label or 'visual content'}"
                f"{': ' + href if href else ''}] "
            )
        elif node.name == "img":
            src = _attr(node, "src")
            if not src or src.lower().startswith("data:"):
                src = _attr(node, "data-src") or _attr(node, "data-lazy-src") or src
            if not src:
                src = (
                    (_attr(node, "srcset") or _attr(node, "data-srcset"))
                    .split(",")[0]
                    .strip()
                    .split(" ")[0]
                )
            node["src"] = _safe_url(src, base)
            node["alt"] = (_attr(node, "alt") or _attr(node, "title") or "Image")[:500]
            if not node["src"]:
                node.replace_with(f" [Image: {node['alt']}; embedded image omitted] ")
        elif node.name == "a":
            target = _safe_url(_attr(node, "href"), base)
            if target:
                node["href"] = target
                node.attrs.pop("title", None)
            else:
                node.attrs.pop("href", None)
    for comment in list(soup.find_all(string=lambda value: isinstance(value, Comment))):
        comment.extract()


def extract_views(
    html: str, url: str, include_links: bool = True
) -> tuple[str, str, dict[str, str], list[str]]:
    """Return main content, complete cleaned page, metadata, and limitations.

    Semantic main/article selection reduces navigation without making omitted
    sections inaccessible. No article scoring or LLM summary can discard facts.
    """
    warnings: list[str] = []
    if len(html) > _MAX_HTML_CHARS:
        warnings.append(
            "HTML exceeded the 4,000,000-character extraction limit; saved content is partial."
        )
        html = html[:_MAX_HTML_CHARS]
    soup = BeautifulSoup(html.lstrip("\ufeff \t\r\n"), "html.parser")
    metadata = _metadata(soup)
    base_tag = soup.find("base", href=True)
    base = _safe_url(_attr(base_tag, "href"), url) if isinstance(base_tag, Tag) else url
    base = base or url
    # Bound recursive conversion independently of the download limit.
    stack: list[tuple[Tag, int]] = [(soup, 0)]
    count = 0
    excessive = False
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > _MAX_DEPTH or count > _MAX_NODES:
            excessive = True
            break
        stack.extend((child, depth + 1) for child in node.children if isinstance(child, Tag))
    _clean(soup, base)
    body = soup.body or soup
    if excessive:
        text = body.get_text("\n", strip=True)
        return (
            text,
            text,
            metadata,
            warnings + [("Complex HTML: text retained, layout and link targets unavailable.")],
        )
    converter = _Converter(
        heading_style="ATX",
        bullets="-",
        strip=[] if include_links else ["a"],
        keep_inline_images_in=["td", "th", "h1", "h2"],
        escape_underscores=False,
        include_links=include_links,
    )
    page = str(converter.convert_soup(body)).strip()
    candidates = body.select("main, [role=main]")
    if not candidates:
        candidates = body.select("article")
    # Keep every top-level candidate (e.g. a discussion with multiple posts).
    candidate_ids = {id(node) for node in candidates}
    selected = [
        node
        for node in candidates
        if not any(id(parent) in candidate_ids for parent in node.parents)
    ]
    main = "\n\n".join(str(converter.convert_soup(node)).strip() for node in selected).strip()
    if not main:
        # Keep complementary content in the already-rendered page view.
        selectors = (
            "nav, header, footer, aside, [role=navigation], [role=banner], [role=contentinfo]"
        )
        for node in list(body.select(selectors)):
            if node.attrs is not None:
                node.decompose()
        main = str(converter.convert_soup(body)).strip() or page
    if "[Embedded " in page:
        warnings.append(
            "Embedded or interactive content is represented by labels/links, not rendered."
        )
    if main != page:
        warnings.append(
            "Main content shown; find also searches the other saved sections of the page."
        )
    return main, page, metadata, warnings


def extract_content(html: str, url: str, include_links: bool = True) -> tuple[str, dict[str, str]]:
    """Compatibility entry point returning the complete cleaned page."""
    _, page, metadata, _ = extract_views(html, url, include_links)
    return page, metadata
