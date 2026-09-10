"""Regression coverage for project-scoped ChatGPT navigation."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.mcp_driver_pool import McpSessionDriverPool
from chatgpt_web2api.project_navigation import ProjectAwareCDPDriver, ProjectNavigationError

PROJECT_ID = "g-p-project123"


def _probe_payload(
    *,
    url: str,
    ready_state: str = "complete",
    app_shell: bool = True,
    composer: bool = True,
) -> str:
    return json.dumps(
        {
            "url": url,
            "ready_state": ready_state,
            "app_shell": app_shell,
            "composer": composer,
        }
    )


def _make_pool_config():
    cfg = MagicMock()
    cfg.chrome.cdp_port = 9222
    cfg.chatgpt.mcp_session_pool_size = 2
    cfg.chatgpt.mcp_session_pool_ttl_seconds = 1800
    cfg.chatgpt.mcp_session_pool_acquire_timeout = 5.0
    cfg.chatgpt.mcp_session_pool_sweep_interval_seconds = 60
    cfg.chatgpt.mcp_session_pool_create_concurrency = 1
    cfg.chatgpt.mcp_account_throttle_cooldown_seconds = 300
    return cfg


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://chatgpt.com/g/{PROJECT_ID}/project", True),
        (f"https://chatgpt.com/g/{PROJECT_ID}-agent-run-logs/project", True),
        (f"https://chatgpt.com/g/{PROJECT_ID}/project?model=auto", True),
        ("https://chatgpt.com/g/g-p-other/project", False),
        (f"https://chatgpt.com/c/conv-123/g/{PROJECT_ID}/project", False),
        (f"https://example.com/g/{PROJECT_ID}/project", False),
        ("https://chatgpt.com/", False),
        ("", False),
    ],
)
def test_project_url_matching_is_exact(url: str, expected: bool):
    assert ProjectAwareCDPDriver._is_url_at_project(url, PROJECT_ID) is expected


@pytest.mark.asyncio
async def test_project_readiness_waits_for_exact_ready_context(monkeypatch):
    driver = ProjectAwareCDPDriver(cdp_port=9222)
    driver._js_strict = AsyncMock(
        side_effect=[
            _probe_payload(
                url=f"https://chatgpt.com/g/{PROJECT_ID}/project",
                ready_state="loading",
                app_shell=False,
                composer=False,
            ),
            _probe_payload(url=f"https://chatgpt.com/g/{PROJECT_ID}-agent-run-logs/project"),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    ready, diagnostic = await driver._wait_for_project_ready(PROJECT_ID, attempts=2)

    assert ready is True
    assert diagnostic == "all stages passed"
    assert driver._js_strict.await_count == 2
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_project_readiness_fails_after_confirmed_displacement(monkeypatch):
    driver = ProjectAwareCDPDriver(cdp_port=9222)
    driver._js_strict = AsyncMock(
        side_effect=[
            _probe_payload(
                url=f"https://chatgpt.com/g/{PROJECT_ID}/project",
                ready_state="loading",
                app_shell=False,
                composer=False,
            ),
            _probe_payload(url="https://chatgpt.com/g/g-p-other/project"),
            _probe_payload(url="https://chatgpt.com/g/g-p-other/project"),
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    ready, diagnostic = await driver._wait_for_project_ready(PROJECT_ID, attempts=3)

    assert ready is False
    assert "url displaced" in diagnostic
    assert driver._js_strict.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_project_navigation_enters_from_shell_before_project(monkeypatch):
    base_calls: list[str | None] = []

    async def fake_base_navigation(self, gizmo_id=None):
        base_calls.append(gizmo_id)

    monkeypatch.setattr(CDPDriver, "navigate_new_chat", fake_base_navigation)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    driver = ProjectAwareCDPDriver(cdp_port=9222)
    driver._current_conv_id = "previous"
    driver._cdp = AsyncMock()
    driver._click_project_link = AsyncMock(
        return_value=(True, f"https://chatgpt.com/g/{PROJECT_ID}/project")
    )
    driver._wait_for_project_ready = AsyncMock(return_value=(True, "all stages passed"))

    await driver.navigate_new_chat(PROJECT_ID)

    assert base_calls == [None]
    driver._cdp.assert_not_awaited()
    driver._click_project_link.assert_awaited_once_with(PROJECT_ID)
    driver._wait_for_project_ready.assert_awaited_once_with(PROJECT_ID)
    assert driver._current_conv_id is None


@pytest.mark.asyncio
async def test_project_navigation_fails_closed_when_link_is_missing(monkeypatch):
    base_calls: list[str | None] = []

    async def fake_base_navigation(self, gizmo_id=None):
        base_calls.append(gizmo_id)

    monkeypatch.setattr(CDPDriver, "navigate_new_chat", fake_base_navigation)

    driver = ProjectAwareCDPDriver(cdp_port=9222)
    driver._click_project_link = AsyncMock(return_value=(False, "https://chatgpt.com/?model=auto"))
    driver._wait_for_project_ready = AsyncMock()

    with pytest.raises(ProjectNavigationError, match="exact project link was not found"):
        await driver.navigate_new_chat(PROJECT_ID)

    assert base_calls == [None]
    driver._wait_for_project_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_project_navigation_fails_closed_when_project_never_becomes_ready(monkeypatch):
    base_calls: list[str | None] = []

    async def fake_base_navigation(self, gizmo_id=None):
        base_calls.append(gizmo_id)

    monkeypatch.setattr(CDPDriver, "navigate_new_chat", fake_base_navigation)

    driver = ProjectAwareCDPDriver(cdp_port=9222)
    driver._click_project_link = AsyncMock(
        return_value=(True, f"https://chatgpt.com/g/{PROJECT_ID}/project")
    )
    driver._wait_for_project_ready = AsyncMock(
        return_value=(False, "composer not present (selector did not match after page loaded)")
    )

    with pytest.raises(ProjectNavigationError, match="composer not present"):
        await driver.navigate_new_chat(PROJECT_ID)

    assert base_calls == [None]


@pytest.mark.asyncio
async def test_non_project_navigation_delegates_unchanged(monkeypatch):
    base_calls: list[str | None] = []

    async def fake_base_navigation(self, gizmo_id=None):
        base_calls.append(gizmo_id)

    monkeypatch.setattr(CDPDriver, "navigate_new_chat", fake_base_navigation)

    driver = ProjectAwareCDPDriver(cdp_port=9222)
    driver._click_project_link = AsyncMock()
    driver._wait_for_project_ready = AsyncMock()

    await driver.navigate_new_chat()

    assert base_calls == [None]
    driver._click_project_link.assert_not_awaited()
    driver._wait_for_project_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_pool_path_constructs_project_aware_driver(monkeypatch):
    monkeypatch.delenv("W2A_INSTANCE_ID", raising=False)
    connect = AsyncMock()
    monkeypatch.setattr(ProjectAwareCDPDriver, "connect", connect)

    pool = McpSessionDriverPool(_make_pool_config())
    slot = MagicMock()
    slot.session_key = "project-session"
    slot.breakers = pool._make_breakers()

    driver = await pool._create_driver(slot)

    assert isinstance(driver, ProjectAwareCDPDriver)
    assert driver.port == 9222
    assert driver.tab_mode == "owned"
    assert driver._parallel_tabs is True
    connect.assert_awaited_once_with()
