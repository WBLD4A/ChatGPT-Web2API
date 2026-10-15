# Conversation attachments

The MCP `chat_completion` tool accepts an optional `attachments` array. Each
item contains a safe plain `name`, a `mime_type`, and strict base64 `content`.
The limits are 10 files, 10 MiB per file, and 20 MiB decoded bytes per turn.
Filenames must be unique without regard to case. Caller-controlled local paths
are never accepted.

```json
{
  "message": "Read the attached note",
  "project_id": "g-p-example",
  "attachments": [
    {"name": "note.txt", "mime_type": "text/plain", "content": "SGVsbG8="}
  ]
}
```

Files are uploaded through the active ChatGPT composer using Chrome's native
`DOM.setFileInputFiles`. Chrome and the MCP worker must share the filesystem.
The worker creates temporary files, verifies the filenames in the composer,
waits for upload indicators to finish and the send control to become enabled,
and then sends the prompt. Account and model restrictions still apply.
Temporary local files are removed after upload verification, including failure
and cancellation. Pending browser state is cleared by reloading after an
attachment turn; failed cleanup quarantines that driver until it is reconnected.
Missing document input, upload rejection or readiness timeout fails before send.
No file is converted into prompt text.

This contract is implemented for `chat_completion` over MCP, the interface
consumed by agent-context's broker. `chat_with_gpt` and the REST chat endpoint
have not been extended. Existing text-only calls keep their arguments and flow.

The selectors have deterministic mocked regression coverage. Verification in a
real ChatGPT conversation remains required after upstream UI changes; a passing
mock test does not establish support for a specific account, model or file type.
See agent-context's `orchestrator/README.md` for GPT Action setup and file routing.
