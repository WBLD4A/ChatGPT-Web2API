"""Regression: DOM message-role drift must not hide a completed anchored turn."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import (
    AuthExpiredError,
    CDPDriver,
    GenerationStuckError,
    RateLimitError,
)
from chatgpt_web2api.turn_anchor import TurnAnchor, TurnTextResult, select_text_for_turn


@pytest.fixture
def driver(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("chatgpt_web2api.completion_detector.time.monotonic", lambda: clock[0])
    async def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr("chatgpt_web2api.completion_detector.asyncio.sleep", sleep)
    d = CDPDriver(instance_id="phase1-unit")
    d._identity_listener = None
    d._assert_owned_tab_required = MagicMock()
    d._read_assistant_count_baseline = AsyncMock(return_value=0)
    d._verify_send_acknowledged = AsyncMock(return_value=True)
    d._capture_pre_send_fallback_anchor = AsyncMock(return_value=TurnAnchor(
        sent_text="same prompt", mode="captured_id", captured_user_message_id="u-new",
    ))
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._get_live_conversation_id_best_effort = AsyncMock(return_value="conv-real")
    async def js(expr, timeout=15):
        if "location.href" in expr:
            return "https://chatgpt.com/c/conv-real"
        if "innerText" in expr:
            return json.dumps({"text": ""})
        return "0"  # no old message-role attributes in the new UI
    d._js_strict = js
    return d


@pytest.mark.asyncio
async def test_missing_dom_returns_only_the_new_anchored_turn(driver):
    nodes = {}
    for name, answer in [("old", "stale answer"), ("new", "fresh answer")]:
        nodes["u-" + name] = {"id": "u-" + name, "children": ["a-" + name], "message": {
            "id": "u-" + name, "author": {"role": "user"},
            "content": {"content_type": "text", "parts": ["same prompt"]},
        }}
        nodes["a-" + name] = {"id": "a-" + name, "parent": "u-" + name, "message": {
            "id": "a-" + name, "author": {"role": "assistant"}, "end_turn": True,
            "content": {"content_type": "text", "parts": [answer]},
        }}
    async def fetch(conv_id, anchor):
        assert conv_id == "conv-real"
        return select_text_for_turn({"nodes": nodes}, anchor)
    driver._fetch_text_for_turn = AsyncMock(side_effect=fetch)
    chunks = [chunk async for chunk in driver.send_and_stream("same prompt", timeout=5)]
    assert "".join(c.delta for c in chunks) == "fresh answer"
    assert chunks[-1].finish_reason == "stop"
    assert driver._current_conv_id == "conv-real"
    driver.click_send.assert_awaited_once()
    driver.type_message.assert_awaited_once_with("same prompt")
    assert driver._fetch_text_for_turn.await_count == 2  # observation + final reconciliation


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [
    ("not_ready", ""), ("ambiguous", "prior answer"), ("degraded_not_fresh", "prior answer"),
    ("fetch_failed", None), ("non_text", None), ("matched", ""), ("matched", "   "),
])
async def test_missing_dom_never_completes_unverified_or_empty_response(driver, status, text):
    driver._fetch_text_for_turn = AsyncMock(return_value=TurnTextResult(status=status, text=text))
    chunks = []
    with pytest.raises(GenerationStuckError):
        async for chunk in driver.send_and_stream("same prompt", timeout=5):
            chunks.append(chunk)
    assert not chunks
    driver.click_send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [AuthExpiredError(), RateLimitError()])
async def test_missing_dom_preserves_auth_and_rate_limit_errors(driver, error):
    driver._fetch_text_for_turn = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        async for _ in driver.send_and_stream("same prompt", timeout=5):
            pass
    driver.click_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_dom_never_uses_request_uuid_as_conversation_identity(driver):
    driver._get_live_conversation_id_best_effort = AsyncMock(return_value="")
    driver._fetch_text_for_turn = AsyncMock(return_value=TurnTextResult(status="matched", text="unsafe"))
    with pytest.raises(GenerationStuckError):
        async for _ in driver.send_and_stream("same prompt", timeout=5):
            pass
    driver._fetch_text_for_turn.assert_not_awaited()
    assert driver._completion.resolved_conversation_id == ""
