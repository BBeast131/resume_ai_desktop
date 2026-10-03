from __future__ import annotations

import pytest

from app.browser.jsbridge import JsError, call_async_js, ensure_script, js_string, run_js, run_js_json
from tests.helpers import load_html


async def test_run_js_returns_values(web_page):
    await load_html(web_page, "<title>t</title><p id=a>hello</p>")
    assert await run_js(web_page, "document.getElementById('a').textContent") == "hello"
    assert await run_js_json(web_page, "({ a: 1, b: [null, 'x'] })") == {"a": 1, "b": [None, "x"]}


async def test_run_js_json_reports_errors(web_page):
    await load_html(web_page, "<p>x</p>")
    with pytest.raises(JsError):
        await run_js_json(web_page, "nope.nothing")


async def test_call_async_awaits_promises(web_page):
    await load_html(web_page, "<p>x</p>")
    value = await call_async_js(web_page, "new Promise((r) => setTimeout(() => r({ n: 42 }), 300))")
    assert value == {"n": 42}
    with pytest.raises(JsError):
        await call_async_js(web_page, "Promise.reject(new Error('boom'))")


async def test_ensure_script_injects_once(web_page):
    await load_html(web_page, "<p>x</p>")
    source = "window.__counter = (window.__counter || 0) + 1; window.__marker = true;"
    await ensure_script(web_page, "window.__marker", source)
    await ensure_script(web_page, "window.__marker", source)
    assert await run_js(web_page, "window.__counter") == 1


def test_js_string_escapes_page_data():
    assert js_string('a"b</script>\n') == '"a\\"b</script>\\n"'
    assert " " not in js_string("x y")
