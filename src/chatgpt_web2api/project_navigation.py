"""Project-scoped navigation safeguards for MCP session drivers."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse

from .cdp_driver import (
    CDPDriver,
    COMPOSER_FALLBACK_SELECTOR,
    COMPOSER_SELECTOR,
    NavigationReadinessProbe,
)

logger = logging.getLogger(__name__)


class ProjectNavigationError(RuntimeError):
    """Raised when the requested ChatGPT project context cannot be proven."""


class ProjectAwareCDPDriver(CDPDriver):
    """CDPDriver that verifies project context before allowing a project chat."""

    @staticmethod
    def _project_segment_matches(segment: str, gizmo_id: str) -> bool:
        """Match either the raw project id or ChatGPT's id-plus-slug segment."""
        return segment == gizmo_id or segment.startswith(f"{gizmo_id}-")

    @classmethod
    def _is_url_at_project(cls, url: str, gizmo_id: str) -> bool:
        """Return True only when *url* identifies the requested ChatGPT project."""
        if not url or not gizmo_id:
            return False
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return False
        if (parsed.hostname or "").lower() != "chatgpt.com":
            return False
        parts = [part for part in parsed.path.split("/") if part]
        return (
            len(parts) >= 2
            and parts[0] == "g"
            and cls._project_segment_matches(parts[1], gizmo_id)
        )

    async def _wait_for_project_ready(
        self,
        gizmo_id: str,
        *,
        attempts: int = 30,
    ) -> tuple[bool, str]:
        """Wait until exact project identity and the send composer are both ready."""
        last_diagnostic = "project readiness was not observed"
        displaced_polls = 0
        saw_requested_project = False

        for _ in range(attempts):
            try:
                raw = await self._js_strict(
                    "(function() {"
                    "  return JSON.stringify({"
                    "    url: location.href,"
                    "    ready_state: document.readyState,"
                    "    app_shell: !!document.querySelector('nav, [class*=\"sidebar\"]'),"
                    f"    composer: !!document.querySelector('{COMPOSER_SELECTOR}') || !!document.querySelector('{COMPOSER_FALLBACK_SELECTOR}')"
                    "  });"
                    "})()"
                )
                state = json.loads(raw)
            except Exception as exc:
                last_diagnostic = f"project readiness probe failed: {type(exc).__name__}: {exc}"
                await asyncio.sleep(0.5)
                continue

            probe = NavigationReadinessProbe(
                url=str(state.get("url") or ""),
                ready_state=str(state.get("ready_state") or ""),
                app_shell_present=bool(state.get("app_shell")),
                composer_present=bool(state.get("composer")),
            )
            url_correct = self._is_url_at_project(probe.url, gizmo_id)
            if url_correct:
                saw_requested_project = True
                displaced_polls = 0
            elif saw_requested_project:
                displaced_polls += 1
            last_diagnostic = probe.diagnostic_summary(url_correct=url_correct)

            if probe.is_ready(url_correct=url_correct):
                return True, last_diagnostic
            if displaced_polls >= 2:
                return False, last_diagnostic
            await asyncio.sleep(0.5)

        return False, last_diagnostic

    async def _click_project_link(self, gizmo_id: str) -> tuple[bool, str]:
        """Click the exact project link through the loaded ChatGPT application shell."""
        try:
            raw = await self._js_with_data_strict(
                "(function() {"
                "  var anchors = document.querySelectorAll('a[href]');"
                "  for (var i = 0; i < anchors.length; i++) {"
                "    var anchor = anchors[i];"
                "    var target;"
                "    try { target = new URL(anchor.href, location.href); } catch (e) { continue; }"
                "    if (target.hostname !== 'chatgpt.com') { continue; }"
                "    var parts = target.pathname.split('/').filter(Boolean);"
                "    for (var j = 0; j + 1 < parts.length; j++) {"
                "      var projectSegment = parts[j + 1];"
                "      var exactProject = parts[j] === 'g' && ("
                "        projectSegment === __D.gizmo_id ||"
                "        projectSegment.indexOf(__D.gizmo_id + '-') === 0"
                "      );"
                "      if (exactProject) {"
                "        anchor.click();"
                "        return JSON.stringify({result: 'clicked', href: target.href});"
                "      }"
                "    }"
                "  }"
                "  return JSON.stringify({result: 'not-found', href: location.href});"
                "})()",
                {"gizmo_id": gizmo_id},
            )
            state = json.loads(raw)
        except Exception as exc:
            raise ProjectNavigationError(
                f"Project navigation probe failed for {gizmo_id}: {type(exc).__name__}: {exc}"
            ) from exc
        return state.get("result") == "clicked", str(state.get("href") or "")

    async def navigate_new_chat(self, gizmo_id: str = None) -> None:
        """Navigate to a fresh chat and fail closed if project scope is uncertain."""
        if not gizmo_id:
            await super().navigate_new_chat(gizmo_id=None)
            return

        logger.info("Navigate to ChatGPT shell before entering project: %s", gizmo_id)
        await super().navigate_new_chat(gizmo_id=None)

        clicked, href = await self._click_project_link(gizmo_id)
        if not clicked:
            raise ProjectNavigationError(
                f"Project navigation failed for {gizmo_id}: exact project link was not found "
                f"from {href or 'the ChatGPT application shell'}"
            )

        ready, diagnostic = await self._wait_for_project_ready(gizmo_id)
        if not ready:
            raise ProjectNavigationError(
                f"Project navigation failed for {gizmo_id} after client-side transition: {diagnostic}"
            )

        logger.info("Project page ready after client-side recovery: %s", gizmo_id)
        await asyncio.sleep(2)
        self._current_conv_id = None
