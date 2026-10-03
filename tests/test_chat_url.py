"""ChatGPT conversation links: the cases of `packages/shared/src/chat-url.test.ts`."""

from __future__ import annotations

import pytest

from app.automation.chat_url import chat_id_from_url, is_same_conversation, normalize_chat_url

ID = "68d0f2a1-4b3c-8000-9d2e-1a2b3c4d5e6f"


def test_accepts_a_plain_conversation_link():
    assert normalize_chat_url(f"https://chatgpt.com/c/{ID}") == f"https://chatgpt.com/c/{ID}"


def test_accepts_the_legacy_host():
    assert normalize_chat_url(f"https://chat.openai.com/c/{ID}") == f"https://chat.openai.com/c/{ID}"


def test_accepts_a_conversation_inside_a_custom_gpt_or_project():
    url = f"https://chatgpt.com/g/g-p-abc123-resumes/c/{ID}"
    assert normalize_chat_url(url) == url


def test_drops_query_fragment_trailing_slash_and_www():
    assert normalize_chat_url(f"https://www.chatgpt.com/c/{ID}/?model=gpt-4o#bottom") == f"https://chatgpt.com/c/{ID}"


def test_tolerates_case_whitespace_and_the_default_port():
    # A browser's URL parser lowercases the scheme and host and drops ":443".
    assert normalize_chat_url(f"  HTTPS://ChatGPT.com:443/c/{ID}\n") == f"https://chatgpt.com/c/{ID}"


# Every one of these would otherwise be stored and rendered as a link in the
# dashboard, which is exactly what this function exists to prevent.
REFUSED = [
    ("the root page (no conversation yet)", "https://chatgpt.com/"),
    ("a non-conversation page", "https://chatgpt.com/settings"),
    ("plain http", f"http://chatgpt.com/c/{ID}"),
    ("a lookalike host", f"https://chatgpt.com.evil.example/c/{ID}"),
    ("a different host entirely", f"https://example.com/c/{ID}"),
    ("credentials in the authority", f"https://user:pass@chatgpt.com/c/{ID}"),
    ("an explicit port", f"https://chatgpt.com:8443/c/{ID}"),
    ("a javascript: URL", "javascript:alert(1)"),
    ("path traversal", "https://chatgpt.com/c/../../admin"),
    ("an implausibly short id", "https://chatgpt.com/c/x"),
    ("garbage", "not a url"),
    # Beyond the reference list.
    ("a host hidden behind a backslash", f"https://evil.example\\@chatgpt.com/c/{ID}"),
    ("a real host in front of ours", f"https://evil.example/@chatgpt.com/c/{ID}"),
    ("a subdomain", f"https://evil.chatgpt.com/c/{ID}"),
    ("traversal after a valid-looking path", f"https://chatgpt.com/c/{ID}/../../settings"),
    ("encoded traversal", f"https://chatgpt.com/c/{ID}/%2e%2e/%2E%2E/settings"),
    ("an id with a path separator", f"https://chatgpt.com/c/{ID}/extra"),
    ("an id with odd characters", "https://chatgpt.com/c/abcdefgh<script>"),
    ("a bad port", f"https://chatgpt.com:port/c/{ID}"),
    ("a share link", f"https://chatgpt.com/share/{ID}"),
]


@pytest.mark.parametrize(("label", "value"), REFUSED, ids=[label for label, _ in REFUSED])
def test_refuses(label: str, value: str):
    assert normalize_chat_url(value) is None


def test_traversal_that_lands_on_a_conversation_is_reduced_like_a_browser_does():
    assert normalize_chat_url(f"https://chatgpt.com/settings/../c/{ID}") == f"https://chatgpt.com/c/{ID}"


def test_absent_input_is_no_link_rather_than_an_error():
    assert normalize_chat_url(None) is None
    assert normalize_chat_url("") is None
    assert normalize_chat_url("   ") is None


def test_same_conversation_reached_two_ways():
    assert is_same_conversation(f"https://chatgpt.com/c/{ID}?x=1", f"https://www.chatgpt.com/c/{ID}/")


def test_never_the_same_when_either_side_is_unrecognised():
    # Two Nones are not "the same conversation".
    assert not is_same_conversation("https://chatgpt.com/", "https://chatgpt.com/")
    assert not is_same_conversation(None, None)
    assert not is_same_conversation(f"https://chatgpt.com/c/{ID}", None)
    assert not is_same_conversation(f"https://chatgpt.com/c/{ID}", f"https://chat.openai.com/c/{ID}")


def test_chat_id_from_url():
    assert chat_id_from_url(f"https://chatgpt.com/c/{ID}?model=x") == ID
    assert chat_id_from_url(f"https://chatgpt.com/g/g-p-abc123-resumes/c/{ID}/") == ID
    assert chat_id_from_url("https://chatgpt.com/") is None
    assert chat_id_from_url(f"https://example.com/c/{ID}") is None
    assert chat_id_from_url(None) is None
