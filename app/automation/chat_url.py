"""ChatGPT conversation links.

A resume is generated in a ChatGPT conversation, and the link to that
conversation is stored with the record. This module decides what counts as
such a link. It is a port of `packages/shared/src/chat-url.ts` and must agree
with it: the web app refuses any link this would not produce.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

CHAT_HOSTS = frozenset({"chatgpt.com", "chat.openai.com"})

# `/c/<id>`, optionally under a custom GPT or project: `/g/<slug>/c/<id>`.
# Conversation ids are UUID-like; slugs are letters, digits, `_` and `-`.
_CONVERSATION_PATH = re.compile(r"(/g/[A-Za-z0-9_-]{1,120})?/c/[A-Za-z0-9_-]{8,80}")
_CHAT_ID = re.compile(r"/c/([A-Za-z0-9_-]{8,80})$")

# What `String.prototype.trim` removes, so both sides trim the same input.
_JS_WHITESPACE = "\t\n\x0b\x0c\r \xa0                　﻿"


def _resolve_dot_segments(path: str) -> str:
    """Collapse `.` and `..` the way a browser's URL parser does."""
    output: list[str] = []
    segments = path.split("/")
    for index, segment in enumerate(segments):
        lowered = segment.lower()
        last = index == len(segments) - 1
        if lowered in ("..", ".%2e", "%2e.", "%2e%2e"):
            if len(output) > 1:
                output.pop()
            if last:
                output.append("")
        elif lowered in (".", "%2e"):
            if last:
                output.append("")
        else:
            output.append(segment)
    return "/".join(output)


def normalize_chat_url(value: str | None) -> str | None:
    """Reduce a URL to its canonical conversation link, or None.

    None for anything that is not a recognised conversation: the root page, a
    settings page, another host, a lookalike host. Callers treat None as "no
    conversation link", never as an error.
    """
    if not value:
        return None
    text = value.strip(_JS_WHITESPACE)
    if not text:
        return None

    try:
        parts = urlsplit(text)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        return None

    # https only. A downgraded link is not one we will hand back to a browser.
    if parts.scheme.lower() != "https":
        return None
    # No credentials smuggled into the authority, and no explicit port. A
    # browser drops ":443" for https, so that one spelling is still accepted.
    if parts.username or parts.password or "@" in parts.netloc:
        return None
    if port is not None and port != 443:
        return None
    if not hostname:
        return None

    host = hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    # Exact match, so "chatgpt.com.evil.example" is refused.
    if host not in CHAT_HOSTS:
        return None

    path = _resolve_dot_segments(parts.path.replace("\\", "/")).rstrip("/")
    if not _CONVERSATION_PATH.fullmatch(path):
        return None

    # Query and fragment dropped: they are UI state, not identity.
    return f"https://{host}{path}"


def chat_id_from_url(value: str | None) -> str | None:
    """The conversation id of a recognised conversation link, or None."""
    canonical = normalize_chat_url(value)
    if canonical is None:
        return None
    match = _CHAT_ID.search(canonical)
    return match.group(1) if match else None


def is_same_conversation(a: str | None, b: str | None) -> bool:
    """Do two links refer to the same conversation? Two unrecognised links never do."""
    left = normalize_chat_url(a)
    right = normalize_chat_url(b)
    return left is not None and left == right
