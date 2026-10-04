# Local transport validation — 2026-10-04

Local changes on base revision `d2dab59b0137eb63a4b3a1cf1d5516f40fa4842a`, requested by the user while integrating the agent-context broker with Vexa Creator. No commit or push was performed.

## Fixes and invariants

`CompletionDetector` now observes the anchored backend terminal text every three seconds while waiting for an assistant DOM node. The current ChatGPT UI no longer exposes the old message-role attributes. Only non-empty, completed text correlated to the submitted turn is accepted. The driver retains its final anchored reconciliation. Reads never resend a prompt, and request UUIDs never become conversation IDs. Auth expiry and persistent rate limits propagate.

`ChatGPTDom` uses the first visible, enabled matching element for composer readiness, focus, clearing, exact-text verification and sending. Continued conversations render hidden responsive duplicates before the active editor, so a plain `querySelector` can choose the wrong element.

Four existing test fixtures were corrected: transient verified identity is legitimate detector state; successful send acknowledgment mocks structured final reconciliation; empty or unverified responses must fail closed, rather than return an empty success. Eleven phase-1 regression cases were added, including identical prompts with old/new replies, empty text, unresolved conversation IDs, ambiguity, auth and rate limits.

## Evidence

- Full standard suite after both fixes: **721 passed**, 31 real-account E2E cases deselected, 272.14 seconds. Command: `.venv/Scripts/python.exe -m pytest -q -m 'not e2e' -p no:cacheprovider`.
- Ruff passes on all changed Python files.
- Real broker fresh turn: JSON result 42, unique completion marker, verified project/conversation IDs and released worker.
- Real continuation: result 47 derived from the prior 42, same conversation, both turns retained, released worker. A failed pre-send composer attempt was retried only after backend history confirmed no message had been sent.
- Real Vexa `OrchestratorLLM.generate()`: parsed JSON result 42 after stripping the marker, terminal run and released worker. Reproduction from the Vexa root: `.venv/Scripts/python.exe scripts/verify_orchestrator.py --project Other`.
- Reloaded REST `/v1/chat/completions`: JSON result 56 with exact fresh nonce and verified conversation identity.

These were isolated diagnostics in the existing `Other` project. No account creation, cookie extraction, publication, video upload or paid API call occurred. Concurrent generation, non-text media and the full Vexa video pipeline are outside this validation. Vexa's configured `Vexa Creator` project is still absent; its production configuration was not silently redirected to `Other`.
