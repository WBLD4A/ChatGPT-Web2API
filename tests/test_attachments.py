import asyncio
import base64
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import ValidationError

import chatgpt_web2api.attachments as attachments
import chatgpt_web2api.mcp_server as mcp_server
from chatgpt_web2api.attachments import (
    AttachmentUploadError,
    InlineAttachment,
    discard_pending_attachments,
    upload_attachments,
    validate_attachments,
)
from chatgpt_web2api.cdp_driver import CDPDriver, CDPJSError, StreamChunk
from chatgpt_web2api.cdp_transport import CDPTransport
from chatgpt_web2api.config import Config


def file(name="report.pdf", data=b"PDF-bytes", **overrides):
    values = {
        "name": name,
        "mime_type": "application/pdf",
        "content": base64.b64encode(data).decode("ascii"),
    }
    values.update(overrides)
    return InlineAttachment(**values)


@pytest.mark.parametrize("name", [
    "../secret.pdf", "C:\\secret.pdf", "CON.pdf", "com1.txt", "a.", " a.pdf", "a\x00.pdf",
])
def test_inline_file_rejects_paths_and_windows_reserved_names(name):
    with pytest.raises(ValidationError):
        file(name=name)


@pytest.mark.parametrize("content", ["!!!!", "YQ", "Y Q==", "===="])
def test_inline_file_rejects_invalid_base64(content):
    with pytest.raises(ValidationError):
        file(content=content)


def test_inline_file_accepts_exact_binary_bytes_and_hides_content_in_repr():
    item = file(name="été.png", data=b"\x00\xffbinary")
    assert base64.b64decode(item.content) == b"\x00\xffbinary"
    assert item.name == "été.png"
    assert item.content not in repr(item)


def test_inline_file_per_file_limit_accepts_boundary_and_rejects_overflow(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", 3)
    assert base64.b64decode(file(data=b"123").content) == b"123"
    with pytest.raises(ValidationError):
        file(data=b"1234")


def test_turn_limits_reject_count_duplicate_names_and_total_bytes(monkeypatch):
    with pytest.raises(ValueError, match="10"):
        validate_attachments([file(f"{index}.pdf") for index in range(11)])
    with pytest.raises(ValueError, match="unique"):
        validate_attachments([file(), file("REPORT.PDF")])
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 5)
    with pytest.raises(ValueError, match="total"):
        validate_attachments([file("first.pdf", b"123"), file("second.pdf", b"456")])
    assert validate_attachments([file(data=b"12345")])


def make_upload_driver(*, state=None, input_available=True, setter_error=None):
    driver = MagicMock()
    paths = []

    async def cdp(method, params=None):
        if method == "Runtime.evaluate":
            return {"result": {"result": {"objectId": "file-input"}}} if input_available else {"result": {"result": {}}}
        if method == "DOM.setFileInputFiles":
            paths.extend(Path(path) for path in params["files"])
            if setter_error is not None:
                raise setter_error
        return {}

    driver._cdp = AsyncMock(side_effect=cdp)
    driver._js_with_data_strict = AsyncMock(return_value=json.dumps(state or {"ready": True, "error": None}))
    return driver, paths


@pytest.mark.asyncio
async def test_native_upload_materializes_exact_bytes_and_cleans_temporary_files():
    driver, paths = make_upload_driver()
    observed_bytes = []
    original = driver._cdp.side_effect

    async def cdp(method, params=None):
        if method == "DOM.setFileInputFiles":
            observed_bytes.extend(Path(path).read_bytes() for path in params["files"])
        return await original(method, params)

    driver._cdp.side_effect = cdp
    await upload_attachments(driver, [file(data=b"\x00\xffPDF")])
    assert observed_bytes == [b"\x00\xffPDF"]
    assert [call.args[0] for call in driver._cdp.await_args_list] == [
        "Runtime.evaluate", "DOM.setFileInputFiles", "Runtime.releaseObject",
    ]
    assert driver._cdp.await_args_list[1].args[1]["objectId"] == "file-input"
    assert driver._js_with_data_strict.call_args.args[1] == {"names": ["report.pdf"]}
    assert paths and all(not path.exists() and not path.parent.exists() for path in paths)
    driver._assert_owned_tab_required.assert_called_once()


@pytest.mark.asyncio
async def test_native_upload_without_input_fails_before_setting_files():
    driver, paths = make_upload_driver(input_available=False)
    with pytest.raises(AttachmentUploadError, match="file input"):
        await upload_attachments(driver, [file()])
    assert [call.args[0] for call in driver._cdp.await_args_list] == ["Runtime.evaluate"]
    assert paths == []


@pytest.mark.asyncio
async def test_native_upload_rejection_releases_object_and_cleans_files():
    driver, paths = make_upload_driver(state={"ready": False, "error": "ChatGPT rejected an attachment"})
    with pytest.raises(AttachmentUploadError, match="rejected"):
        await upload_attachments(driver, [file()])
    assert [call.args[0] for call in driver._cdp.await_args_list][-1] == "Runtime.releaseObject"
    assert paths and all(not path.exists() and not path.parent.exists() for path in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("CDP failed"), asyncio.CancelledError()])
async def test_native_upload_setter_failure_and_cancellation_release_object_and_cleanup(error):
    driver, paths = make_upload_driver(setter_error=error)
    with pytest.raises(type(error)):
        await upload_attachments(driver, [file()])
    assert [call.args[0] for call in driver._cdp.await_args_list][-1] == "Runtime.releaseObject"
    assert paths and all(not path.exists() and not path.parent.exists() for path in paths)


@pytest.mark.asyncio
async def test_native_upload_timeout_fails_closed_and_cleans_files(monkeypatch):
    driver, paths = make_upload_driver(state={"ready": False, "error": None})
    clock = iter([0, 0, 2])
    monkeypatch.setattr(attachments, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(attachments.asyncio, "sleep", AsyncMock())
    with pytest.raises(AttachmentUploadError, match="timed out"):
        await upload_attachments(driver, [file()], timeout=1)
    assert driver._js_with_data_strict.await_count == 1
    assert paths and all(not path.exists() and not path.parent.exists() for path in paths)


@pytest.mark.asyncio
async def test_empty_upload_does_not_touch_browser():
    driver, paths = make_upload_driver()
    await upload_attachments(driver, [])
    driver._cdp.assert_not_awaited()
    driver._assert_owned_tab_required.assert_not_called()
    assert paths == []


@pytest.mark.asyncio
async def test_discard_pending_attachments_reloads_and_clears_dirty_flag():
    driver = MagicMock()
    driver._cdp = AsyncMock()
    driver._js_with_data_strict = AsyncMock(side_effect=["armed", json.dumps({"clean": True})])
    await discard_pending_attachments(driver)
    driver._cdp.assert_awaited_once_with("Page.reload")
    assert driver._js_with_data_strict.await_count == 2
    marker = driver._js_with_data_strict.await_args_list[0].args[1]["marker"]
    assert marker.startswith("w2a_cleanup_")
    assert driver._js_with_data_strict.await_args_list[1].args[1] == {"marker": marker}
    assert driver._attachment_state_dirty is False


@pytest.mark.asyncio
@pytest.mark.parametrize("composer_ready", [False, RuntimeError("not ready")])
async def test_cleanup_failure_keeps_driver_quarantined(composer_ready, monkeypatch):
    driver = MagicMock()
    driver._cdp = AsyncMock()
    driver._js_with_data_strict = AsyncMock(side_effect=[
        "armed",
        composer_ready if isinstance(composer_ready, Exception) else json.dumps({"clean": False}),
    ])
    clock = iter([0, 0, 9])
    monkeypatch.setattr(attachments, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(attachments.asyncio, "sleep", AsyncMock())
    with pytest.raises(AttachmentUploadError, match="cleanup failed"):
        await discard_pending_attachments(driver)
    assert driver._attachment_state_dirty is True


def make_completion_driver(stream):
    driver = MagicMock()
    driver._current_conv_id = "conv"
    driver._current_model = None
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_conversation = AsyncMock()
    driver.navigate_new_chat = AsyncMock()
    driver.ensure_current_conversation = AsyncMock()
    driver.send_and_stream = stream
    return driver


@pytest.mark.asyncio
async def test_mcp_completion_forwards_files_to_explicit_followup_conversation():
    calls = []

    async def stream(text, timeout=120, *, budgets=None, model=None, attachments=None):
        calls.append((text, attachments))
        yield StreamChunk(delta="Read exact files")
        yield StreamChunk(delta="", finish_reason="stop")

    driver = make_completion_driver(stream)
    result = await mcp_server.do_chat_completion(driver, {
        "message": "read",
        "conversation_id": "conv",
        "project_id": "g-p-task",
        "attachments": [file().model_dump()],
    }, Config.load(None))
    driver.navigate_conversation.assert_awaited_once_with("conv")
    assert calls[0][0] == "read"
    assert calls[0][1][0].model_dump() == file().model_dump()
    assert result["content"] == "Read exact files"
    assert result["conversation_id"] == "conv"


@pytest.mark.asyncio
async def test_mcp_text_only_call_keeps_legacy_driver_signature():
    calls = []

    async def stream(text, timeout=120, *, budgets=None, model=None):
        calls.append(text)
        yield StreamChunk(delta="legacy")
        yield StreamChunk(delta="", finish_reason="stop")

    driver = make_completion_driver(stream)
    result = await mcp_server.do_chat_completion(
        driver, {"message": "text", "project_id": "g-p-task"}, Config.load(None),
    )
    assert calls == ["text"]
    assert result["content"] == "legacy"
    driver.navigate_new_chat.assert_awaited_once_with(gizmo_id="g-p-task")


@pytest.mark.asyncio
async def test_mcp_rejects_invalid_files_before_navigation_and_send():
    stream = MagicMock()
    driver = make_completion_driver(stream)
    with pytest.raises(ValidationError):
        await mcp_server.do_chat_completion(driver, {
            "message": "read", "attachments": [{"name": "a.pdf", "content": "!!!!"}],
        }, Config.load(None))
    driver.navigate_new_chat.assert_not_awaited()
    driver.ensure_current_conversation.assert_not_awaited()
    stream.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_rejects_aggregate_file_limit_before_browser_mutation(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", 5)
    stream = MagicMock()
    driver = make_completion_driver(stream)
    with pytest.raises(ValueError, match="total"):
        await mcp_server.do_chat_completion(driver, {
            "message": "read",
            "attachments": [file("a.pdf", b"123").model_dump(), file("b.pdf", b"456").model_dump()],
        }, Config.load(None))
    driver.navigate_new_chat.assert_not_awaited()
    stream.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_tool_advertises_optional_bounded_inline_files(monkeypatch):
    monkeypatch.setattr(mcp_server, "_driver", None)
    server = mcp_server.create_server()
    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        tools = await session.list_tools()
    tool = next(tool for tool in tools.tools if tool.name == "chat_completion")
    field = tool.inputSchema["properties"]["attachments"]
    assert field["maxItems"] == 10
    assert "attachments" not in tool.inputSchema["required"]
    schema = tool.inputSchema["$defs"]["InlineAttachment"]
    assert set(schema["required"]) >= {"name", "content"}
    assert "path" not in schema["properties"]


def make_send_driver():
    driver = CDPDriver(cdp_port=9222)
    driver._assert_owned_tab_required = MagicMock()
    driver._identity_listener = None
    driver._read_assistant_count_baseline = AsyncMock(return_value=0)
    driver._capture_pre_send_fallback_anchor = AsyncMock(return_value=SimpleNamespace())
    driver.type_message = AsyncMock()
    driver.click_send = AsyncMock()
    driver._cdp = AsyncMock()
    driver._js_with_data_strict = AsyncMock(side_effect=["armed", json.dumps({"clean": True})])
    return driver


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [AttachmentUploadError("upload rejected"), asyncio.CancelledError()])
async def test_send_upload_failure_or_cancellation_never_clicks_send_and_cleans_composer(monkeypatch, error):
    driver = make_send_driver()
    monkeypatch.setattr(attachments, "upload_attachments", AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        async for _ in driver.send_and_stream("read", attachments=[file()]):
            pass
    driver.click_send.assert_not_awaited()
    driver._cdp.assert_awaited_once_with("Page.reload")
    assert driver._attachment_state_dirty is False

NODE_DOM_SCRIPT = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const scenario = input.scenario;
function element(spec = {}) {
    const attributes = spec.attributes || {};
    return {
        textContent: spec.text || '',
        innerText: spec.text || '',
        value: spec.value || '',
        disabled: Boolean(spec.disabled),
        getAttribute(name) { return attributes[name] || null; },
        getClientRects() { return spec.hidden ? [] : [{}]; },
        contains(other) { return other === this || Boolean(spec.containsComposer && other === composer); },
        querySelectorAll() { return (spec.labels || []).map(text => element({text})); },
        closest() { return null; },
    };
}
const composer = element({text: scenario.prompt, value: scenario.value});
const cards = (scenario.cards || []).map(spec => element(spec));
const busy = scenario.busy ? [element()] : [];
const alerts = scenario.alert ? [element({text: scenario.alert})] : [];
const send = scenario.noSend ? null : element({
    disabled: scenario.disabled,
    attributes: {'aria-disabled': scenario.ariaDisabled ? 'true' : 'false'},
});
const scope = {
    textContent: scenario.prompt || '',
    querySelectorAll(selector) {
        if (selector.includes('[data-file-id]')) return cards;
        if (selector === 'button') return [];
        if (selector.includes('progressbar')) return busy;
        if (selector.includes('role="alert"')) return alerts;
        return [];
    },
    querySelector() { return send; },
};
composer.closest = () => scope;
composer.parentElement = scope;
const window = scenario.oldDocument ? {[input.marker]: true} : {};
const document = {
    readyState: scenario.loading ? 'loading' : 'complete',
    querySelector() { return scenario.noComposer ? null : composer; },
};
const output = vm.runInNewContext(input.expression, {window, document});
process.stdout.write(JSON.stringify(output));
"""


def evaluate_dom_expression(expression, scenario, marker=None):
    node = shutil.which("node")
    assert node is not None, "Node.js is required for attachment DOM contract tests"
    result = subprocess.run(
        [node, "-e", NODE_DOM_SCRIPT],
        input=json.dumps({"expression": expression, "scenario": scenario, "marker": marker}),
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return json.loads(result.stdout)


async def transport_dom_state(expression, data, scenario):
    async def js_strict(wrapped, timeout=15):
        return await asyncio.to_thread(evaluate_dom_expression, wrapped, scenario, data.get("marker"))

    driver = SimpleNamespace(_js_strict=js_strict)
    return json.loads(await CDPTransport(driver)._js_with_data_strict(expression, data))


@pytest.mark.asyncio
@pytest.mark.parametrize(("names", "scenario"), [
    (["a.pdf"], {"prompt": "Please read a.pdf", "cards": []}),
    (["a.pdf"], {"cards": [{"text": "data.pdf"}]}),
    (["a.pdf", "data.pdf"], {"cards": [{"labels": ["a.pdf", "data.pdf"]}]}),
    (["a.pdf"], {"cards": [{"text": "a.pdf", "containsComposer": True}]}),
    (["a.pdf"], {"cards": [{"text": "a.pdf", "hidden": True}]}),
])
async def test_real_upload_expression_rejects_prompt_substrings_duplicate_cards_and_hidden_cards(names, scenario):
    state = await transport_dom_state(attachments._UPLOAD_STATE_EXPRESSION, {"names": names}, scenario)
    assert state["ready"] is False


@pytest.mark.asyncio
async def test_real_upload_expression_accepts_exact_distinct_cards_through_transport_data_injection():
    names = ["a.pdf", "data.pdf", 'quoted "document".pdf']
    scenario = {"prompt": "Read the supplied files", "cards": [
        {"labels": ["a.pdf"]},
        {"attributes": {"title": "data.pdf"}},
        {"attributes": {"aria-label": 'quoted "document".pdf'}},
    ]}
    state = await transport_dom_state(attachments._UPLOAD_STATE_EXPRESSION, {"names": names}, scenario)
    assert state == {"ready": True, "error": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", [
    {"busy": True},
    {"disabled": True},
    {"ariaDisabled": True},
    {"noSend": True},
    {"noComposer": True},
])
async def test_real_upload_expression_rejects_busy_disabled_or_missing_composer(boundary):
    scenario = {"cards": [{"text": "a.pdf"}], **boundary}
    state = await transport_dom_state(attachments._UPLOAD_STATE_EXPRESSION, {"names": ["a.pdf"]}, scenario)
    assert state["ready"] is False


@pytest.mark.asyncio
async def test_real_upload_expression_surfaces_visible_upload_rejection():
    state = await transport_dom_state(attachments._UPLOAD_STATE_EXPRESSION, {"names": ["a.pdf"]}, {
        "cards": [{"text": "a.pdf"}], "alert": "File rejected",
    })
    assert state["error"] == "ChatGPT rejected an attachment"


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    {"oldDocument": True},
    {"prompt": "unsent draft"},
    {"value": "unsent text"},
    {"cards": [{"text": "a.pdf"}]},
    {"loading": True},
    {"noComposer": True},
])
async def test_real_cleanup_expression_rejects_old_document_draft_files_or_loading(scenario):
    state = await transport_dom_state(
        attachments._CLEANUP_STATE_EXPRESSION, {"marker": "cleanup_marker"}, scenario,
    )
    assert state["clean"] is False


@pytest.mark.asyncio
async def test_real_cleanup_expression_accepts_new_complete_empty_document():
    state = await transport_dom_state(
        attachments._CLEANUP_STATE_EXPRESSION, {"marker": "cleanup_marker"}, {},
    )
    assert state == {"clean": True}


@pytest.mark.asyncio
async def test_reload_retaining_old_document_keeps_driver_quarantined(monkeypatch):
    marker = None
    clock = iter([0, 0, 9])

    async def js_with_data(expression, data):
        nonlocal marker
        if "return 'armed'" in expression:
            marker = data["marker"]
            return "armed"
        assert data["marker"] == marker
        state = await transport_dom_state(expression, data, {"oldDocument": True})
        return json.dumps(state)

    driver = SimpleNamespace(_cdp=AsyncMock(), _js_with_data_strict=js_with_data)
    monkeypatch.setattr(attachments, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(attachments.asyncio, "sleep", AsyncMock())
    with pytest.raises(AttachmentUploadError, match="cleanup failed"):
        await discard_pending_attachments(driver)
    assert driver._attachment_state_dirty is True
    driver._cdp.assert_awaited_once_with("Page.reload")

@pytest.mark.asyncio
async def test_send_orders_text_upload_then_click_and_cleans_on_send_failure(monkeypatch):
    driver = make_send_driver()
    order = []

    async def type_message(text):
        order.append(("text", text))

    async def upload(current_driver, files):
        order.append(("upload", files[0].name))

    async def click_send():
        order.append(("click", None))
        raise RuntimeError("send refused")

    driver.type_message = AsyncMock(side_effect=type_message)
    driver.click_send = AsyncMock(side_effect=click_send)
    monkeypatch.setattr(attachments, "upload_attachments", upload)
    with pytest.raises(RuntimeError, match="send refused"):
        async for _ in driver.send_and_stream("read", attachments=[file()]):
            pass
    assert order == [("text", "read"), ("upload", "report.pdf"), ("click", None)]
    driver._cdp.assert_awaited_once_with("Page.reload")


@pytest.mark.asyncio
async def test_send_quarantined_driver_blocks_later_text_calls_before_mutation():
    driver = make_send_driver()
    driver._attachment_state_dirty = True
    with pytest.raises(AttachmentUploadError, match="reconnect"):
        async for _ in driver.send_and_stream("later text"):
            pass
    driver._read_assistant_count_baseline.assert_not_awaited()
    driver.type_message.assert_not_awaited()
    driver.click_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_retries_destroyed_context_then_verifies_new_document(monkeypatch):
    driver = SimpleNamespace(
        _cdp=AsyncMock(),
        _js_with_data_strict=AsyncMock(side_effect=[
            "armed", CDPJSError("Execution context was destroyed"), json.dumps({"clean": True}),
        ]),
    )
    monkeypatch.setattr(attachments.asyncio, "sleep", AsyncMock())
    await discard_pending_attachments(driver)
    assert driver._attachment_state_dirty is False
    assert driver._js_with_data_strict.await_count == 3
    driver._cdp.assert_awaited_once_with("Page.reload")


@pytest.mark.asyncio
async def test_cleanup_permanent_js_error_keeps_driver_quarantined():
    driver = SimpleNamespace(
        _cdp=AsyncMock(),
        _js_with_data_strict=AsyncMock(side_effect=["armed", CDPJSError("ReferenceError: broken selector")]),
    )
    with pytest.raises(AttachmentUploadError, match="cleanup failed"):
        await discard_pending_attachments(driver)
    assert driver._attachment_state_dirty is True
    assert driver._js_with_data_strict.await_count == 2


@pytest.mark.asyncio
async def test_upload_write_cancellation_joins_writer_before_removing_temporary_directory(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    paths = []
    writes = []

    async def delayed_write(function, data):
        path = function.__self__
        paths.append(path)
        started.set()
        await release.wait()
        writes.append(function(data))

    driver, _ = make_upload_driver()
    monkeypatch.setattr(attachments.asyncio, "to_thread", delayed_write)
    task = asyncio.create_task(upload_attachments(driver, [file()]))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert paths[0].parent.exists()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writes == [len(b"PDF-bytes")]
    assert not paths[0].parent.exists()
    driver._cdp.assert_not_awaited()
