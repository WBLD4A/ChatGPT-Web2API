"""Bounded inline files and native uploads to the active ChatGPT composer."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import tempfile
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_ATTACHMENTS = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_BASE64_CHARS = ((MAX_FILE_BYTES + 2) // 3) * 4


class AttachmentUploadError(RuntimeError):
    """An upload could not be verified; the prompt must not be sent."""


class InlineAttachment(BaseModel):
    """File bytes, never a caller-controlled local filesystem path."""

    model_config = ConfigDict(hide_input_in_errors=True)

    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(default="application/octet-stream", max_length=128)
    content: str = Field(min_length=4, max_length=MAX_BASE64_CHARS, repr=False)

    @field_validator("name")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        """Reject traversal and names Windows cannot represent as ordinary files."""
        if (
            value in (".", "..")
            or value != value.strip()
            or value.endswith(".")
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", value.split(".")[0])
        ):
            raise ValueError("Attachment name must be a safe plain filename")
        return value

    @field_validator("content")
    @classmethod
    def valid_content(cls, value: str) -> str:
        """Validate strict base64 and the decoded per-file bound."""
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Attachment content must be valid base64") from exc
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError("Attachment must contain 1 byte through 10 MiB")
        return value


def validate_attachments(files: list[InlineAttachment]) -> list[InlineAttachment]:
    """Bound each turn and disallow filename collisions in the temporary directory."""
    if len(files) > MAX_ATTACHMENTS:
        raise ValueError("At most 10 attachments are allowed")
    if len({file.name.casefold() for file in files}) != len(files):
        raise ValueError("Attachment filenames must be unique (case-insensitive)")
    if sum(len(base64.b64decode(file.content)) for file in files) > MAX_TOTAL_BYTES:
        raise ValueError("Attachments exceed the 20 MiB total limit")
    return files


_FILE_INPUT_EXPRESSION = """(() => {
    const inputs = [...document.querySelectorAll('input[type="file"]')];
    return inputs.find(el => !el.disabled &&
        (!el.accept || el.accept === '*' || el.accept.includes('pdf') ||
         el.accept.includes('.txt') || el.accept.includes('.doc'))) ||
        (__D.imagesOnly ? inputs.find(el => !el.disabled && el.accept.includes('image/')) : undefined);
})()"""

_ATTACHMENT_CARDS_EXPRESSION = """(scope, composer) => {
    const dedicated = [...scope.querySelectorAll(
        '[data-file-id],[data-testid="file-thumbnail"],[data-testid="file-chip"],[data-testid="attachment"]')];
    const remove = [...scope.querySelectorAll('button')].filter(el => {
        const label = (el.getAttribute('aria-label') || '').toLowerCase();
        return ['remove-file', 'remove-attachment', 'remove-image'].includes(el.getAttribute('data-testid')) ||
            ['remove file', 'remove attachment', 'remove image', 'supprimer le fichier',
             'supprimer la pièce jointe', "supprimer l'image"].includes(label);
    });
    const inferred = remove.map(el => el.closest('[data-file-id],[data-testid="file-thumbnail"],[data-testid="file-chip"],[data-testid="attachment"]') ||
        (el.parentElement && el.parentElement.parentElement));
    return [...new Set([...dedicated, ...inferred])].filter(card => card && card !== scope &&
        !card.contains(composer) && !composer.contains(card) && card.getClientRects().length > 0);
}"""

_UPLOAD_STATE_EXPRESSION = """(() => {
    const names = __D.names;
    const composer = document.querySelector('#prompt-textarea,textarea[name="prompt-textarea"]');
    const scope = composer && (composer.closest('form') || composer.parentElement);
    if (!scope) return JSON.stringify({ready: false, error: 'Composer disappeared during upload'});
    const cards = (__CARDS__)(scope, composer);
    const used = new Set();
    const present = names.every(name => {
        const index = cards.findIndex((card, index) => {
            if (used.has(index)) return false;
            const nodes = [card, ...card.querySelectorAll('[title],[aria-label],img,span,p')];
            const labels = nodes.flatMap(el => [el.getAttribute('title') || '', el.getAttribute('aria-label') || '',
                el.getAttribute('alt') || '', el.textContent || '']);
            return labels.some(label => label.trim() === name);
        });
        if (index < 0) return false;
        used.add(index);
        return true;
    });
    const busy = [...scope.querySelectorAll('[role="progressbar"],[aria-busy="true"],.animate-spin')]
        .some(el => el.getClientRects().length > 0);
    const alert = [...scope.querySelectorAll('[role="alert"],[data-state="error"]')]
        .find(el => el.getClientRects().length > 0 && (el.textContent || '').trim());
    const send = scope.querySelector('[data-testid="send-button"],button[aria-label="Send prompt"]');
    return JSON.stringify({ready: present && !busy && !!send && !send.disabled &&
        send.getAttribute('aria-disabled') !== 'true', error: alert ? 'ChatGPT rejected an attachment' : null});
})()""".replace("__CARDS__", _ATTACHMENT_CARDS_EXPRESSION)

_CLEANUP_STATE_EXPRESSION = """(() => {
    const composer = document.querySelector('#prompt-textarea,textarea[name="prompt-textarea"]');
    const scope = composer && (composer.closest('form') || composer.parentElement);
    return JSON.stringify({clean: !window[__D.marker] && document.readyState === 'complete' && !!scope &&
        !(composer.innerText || composer.value || '').trim() && (__CARDS__)(scope, composer).length === 0});
})()""".replace("__CARDS__", _ATTACHMENT_CARDS_EXPRESSION)


async def discard_pending_attachments(driver) -> None:
    """Require a new loaded document with an empty composer and no attachment cards."""
    from .cdp_driver import CDPJSError

    driver._attachment_state_dirty = True
    marker = "w2a_cleanup_" + uuid.uuid4().hex
    try:
        await driver._js_with_data_strict("(() => { window[__D.marker] = true; return 'armed'; })()", {"marker": marker})
        await driver._cdp("Page.reload")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                raw = await driver._js_with_data_strict(_CLEANUP_STATE_EXPRESSION, {"marker": marker})
            except CDPJSError as exc:
                if "context" not in str(exc).lower() or not any(
                    term in str(exc).lower() for term in ("destroyed", "cannot find", "not found")
                ):
                    raise
                await asyncio.sleep(0.25)
                continue
            if json.loads(raw).get("clean") is True:
                driver._attachment_state_dirty = False
                return
            await asyncio.sleep(0.25)
        raise AttachmentUploadError("Composer cleanup failed; reconnect the worker")
    except Exception as exc:
        raise AttachmentUploadError("Attachment cleanup failed; reconnect the worker") from exc


async def _write_temporary_file(path: Path, data: bytes) -> None:
    """Join an in-flight filesystem write before cancellation removes its directory."""
    write = asyncio.create_task(asyncio.to_thread(path.write_bytes, data))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        await write
        raise


async def upload_attachments(driver, files: list[InlineAttachment], timeout: float = 60) -> None:
    """Use Chrome's native file chooser, wait for visible files and an enabled send control.

    Temporary files live only until upload verification. Missing selectors, rejection
    and timeout fail closed; the caller clears unsent browser state in its finally path.
    """
    validate_attachments(files)
    if not files:
        return
    driver._assert_owned_tab_required()
    with tempfile.TemporaryDirectory(prefix="w2a-attachments-") as directory:
        paths = []
        for file in files:
            path = Path(directory) / file.name
            await _write_temporary_file(path, base64.b64decode(file.content))
            paths.append(str(path.resolve()))
        result = await driver._cdp("Runtime.evaluate", {
            "expression": "((__D) => (" + _FILE_INPUT_EXPRESSION + "))(" + json.dumps({
                "imagesOnly": all(file.mime_type.startswith("image/") for file in files),
            }) + ")",
            "returnByValue": False,
        })
        evaluation = result.get("result", {})
        object_id = evaluation.get("result", {}).get("objectId")
        if not object_id or result.get("error") or evaluation.get("exceptionDetails"):
            raise AttachmentUploadError("ChatGPT has no document file input for this model")
        try:
            await driver._cdp("DOM.setFileInputFiles", {"files": paths, "objectId": object_id})
        finally:
            await driver._cdp("Runtime.releaseObject", {"objectId": object_id})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await driver._js_with_data_strict(
                _UPLOAD_STATE_EXPRESSION, {"names": [file.name for file in files]},
            )
            state = json.loads(raw)
            if state.get("error"):
                raise AttachmentUploadError(state["error"])
            if state.get("ready") is True:
                return
            await asyncio.sleep(0.25)
        raise AttachmentUploadError("Attachment upload readiness timed out; message was not sent")
