"""Conservative HTML cleanup and readable, structure-preserving extraction."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment, Tag
from markdownify import MarkdownConverter

_NOISE = re.compile(
    r"^(?:cookie[-_]?(?:bar|banner|consent|notice|popup|overlay)"
    r"|consent[-_]?(?:bar|banner|modal)|ad[-_]?(?:banner|slot|wrapper|container|unit)"
    r"|popup[-_]?overlay|modal[-_]?backdrop|newsletter[-_]?(?:signup|popup|modal)"
    # MediaWiki's [edit] link after every section heading.
    r"|mw-editsection)$",
    re.IGNORECASE,
)
# Inline styles that hide an element's text from a reader. Transparent text is
# a colored swatch (Wikipedia's mini periodic table in every element infobox:
# 118 links whose names are invisible).
_HIDDEN = re.compile(
    "(?:^|;)\\s*(?:display\\s*:\\s*none|visibility\\s*:\\s*hidden|color\\s*:\\s*transparent)"
    "\\s*(?:!important\\s*)?(?:;|$)",
    re.I,
)
# Images this small are icons (badges, protection locks, bullets): their address
# costs more than the image tells. They keep their alt text.
_ICON_PIXELS = 32
# Address parts that only pick an image's rendered size, format or cache version
# (Shopify's width and v, WordPress's -300x200 suffix, image CDNs' w, q and dpr):
# the same image in another size is shown once per view.
_IMAGE_SIZE_PARAMETERS = frozenset(
    {
        "w",
        "h",
        "width",
        "height",
        "size",
        "format",
        "fm",
        "q",
        "quality",
        "v",
        "ver",
        "version",
        "crop",
        "fit",
        "dpr",
        "auto",
        "resize",
        "scale",
    }
)
_IMAGE_SIZE_SUFFIX = re.compile(r"-[0-9]+x[0-9]+(?=\.\w+$)")
# Superscript and subscript forms of the characters Unicode has them for.
_SUPERSCRIPT = str.maketrans("0123456789+-−–=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁻⁼⁽⁾")
_SUBSCRIPT = str.maketrans("0123456789+-−–=()", "₀₁₂₃₄₅₆₇₈₉₊₋₋₋₌₍₎")
_SCRIPT_CHARACTERS = frozenset("0123456789+-−–=()")
# Raised ordinal suffixes, which read as plain text after their number (1st, 2ème).
# A single raised letter after a number stays marked: it is often a footnote.
_ORDINAL_SUFFIXES = frozenset({"st", "nd", "rd", "th", "er", "re", "ème", "eme"})
_STRIP = {"script", "style", "template", "head", "object", "embed"}
# Navigation is never the main content, also where a site puts it inside its
# main landmark (Wikipedia's page toolbar, navboxes). Header, footer and aside
# inside it are the content's own title, byline and notes, so only a page
# without main markup loses them.
_NAVIGATION = "nav, [role=navigation]"
_PAGE_FURNITURE = f"{_NAVIGATION}, header, footer, aside, [role=banner], [role=contentinfo]"
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
    parts = urlsplit(absolute)
    if parts.scheme.lower() not in {"https", "http", "mailto", "tel"}:
        return ""
    if "utm_" in parts.query:
        # Campaign tracking (Wikipedia tags every image address with three).
        query = [pair for pair in parts.query.split("&") if not pair.startswith("utm_")]
        absolute = urlunsplit(parts._replace(query="&".join(query)))
    return absolute


def _is_icon(node: Tag) -> bool:
    sizes = [_attr(node, "width"), _attr(node, "height")]
    return all(size.isdigit() and int(size) <= _ICON_PIXELS for size in sizes)


def _image_key(src: str) -> str:
    """Return what names an image apart from its rendered size and cache version."""
    parts = urlsplit(src)
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _IMAGE_SIZE_PARAMETERS
    )
    path = _IMAGE_SIZE_SUFFIX.sub("", parts.path)
    return f"{parts.netloc.lower()}{path}?{urlencode(query)}"


def _script(node: Tag, text: str, translation: dict[int, str], mark: str) -> str:
    """Render a superscript or subscript so ``10<sup>5</sup>`` does not read as 105.

    Digits and signs take their Unicode forms (``10⁵``, ``²⁸⁹Fl``, ``H₂O``). Other
    content is marked (``10^n``, ``T_c``), except references and footnote links,
    footnote symbols, and an ordinal suffix after a number (``1<sup>st</sup>``).
    """
    plain = node.get_text()
    stripped = plain.strip()
    if not stripped or node.find("a") is not None:
        return text
    if all(character in _SCRIPT_CHARACTERS for character in stripped):
        return plain.translate(translation)
    if not any(character.isalnum() for character in stripped):
        return text
    before = node.previous_sibling
    if (
        mark == "^"
        and stripped.lower() in _ORDINAL_SUFFIXES
        and isinstance(before, str)
        and before[-1:].isdigit()
    ):
        return text
    body = text.strip()
    return f"{mark}{body}" if len(body) == 1 else f"{mark}({body})"


class _Converter(MarkdownConverter):
    """Keep code fences safe and spanning table cells unambiguous."""

    def __init__(self, *, include_links: bool = True, **options: Any) -> None:
        super().__init__(**options)
        self.include_links = include_links
        # The alt texts shown for each image of the view being converted.
        self._images: dict[str, set[str]] = {}

    def convert_view(self, nodes: list[Tag]) -> str:
        """Convert ``nodes`` as one view, which shows each image's address once."""
        self._images = {}
        return "\n\n".join(str(self.convert_soup(node)).strip() for node in nodes).strip()

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

    def convert_a(self, el, text, parent_tags):
        # A linked image (a thumbnail linking to its file page or a larger copy)
        # keeps the image's own address; a second one tells the reader nothing.
        if el.find("img") is not None and not el.get_text(strip=True):
            return text
        return super().convert_a(el, text, parent_tags)

    def convert_img(self, el, text, parent_tags):
        alt = _attr(el, "alt")
        if not self.include_links or _is_icon(el):
            return alt
        # Shop pages repeat each product image in several sizes (Swarm run
        # 2026-10-20: 53 images, 26 different ones); a repeat shows only an alt
        # text not shown with it yet.
        key = _image_key(_attr(el, "src"))
        shown = self._images.get(key)
        if shown is not None:
            if alt in shown:
                return ""
            shown.add(alt)
            return alt
        rendered = super().convert_img(el, text, parent_tags)
        if rendered.startswith("!["):
            self._images[key] = {alt}
        return rendered

    def convert_sup(self, el, text, parent_tags):
        if "_noformat" in parent_tags:
            return text
        return _script(el, text, _SUPERSCRIPT, "^")

    def convert_sub(self, el, text, parent_tags):
        if "_noformat" in parent_tags:
            return text
        return _script(el, text, _SUBSCRIPT, "_")


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


def _site_path(target: str, page_url: str) -> str:
    """Return a link to the page's own site as a root-relative path.

    Most links of a page lead to its own site (an encyclopedia article holds
    hundreds), and the scheme and host repeated in each cost a large part of
    the page. The path resolves against the page's url as a browser would.
    """
    link, page = urlsplit(target), urlsplit(page_url)
    if (link.scheme.lower(), link.netloc.lower()) != (page.scheme.lower(), page.netloc.lower()):
        return target
    return urlunsplit(("", "", link.path or "/", link.query, link.fragment))


def _clean(soup: BeautifulSoup, base: str, page_url: str) -> None:
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
            described = _attr(node, "alt").strip()
            if not described and (
                _is_icon(node) or not (_attr(node, "title").strip() or node.find_parent("figure"))
            ):
                # A spacer, an unlabeled icon, or an undescribed image outside a
                # figure: on shop and gallery pages these are dozens of product
                # shots whose addresses cost more than they tell.
                node.decompose()
                continue
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
            src = _safe_url(src, base)
            node["src"] = src and _site_path(src, page_url)
            node["alt"] = (_attr(node, "alt") or _attr(node, "title") or "Image")[:500]
            node.attrs.pop("title", None)
            if not node["src"]:
                node.replace_with(f" [Image: {node['alt']}; embedded image omitted] ")
        elif node.name == "a":
            target = _safe_url(_attr(node, "href"), base)
            if target:
                node["href"] = _site_path(target, page_url)
                node.attrs.pop("title", None)
            else:
                node.attrs.pop("href", None)
    for comment in list(soup.find_all(string=lambda value: isinstance(value, Comment))):
        comment.extract()
    # A table left without text is layout (a grid of colored swatches), not data.
    for table in reversed(soup.find_all("table")):
        if not table.get_text(strip=True) and table.find("img") is None:
            table.decompose()


def _is_language_list(node: Tag) -> bool:
    """Whether a list only links to versions of the page in other languages.

    Such a list (Wikipedia's 200-odd interlanguage links sit in the article's
    title bar) marks every link with ``hreflang`` and holds no other text.
    """
    links = node.find_all("a")
    return (
        len(links) >= 2
        and all(link.has_attr("hreflang") for link in links)
        and node.get_text(strip=True) == "".join(link.get_text(strip=True) for link in links)
    )


def _drop_navigation(root: Tag, selectors: str) -> None:
    """Remove navigation and language lists from a main view in progress."""
    for node in list(root.select(selectors)):
        if node.attrs is not None:  # not already removed with an ancestor
            node.decompose()
    lists = {
        id(parent): parent
        for link in root.select("a[hreflang]")
        if (parent := link.find_parent(["ul", "ol", "menu"])) is not None
    }
    for node in lists.values():
        if node.attrs is not None and _is_language_list(node):
            node.decompose()


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
    _clean(soup, base, url)
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
        keep_inline_images_in=["td", "th", "h1", "h2", "a"],
        escape_underscores=False,
        include_links=include_links,
    )
    page = converter.convert_view([body])
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
    # The page view is already rendered, so what the main view drops stays
    # searchable there.
    for node in selected:
        _drop_navigation(node, _NAVIGATION)
    main = converter.convert_view(selected)
    if not main:
        _drop_navigation(body, _PAGE_FURNITURE)
        main = converter.convert_view([body]) or page
    if "[Embedded " in page:
        warnings.append(
            "Embedded or interactive content is represented by labels/links, not rendered."
        )
    if main != page:
        warnings.append(
            "Main content shown; find also searches the other saved sections of the page."
        )
    return main, page, metadata, warnings
