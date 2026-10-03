"""The ChatGPT bridge against local pages that reproduce ChatGPT's DOM.

The reader cases are the ones in the extension's `chatgpt-reader.test.ts`; the
rest drive `tests/fixtures/chatgpt/composer.html`, a stand-in for the composer
that behaves the way the live one was measured to.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import pytest

from app.automation import chatgpt
from app.automation.chatgpt import (
    Cancelled,
    ChatGPTBlocked,
    ChatGPTError,
    ReplyTimeout,
    contains_json_object,
    conversation_url,
    count_replies,
    detect_blocked,
    ensure_bridge,
    looks_like_complete_json,
    pasting_text,
    submit_prompt,
    wait_for_composer,
    wait_for_reply,
    waiting_text,
)
from app.browser.jsbridge import js_string, run_js, run_js_json
from tests import helpers

BRIDGE = "window.__RAI_BRIDGE__"
WAITING = re.compile(r"^Waiting for ChatGPT \(\d+ reply blocks?, [\d,]+ chars, Stop (shown|gone)\)$")
PASTING = re.compile(r"^Pasting prompt (\d+)/(\d+)…$")


async def load_fixture(page: Any, relative: str) -> bool:
    """Load a fixture page and mark it as shown.

    Chromium slows the timers of a hidden page to one tick a second. In the app
    the ChatGPT tab is on screen while it is driven, so the tests run that way too.
    """
    ok = await helpers.load_fixture(page, relative)
    page.setVisible(True)
    return ok


async def load_html(page: Any, html: str, base_url: str = "https://example.test/") -> bool:
    ok = await helpers.load_html(page, html, base_url)
    page.setVisible(True)
    return ok


def reply(inner: str) -> str:
    return f'<div data-message-author-role="assistant">{inner}</div>'


def unit(body: str) -> str:
    """ChatGPT's markup as measured live on 2026-09-26: no role attribute at all."""
    return (
        '<div data-content-search-unit-key="fallback-turn-0:2:assistant"'
        ' data-chatgpt-search-unit-key="fallback-turn-0:2:assistant">'
        '<h4 data-conversation-role="assistant">ChatGPT said:</h4>'
        f'<div data-markdown-text-style="assistant-message">{body}</div></div>'
    )


async def read_html(page: Any, body: str, baseline: int = 0) -> dict[str, Any]:
    await load_html(page, f"<!doctype html><title>t</title><body>{body}</body>")
    await ensure_bridge(page)
    return await run_js_json(page, f"{BRIDGE}.readLatest({baseline})")


async def read_file(page: Any, name: str, function: str = "readLatest", baseline: int = 0) -> dict[str, Any]:
    assert await load_fixture(page, f"chatgpt/{name}")
    await ensure_bridge(page)
    return await run_js_json(page, f"{BRIDGE}.{function}({baseline})")


async def state(page: Any) -> dict[str, Any]:
    return await run_js_json(page, "window.__state()")


def long_prompt(target: int = 30_000, marker: str = "PROMPT-MARKER-7391") -> str:
    """A prompt of about `target` characters in ordinary lines (none over the chunk limit)."""
    lines = [f"{marker} CANDIDATE: Jane Doe"]
    index = 0
    while sum(len(line) + 1 for line in lines) < target:
        index += 1
        lines.append(f"{index:05d} Built retrieval pipelines, evaluated models, and shipped them to production.")
    return "\n".join(lines)


@pytest.fixture
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the page four times as often. The completion rules (1.8 s, 3 s) are untouched."""
    monkeypatch.setattr(chatgpt, "POLL_SECONDS", 0.3)


# ---------------------------------------------------------------------------
# Reader: the cases of chatgpt-reader.test.ts
# ---------------------------------------------------------------------------


async def test_reads_a_json_code_block_verbatim(web_page):
    poll = await read_html(web_page, reply('<pre><code>{"a":1}</code></pre>'))
    assert poll["status"] == "ok"
    assert poll["text"] == '{"a":1}'


async def test_reads_json_written_as_plain_text(web_page):
    poll = await read_html(web_page, reply('<p>{</p><p>"institution": "UT Austin"</p><p>}</p>'))
    assert poll["status"] == "ok"
    assert '"institution": "UT Austin"' in poll["text"]


async def test_an_empty_code_element_does_not_hide_the_reply(web_page):
    poll = await read_html(web_page, reply('<pre><code></code></pre><p>{"a":1}</p>'))
    assert '{"a":1}' in poll["text"]


async def test_prefers_the_longest_code_block(web_page):
    poll = await read_html(web_page, reply('<pre><code>json</code></pre><pre><code>{"a":{"b":2}}</code></pre>'))
    assert poll["text"] == '{"a":{"b":2}}'


async def test_skips_the_extra_empty_reply_node(web_page):
    poll = await read_html(web_page, reply('<pre><code>{"a":1}</code></pre>') + reply(""))
    assert poll["text"] == '{"a":1}'


async def test_reads_the_surrounding_turn_when_the_reply_node_is_empty(web_page):
    body = f'<section data-turn="assistant">{reply("")}<div><pre><code>{{"a":1}}</code></pre></div></section>'
    poll = await read_html(web_page, body)
    assert poll["text"] == '{"a":1}'


async def test_says_how_much_it_sees(web_page):
    poll = await read_html(web_page, reply('<pre><code>{"a":1}</code></pre>'))
    assert poll["diag"] == "1 reply block(s), 7 chars, Stop gone"


async def test_new_layout_finds_the_reply_without_a_role_attribute(web_page):
    body = (
        '<div data-content-search-unit-key="fallback-turn-0:0:user">'
        '<div data-user-message-bubble="true">{"prompt":true}</div></div>' + unit('<p>{"a":1}</p>')
    )
    poll = await read_html(web_page, body)
    assert poll["status"] == "ok"
    assert poll["text"] == '{"a":1}'


async def test_new_layout_leaves_out_the_heading(web_page):
    poll = await read_html(web_page, unit('<p>{"a":1}</p>'))
    assert "ChatGPT said" not in poll["text"]


async def test_new_layout_keeps_an_autolinked_email_inside_its_string(web_page):
    body = unit(
        '<p><span>{</span><br><span>"email": "</span><a href="mailto:a@b.co"><span>a@b.co</span>'
        '<span data-markdown-copy="exclude" style="display:inline-block"><svg></svg></span></a>'
        '<span>",</span><br><span>"n": 1</span><br><span>}</span></p>'
    )
    poll = await read_html(web_page, body)
    assert json.loads(poll["text"]) == {"email": "a@b.co", "n": 1}


async def test_new_layout_reads_a_code_block_without_pre(web_page):
    body = unit(
        '<div data-markdown-copy="code-block"><div data-markdown-copy="exclude">json<button>Copy</button></div>'
        '<div aria-hidden="true"><code>{"a":1}</code></div></div>'
    )
    poll = await read_html(web_page, body)
    assert poll["text"] == '{"a":1}'


async def test_new_layout_takes_the_newest_reply(web_page):
    poll = await read_html(web_page, unit('<p>{"old":1}</p>') + unit('<p>{"new":2}</p>'))
    assert poll["text"] == '{"new":2}'


COMPLETE_JSON_CASES = [
    ('{"a":"}"}', True),
    ('```json\n{"a":[1,{"b":"x"}]}\n```', True),
    ('{"a":1}\n```\n  ', True),
    ('{"a":"quote \\" brace }"}', True),
    ('{"a":[1,2', False),
    ('{"a":"}', False),
    ('{"a":1} and more', False),
    ('{"a":1}}', False),
    ("no json", False),
    ("", False),
]


@pytest.mark.parametrize(("text", "expected"), COMPLETE_JSON_CASES)
def test_looks_like_complete_json(text: str, expected: bool):
    assert looks_like_complete_json(text) is expected


async def test_python_and_page_agree_on_complete_json(web_page):
    await load_html(web_page, "<p>x</p>")
    await ensure_bridge(web_page)
    for text, expected in COMPLETE_JSON_CASES:
        assert await run_js_json(web_page, f"{BRIDGE}.looksLikeCompleteJson({js_string(text)})") is expected


# ---------------------------------------------------------------------------
# Reader: the fixture pages
# ---------------------------------------------------------------------------


async def test_fixture_old_role_layout(web_page):
    poll = await read_file(web_page, "old_layout.html")
    assert json.loads(poll["text"]) == {"contact": {"fullName": "Jane Doe"}, "skills": ["Python", "SQL"]}
    assert poll["streaming"] is False
    assert poll["stopVisible"] is False
    # The user's own message and the empty extra node are not replies.
    assert await count_replies(web_page) == 1


async def test_fixture_search_unit_layout(web_page):
    poll = await read_file(web_page, "search_unit.html")
    assert "ChatGPT said" not in poll["text"]
    assert "You said" not in poll["text"]
    assert json.loads(poll["text"]) == {"institution": "UT Austin", "degree": "BSc"}
    # The nested unit is part of the outer one, not a second reply.
    assert poll["diag"].startswith("1 reply block(s), ")
    assert await count_replies(web_page) == 1


async def test_fixture_code_block_without_pre(web_page):
    poll = await read_file(web_page, "code_block_no_pre.html")
    assert poll["text"] == '{"a": 1, "list": [1, 2, {"b": "x"}]}'
    assert looks_like_complete_json(poll["text"])


async def test_fixture_autolinked_email(web_page):
    poll = await read_file(web_page, "email_autolink.html")
    assert json.loads(poll["text"]) == {"email": "a@b.co", "n": 1}
    assert '"email": "a@b.co",' in poll["text"]  # no newline inside the string
    assert "mail" not in poll["text"].replace("email", "")  # the icon's <title> is not read


async def test_fixture_visible_stop_button_means_still_answering(web_page):
    poll = await read_file(web_page, "stop_visible.html")
    assert poll["streaming"] is True
    assert poll["stopVisible"] is True
    assert poll["diag"].endswith("Stop shown")
    assert not looks_like_complete_json(poll["text"])


async def test_fixture_hidden_stop_and_stop_dictation_do_not_count(web_page):
    poll = await read_file(web_page, "stop_hidden.html")
    assert poll["text"] == '{"a":1}'
    assert poll["streaming"] is False
    assert poll["stopVisible"] is False
    assert poll["diag"] == "1 reply block(s), 7 chars, Stop gone"


async def test_read_beyond_ignores_replies_up_to_the_baseline(web_page):
    body = unit('<p>{"old":1}</p>') + unit('<p>{"new":2}</p>')
    await load_html(web_page, f"<body>{body}</body>")
    assert await count_replies(web_page) == 2
    beyond_one = await run_js_json(web_page, f"{BRIDGE}.readBeyond(1)")
    assert beyond_one["text"] == '{"new":2}'
    assert beyond_one["blocks"] == 2
    # Nothing new yet: no text, and it counts as "still answering" so the wait goes on.
    beyond_two = await run_js_json(web_page, f"{BRIDGE}.readBeyond(2)")
    assert beyond_two["text"] == ""
    assert beyond_two["streaming"] is True


# ---------------------------------------------------------------------------
# Completion rules
# ---------------------------------------------------------------------------


async def test_reply_is_accepted_once_stop_is_gone_and_the_text_is_stable(web_page, fast_polls):
    assert await load_fixture(web_page, "chatgpt/stop_hidden.html")
    await ensure_bridge(web_page)
    loop = asyncio.get_running_loop()
    seen: list[str] = []
    started = loop.time()
    text = await wait_for_reply(web_page, 0, timeout=20, on_progress=seen.append)
    elapsed = loop.time() - started
    assert text == '{"a":1}'
    assert elapsed >= chatgpt.STABLE_SECONDS
    assert seen == ["Waiting for ChatGPT (1 reply block, 7 chars, Stop gone)"]


async def test_reply_is_not_accepted_while_stop_shows_and_the_json_is_open(web_page, fast_polls):
    assert await load_fixture(web_page, "chatgpt/stop_visible.html")
    await ensure_bridge(web_page)
    seen: list[str] = []
    with pytest.raises(ReplyTimeout):
        await wait_for_reply(web_page, 0, timeout=4.5, on_progress=seen.append)
    assert seen and all(line.endswith("Stop shown)") for line in seen)


async def test_complete_json_unchanged_for_three_seconds_is_done_even_with_stop_shown(web_page, fast_polls):
    assert await load_fixture(web_page, "chatgpt/stop_visible.html")
    await ensure_bridge(web_page)
    await run_js(web_page, 'document.querySelector(\'code\').textContent = \'{"contact": {"fullName": "Jane"}}\'; true')
    loop = asyncio.get_running_loop()
    started = loop.time()
    text = await wait_for_reply(web_page, 0, timeout=20)
    assert json.loads(text) == {"contact": {"fullName": "Jane"}}
    assert loop.time() - started >= chatgpt.COMPLETE_JSON_QUIET_SECONDS


async def test_wait_for_reply_times_out_when_nothing_comes(web_page, fast_polls):
    await load_html(web_page, "<main></main>")
    await ensure_bridge(web_page)
    seen: list[str] = []
    with pytest.raises(ReplyTimeout):
        await wait_for_reply(web_page, 0, timeout=1.5, on_progress=seen.append)
    assert seen == ["Waiting for ChatGPT (0 reply blocks, 0 chars, Stop gone)"]


def test_status_lines():
    assert waiting_text(1, 8412, True) == "Waiting for ChatGPT (1 reply block, 8,412 chars, Stop shown)"
    assert waiting_text(2, 0, False) == "Waiting for ChatGPT (2 reply blocks, 0 chars, Stop gone)"
    assert pasting_text(3, 14) == "Pasting prompt 3/14…"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"contact": {"fullName": "Jane"}}', True),
        ('Here is the JSON:\n```json\n{"a": 1}\n```\nHope that helps.', True),
        ('{"a": 1,}', True),  # repairable: the web app removes the trailing comma
        ("{“a”: 1}", True),  # repairable: smart quotes
        ("I cannot help with that.", False),
        ("", False),
        ("{}", False),
        ("} nothing {", False),
        ("[1, 2, 3]", False),
        ('{"a": 1', False),
        ("see {this}", False),
    ],
)
def test_contains_json_object(text: str, expected: bool):
    assert contains_json_object(text) is expected


# ---------------------------------------------------------------------------
# Composer: paste, send, wait
# ---------------------------------------------------------------------------


async def test_long_prompt_is_pasted_in_chunks_sent_with_enter_and_answered(web_page, caplog):
    caplog.set_level(logging.DEBUG)
    assert await load_fixture(web_page, "chatgpt/composer.html")
    await wait_for_composer(web_page, timeout=10)

    prompt = long_prompt(30_000)
    assert 29_000 < len(prompt) < 31_000
    seen: list[str] = []

    baseline = await submit_prompt(web_page, prompt, on_progress=seen.append)
    assert baseline == 0

    after = await state(web_page)
    assert after["chips"] == 0, "no chunk may reach the 10,000-character attachment threshold"
    assert after["largestPaste"] <= 8000
    assert after["pastes"] >= 4
    assert after["sent"] == 1
    assert after["sentLengths"] == [len(prompt)], "nothing lost, nothing added"
    assert await run_js_json(web_page, f"window.__sentEquals(0, {js_string(prompt)})") is True
    assert (after["enterSends"], after["clickSends"]) == (1, 0)
    # The composer was empty, so it must not have been cleared (that drops the first paste).
    assert (after["selectAll"], after["deletes"], after["droppedPastes"]) == (0, 0, 0)

    pasting = [PASTING.match(line) for line in seen if line.startswith("Pasting")]
    assert pasting and all(pasting)
    chunks = int(pasting[-1].group(2))
    assert chunks == 4
    assert pasting[-1].group(1) == "4", "the last chunk is always reported"
    steps = [int(match.group(1)) for match in pasting]
    assert steps == sorted(steps)

    waiting: list[str] = []
    text = await wait_for_reply(web_page, baseline, timeout=30, on_progress=waiting.append)
    answer = json.loads(text)
    assert answer == {"ok": True, "turn": 1, "email": "a@b.co", "received": len(prompt)}
    assert waiting and all(WAITING.match(line) for line in waiting)
    assert waiting[-1] == f"Waiting for ChatGPT (1 reply block, {len(text):,} chars, Stop gone)"

    # Lengths and counts may be logged; the prompt and the reply never are.
    assert "PROMPT-MARKER-7391" not in caplog.text
    assert "a@b.co" not in caplog.text
    assert "retrieval pipelines" not in caplog.text


async def test_an_existing_draft_is_cleared_before_pasting(web_page):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    await run_js(web_page, "window.__setDraft('an old half-written draft'); true")
    prompt = long_prompt(9_000)

    await submit_prompt(web_page, prompt)

    after = await state(web_page)
    assert after["selectAll"] >= 1 and after["deletes"] >= 1
    assert after["droppedPastes"] == 0, "the 300 ms wait after clearing keeps the first paste"
    assert after["sentLengths"] == [len(prompt)]
    assert await run_js_json(web_page, f"window.__sentEquals(0, {js_string(prompt)})") is True


async def test_an_empty_composer_is_never_cleared(web_page):
    # The fixture drops a paste that follows selectAll + delete on an empty
    # composer, as the live page does. Had the bridge cleared it, the first
    # chunk would be missing from what was sent.
    assert await load_fixture(web_page, "chatgpt/composer.html")
    prompt = long_prompt(17_000)

    await submit_prompt(web_page, prompt)

    after = await state(web_page)
    assert (after["selectAll"], after["deletes"], after["droppedPastes"]) == (0, 0, 0)
    assert after["sentLengths"] == [len(prompt)]


async def test_a_swallowed_first_paste_is_noticed_and_pasted_again(web_page):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    await run_js(web_page, "window.__fixture.swallowFirstPaste = true; true")
    prompt = long_prompt(17_000)

    await submit_prompt(web_page, prompt)

    after = await state(web_page)
    assert after["swallowedPastes"] == 1
    assert after["chips"] == 0
    assert after["sentLengths"] == [len(prompt)]
    assert await run_js_json(web_page, f"window.__sentEquals(0, {js_string(prompt)})") is True


async def test_a_follow_up_reads_only_the_new_reply(web_page, fast_polls):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    await run_js(web_page, "window.__fixture.replyDelayMs = 200; true")

    first = "first prompt " * 20
    baseline = await submit_prompt(web_page, first)
    assert baseline == 0
    assert json.loads(await wait_for_reply(web_page, baseline, timeout=20))["turn"] == 1

    follow_up = "Return only the corrected JSON matching the schema; errors:\n- contact.fullName: Required"
    baseline = await submit_prompt(web_page, follow_up)
    assert baseline == 1, "the first reply is already on the page"
    answer = json.loads(await wait_for_reply(web_page, baseline, timeout=20))
    assert answer["turn"] == 2
    assert answer["received"] == len(follow_up)


async def test_an_unfinished_reply_is_not_returned_while_chatgpt_is_still_answering(web_page, fast_polls):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    await run_js(web_page, "Object.assign(window.__fixture, { replyDelayMs: 200, streamMs: 3000 }); true")

    baseline = await submit_prompt(web_page, "short prompt")
    seen: list[str] = []
    text = await wait_for_reply(web_page, baseline, timeout=30, on_progress=seen.append)

    assert json.loads(text)["received"] == len("short prompt")
    assert any(line.endswith("Stop shown)") for line in seen)
    assert seen[-1].endswith("Stop gone)")


async def test_a_finished_json_reply_is_returned_even_if_stop_never_goes_away(web_page, fast_polls):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    await run_js(web_page, "Object.assign(window.__fixture, { replyDelayMs: 200, keepStop: true }); true")

    baseline = await submit_prompt(web_page, "short prompt")
    seen: list[str] = []
    text = await wait_for_reply(web_page, baseline, timeout=30, on_progress=seen.append)

    assert json.loads(text)["ok"] is True
    assert seen[-1].endswith("Stop shown)")


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


async def test_cancel_aborts_the_wait_for_a_reply(web_page):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    await run_js(web_page, "window.__fixture.neverReply = true; true")
    baseline = await submit_prompt(web_page, "short prompt")

    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, cancel.set)
    started = loop.time()
    with pytest.raises(Cancelled):
        await wait_for_reply(web_page, baseline, timeout=60, cancel=cancel)
    assert loop.time() - started < 1.1, "Stop does not wait for the next poll"


async def test_cancel_set_beforehand_sends_nothing(web_page):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        await submit_prompt(web_page, "short prompt", cancel=cancel)
    with pytest.raises(Cancelled):
        await wait_for_reply(web_page, 0, timeout=60, cancel=cancel)
    with pytest.raises(Cancelled):
        await wait_for_composer(web_page, cancel=cancel)
    after = await state(web_page)
    assert (after["sent"], after["pastes"]) == (0, 0)


async def test_cancel_during_the_paste_stops_between_chunks_and_never_sends(web_page):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    prompt = long_prompt(300_000)
    cancel = asyncio.Event()
    seen: list[str] = []

    def on_progress(line: str) -> None:
        seen.append(line)
        match = PASTING.match(line)
        if match and int(match.group(1)) >= 2:
            cancel.set()

    with pytest.raises(Cancelled):
        await submit_prompt(web_page, prompt, on_progress=on_progress, cancel=cancel)

    after = await state(web_page)
    assert after["sent"] == 0
    assert 0 < after["length"] < len(prompt), "stopped part-way, at a chunk boundary"
    assert after["chips"] == 0


# ---------------------------------------------------------------------------
# Log-in and challenge pages
# ---------------------------------------------------------------------------


async def test_logged_out_page_blocks_with_login(web_page):
    assert await load_fixture(web_page, "chatgpt/logged_out.html")
    assert await detect_blocked(web_page) == "login"
    with pytest.raises(ChatGPTBlocked) as caught:
        await wait_for_composer(web_page, timeout=10)
    assert caught.value.reason == "login"
    assert isinstance(caught.value, ChatGPTError)
    assert "log in" in str(caught.value).lower()


async def test_challenge_page_blocks_with_challenge(web_page, monkeypatch):
    monkeypatch.setattr(chatgpt, "CHALLENGE_GRACE_SECONDS", 0.8)
    assert await load_fixture(web_page, "chatgpt/challenge.html")
    assert await detect_blocked(web_page) == "challenge"
    with pytest.raises(ChatGPTBlocked) as caught:
        await wait_for_composer(web_page, timeout=10)
    assert caught.value.reason == "challenge"


async def test_a_challenge_that_clears_by_itself_does_not_block(web_page):
    assert await load_fixture(web_page, "chatgpt/challenge.html")
    # Cloudflare's automatic check passes and the app loads, well inside the grace period.
    await run_js(
        web_page,
        "setTimeout(() => { document.title = 'ChatGPT'; document.body.innerHTML ="
        ' \'<form><div id="prompt-textarea" class="ProseMirror" contenteditable="true"><p><br></p></div></form>\';'
        " }, 700); true",
    )
    await wait_for_composer(web_page, timeout=10)
    assert await detect_blocked(web_page) is None


async def test_login_urls_block_whatever_the_page_shows(web_page):
    await load_html(web_page, "<p>Welcome back</p><input type=email>", base_url="https://auth.openai.com/log-in")
    assert await detect_blocked(web_page) == "login"
    await load_html(web_page, "<p>Welcome back</p>", base_url="https://chatgpt.com/auth/login?next=/")
    assert await detect_blocked(web_page) == "login"


async def test_signed_out_chat_with_a_composer_still_counts_as_login(web_page):
    # Signed-out ChatGPT offers an anonymous chat box next to "Log in". A resume must not be sent there.
    await load_html(
        web_page,
        '<header><button data-testid="login-button">Log in</button><a href="/auth/login">Sign up for free</a></header>'
        '<form><div id="prompt-textarea" class="ProseMirror" contenteditable="true"><p><br></p></div></form>',
    )
    assert await detect_blocked(web_page) == "login"
    with pytest.raises(ChatGPTBlocked):
        await wait_for_composer(web_page, timeout=10)


async def test_a_signed_in_page_is_not_blocked(web_page):
    assert await load_fixture(web_page, "chatgpt/composer.html")
    assert await detect_blocked(web_page) is None
    await wait_for_composer(web_page, timeout=10)

    # Words in a reply, a hidden button, or a chat titled "Log in" in the sidebar are not a log-in page.
    await load_html(
        web_page,
        reply("<p>Click <button>Log in</button> then verify you are human.</p>")
        + '<div style="display:none"><button data-testid="login-button">Log in</button></div>'
        + '<nav><a href="/c/68d0f2a1-4b3c-8000-9d2e-1a2b3c4d5e6f">Log in</a></nav>'
        + '<form><div id="prompt-textarea" class="ProseMirror" contenteditable="true"><p><br></p></div></form>',
    )
    assert await detect_blocked(web_page) is None


async def test_wait_for_composer_gives_up_when_there_is_no_message_box(web_page):
    await load_html(web_page, "<main><p>Something else entirely.</p></main>")
    with pytest.raises(ChatGPTError) as caught:
        await wait_for_composer(web_page, timeout=1.2)
    assert not isinstance(caught.value, ChatGPTBlocked)


async def test_submit_on_a_logged_out_page_reports_login(web_page):
    assert await load_fixture(web_page, "chatgpt/logged_out.html")
    await ensure_bridge(web_page)
    # The page script looks for the composer for 15 s before giving up; hurry it along.
    await run_js(
        web_page,
        "(() => { const real = Date.now.bind(Date); const t0 = real();"
        " Date.now = () => t0 + (real() - t0) * 40; })(); true",
    )
    with pytest.raises(ChatGPTBlocked) as caught:
        await submit_prompt(web_page, "short prompt")
    assert caught.value.reason == "login"


# ---------------------------------------------------------------------------
# Conversation link
# ---------------------------------------------------------------------------


async def test_conversation_url_is_the_normalised_tab_url(web_page):
    chat_id = "68d0f2a1-4b3c-8000-9d2e-1a2b3c4d5e6f"
    await load_html(web_page, "<p>x</p>", base_url="https://chatgpt.com/")
    assert conversation_url(web_page) is None
    await load_html(web_page, "<p>x</p>", base_url=f"https://www.chatgpt.com/c/{chat_id}?model=gpt-5#end")
    assert conversation_url(web_page) == f"https://chatgpt.com/c/{chat_id}"
