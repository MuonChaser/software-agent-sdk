# Auto-title fallback and failure signaling (#6250)

Auto-title generation tries the selected title profile first, then the running
agent's LLM once, then truncates the first user message. ACP's `acp-managed`
placeholder is never called. A successful fallback publishes no error. If title
calls still fail, one existing `ConversationErrorEvent` carries
`code="TitleGenerationError"` and explicit
`{kind: "unknown", retryable: false, user_action: "none"}`. Main-agent error
publication keeps its existing code and classification.

Both attempts use the existing title helper, system-first messages, conversation
call context, prompt override, 1,000-character input cap, `store=False`, and title
length limit. Each LLM keeps its own model, endpoint, credentials and transport
configuration; profile configuration/secrets are not copied onto the agent.
The agent fallback receives the same title prompt and message excerpt. Existing
per-LLM retry policies remain; this change adds at most one agent title-generation
invocation and never cycles back to the profile. Both LLM configuration objects are excluded
from the enclosing trace's recorded inputs.

## Reproduction

Base: `d0d9d81324210414462fb676b377e0563a08a0cb`.
Linux/WSL, Python 3.13.17, repository uv lock; synthetic credentials only.

The new parametrized HTTP test starts the real FastAPI server on a loopback
socket, creates a conversation via REST, loads a profile from disk, sends a user
message through REST, and reads persisted metadata and events through REST.
Only the LiteLLM transport boundary is replaced with deterministic responses.
It does not run the agent; the complementary EventService test does run the real
agent to its `finish` tool while auto-titling executes in the background.

From this PR's checkout, reproduce against the unchanged base:

```bash
git worktree add --detach ../sdk-6250-before d0d9d81324210414462fb676b377e0563a08a0cb
cp tests/cross/test_remote_conversation_live_server.py ../sdk-6250-before/tests/cross/
cd ../sdk-6250-before
uv sync --frozen --dev
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 \
  uv run pytest tests/cross/test_remote_conversation_live_server.py -k auto_title_profile_failure -q
```

Observed on base: **2 failed**. The success case saved `Fix login` rather than
`Agent-generated title`; the both-failed case published `LLMAuthenticationError`
rather than `TitleGenerationError`. Only the profile was called.

Run the same pytest command from this PR's checkout: **2 passed**. The first
case records the agent-generated title and no error; the second truncates and
publishes exactly one title-specific event with non-auth classification.

## Regression and validation

```bash
uv run pytest tests/agent_server/test_auto_title_fallback.py -q
uv run pytest tests/agent_server/test_auto_title_span_metadata.py -q
uv run pytest tests/agent_server/test_conversation_service.py -q
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy \
  NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1 \
  uv run pytest tests/agent_server -q
uv run pre-commit run --all-files
cd clients/typescript
npm ci
npm run build
npm test -- event-types.test.ts
```

- Auto-title EventService matrix: **9 passed**, including successful profile,
  fallback success/failure, missing/corrupt/absent profile and ACP sentinel.
  It checks the actual decrypted profile key/endpoint and the separate agent
  key/endpoint at the transport, custom title prompt, saved metadata, published
  error count/classification, healthy main run and normal auth classification.
- Existing conversation-service tests: **139 passed** in the focused run.
- Real Laminar span probe: **2 passed** (normal and profile → agent fallback).
- TypeScript build passed; event-type tests: **21 passed**. No schema or client
  type change is needed: code is already a string and `unknown`/`none` are
  existing classification values. The real REST test checks serialized output.
- Full Agent Server suite: **2,872 passed, 1 skipped, 19 deselected**.
  Repository-wide pre-commit passed; existing main-auth/classification tests: **25 passed**.
  An initial broad run was interrupted after proxy contamination
  produced localhost credential-callback HTTP 502s; all 34 credential tests pass
  with proxy variables cleared. No product change was made for that environment.

## Canvas boundary and limits

Inspected current `OpenHands/OpenHands` main at
[`dbe578d5dbac60a584b7b9ffeb23ec7005779325`](https://github.com/OpenHands/OpenHands/commit/dbe578d5dbac60a584b7b9ffeb23ec7005779325):

- [`conversation-websocket-context.tsx`](https://github.com/OpenHands/OpenHands/blob/dbe578d5dbac60a584b7b9ffeb23ec7005779325/src/contexts/conversation-websocket-context.tsx#L677-L698)
  passes the supplied code/classification into the error store.
- [`type-guards.ts`](https://github.com/OpenHands/OpenHands/blob/dbe578d5dbac60a584b7b9ffeb23ec7005779325/src/types/agent-server/type-guards.ts#L295-L300)
  still displays every `ConversationErrorEvent` as a banner.
- [`error-message-banner.tsx`](https://github.com/OpenHands/OpenHands/blob/dbe578d5dbac60a584b7b9ffeb23ec7005779325/src/components/features/chat/error-message-banner.tsx)
  displays `unknown` as a generic error, and title errors have no ACP auth header
  or reauthentication action. The error store remains sticky until dismissed
  or cleared by user activity. There is no title-code suppression today.

Thus fallback success removes the erroneous event entirely. When both attempts
fail, this PR supplies a distinguishable non-auth event and leaves execution
healthy; **it does not remove Canvas's generic banner**. Suppressing that banner
would be a separate Canvas change, outside the issue's current bounded scope.
This is source inspection plus backend/transport validation, not a browser UI test.
No private Enterprise source, Cloud deployment, rotated managed proxy key, live
paid provider, GPU, Docker image or packaged binary was independently validated.
The backend change does not resolve the underlying rejected title-profile key.

AI assistance was used for implementation, tests, review and this evidence note.
