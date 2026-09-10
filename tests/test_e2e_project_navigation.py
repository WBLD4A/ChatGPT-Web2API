"""Live MCP coverage for project-scoped navigation.

Run with an existing ChatGPT Project:

    W2A_E2E_RUN=1 W2A_E2E_PROJECT_ID=g-p-... \
        pytest tests/test_e2e_project_navigation.py -m e2e -v
"""

from __future__ import annotations

import asyncio
import os

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import chatgpt_web2api.mcp_server as mod
from chatgpt_web2api.project_navigation import ProjectAwareCDPDriver

pytestmark = pytest.mark.e2e


async def _live_project_server(driver: ProjectAwareCDPDriver, config):
    mod._driver = driver
    mod._driver_pool = None
    mod._config = config
    mod._lock = asyncio.Lock()
    return mod.create_server()


async def test_mcp_client_chats_inside_explicit_project_live(
    e2e_login_ready,
    e2e_config,
    e2e_created: dict,
):
    project_id = os.environ.get("W2A_E2E_PROJECT_ID", "").strip()
    if not project_id:
        pytest.skip("set W2A_E2E_PROJECT_ID to an existing ChatGPT Project id")

    driver = ProjectAwareCDPDriver(cdp_port=e2e_config.chrome.cdp_port)
    await driver.connect()
    server = await _live_project_server(driver, e2e_config)
    ctx = create_connected_server_and_client_session(server)
    session = await ctx.__aenter__()
    try:
        await session.initialize()
        result = await session.call_tool(
            "chat_completion",
            {
                "message": "Reply with exactly this token: W2A-E2E-MCP-PROJECT-OK",
                "project_id": project_id,
            },
        )
        assert result.isError is not True

        data = result.structuredContent or {}
        conversation_id = str(data.get("conversation_id") or "")
        if conversation_id:
            e2e_created["conversations"].add(conversation_id)
        assert conversation_id, f"conversation id missing from structured content: {data}"
        assert "W2A-E2E-MCP-PROJECT-OK" in str(data.get("content", "")), (
            f"marker missing from structured content: {data}"
        )

        listing = await session.call_tool(
            "list_conversations",
            {"limit": 50, "offset": 0},
        )
        assert listing.isError is not True
        conversations = (listing.structuredContent or {}).get("conversations", [])
        created = next(
            (item for item in conversations if item.get("id") == conversation_id),
            None,
        )
        assert created is not None, (
            f"new conversation {conversation_id} not present in list_conversations"
        )
        assert created.get("gizmo_id") == project_id, (
            f"conversation project mismatch: expected {project_id}, got {created.get('gizmo_id')}"
        )
    finally:
        await ctx.__aexit__(None, None, None)
        await driver.close()
