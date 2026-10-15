"""HTML from job APIs as readable text: paragraphs, headings and bullet lines.

Job board APIs hand the description over as HTML (sometimes entity-escaped
twice, as Greenhouse does). Parsed with the standard library; nothing in it is
ever executed or fetched.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK = {
    "p", "div", "section", "article", "header", "footer", "ul", "ol", "table", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dl", "dt", "dd",
}  # fmt: skip
_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "head"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n• ")
        elif tag in _BLOCK:
            self.out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n• *\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_text(value: str) -> str:
    """Text of an HTML fragment. Plain text passes through tidied."""
    if not value:
        return ""
    raw = value
    # Entity-escaped markup (&lt;p&gt;...) is unescaped first.
    if "&lt;" in raw and "<" not in raw[:2000]:
        raw = html.unescape(raw)
    if not re.search(r"<[a-zA-Z/!][^>]*>", raw):
        return tidy(html.unescape(raw))
    parser = _Text()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: fall back to stripping tags
        return tidy(html.unescape(re.sub(r"<[^>]+>", " ", raw)))
    return tidy("".join(parser.out))
