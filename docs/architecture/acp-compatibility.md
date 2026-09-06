# ACP compatibility campaign

Measured on 2026-09-06, Linux x86_64, using Greatminds' common `AcpTransport`
and `agent-client-protocol==0.11.1`. Each executable ran in a fresh temporary
workspace. Initialization and session probes do not send model prompts. A separate
explicit inference stage sends one synthetic text request per tested installation.

## Recorded initialization results

| Harness | Tested installation | ACP argv | Initialize |
| --- | --- | --- | --- |
| Codex | codex-acp 1.10.0, Codex CLI 0.149.1 selected with `CODEX_PATH` | `codex-acp` | Protocol 1 accepted |
| Claude | claude-agent-acp 0.75.1, Claude Agent SDK 0.3.257 | `claude-agent-acp` | Protocol 1 accepted |
| Qwen Code | 0.19.6 | `qwen --acp` | Protocol 1 accepted |
| Kimi Code | 0.39.1 | `kimi acp` | Protocol 1 accepted |
| Grok | 1.0.13, build 5e9a58528b76 | `grok --no-auto-update agent stdio` | Protocol 1 accepted |
| Cline | 3.0.50 | `cline --acp` | Protocol 1 accepted |
| Gemini CLI | 0.49.0 | `gemini --acp` | Protocol 1 accepted |
| OpenHands CLI | 1.16.0 | `openhands acp` | Protocol 1 accepted |
| Cursor CLI | 2026.06.15-18-00-12-6f5a2cf | `cursor-agent acp` | Protocol 1 accepted |

The [recorded JSON](evidence/acp-initialize-2026-09-06.json) retains the protocol
responses with arbitrary metadata, host paths, process IDs, and raw logs excluded.
Capabilities in that file are **advertised**, not exercised. Initialization proves
that these installations speak to the shared client; it does not yet establish
tool permissions, task execution, resumption, cancellation during inference, or a
mixed-agent pipeline. Those remain required for support.

## Session and minimal inference results

| Harness | Session without explicit authenticate | Exact synthetic reply |
| --- | --- | --- |
| Codex CLI 0.149.1 / adapter 1.10.0 | Created | Failed: selected model requires newer CLI |
| Codex CLI 0.153.4 / adapter 1.10.0 | Created | Passed |
| Claude SDK 0.3.257 / adapter 0.75.1 | Created | Passed |
| Grok 1.0.13 | Created | Passed |
| Cline 3.0.50 | Created | Not tested |
| Qwen Code 0.19.6 | Authentication required (-32000) | Not tested |
| Kimi Code 0.39.1 | Authentication required (-32000) | Not tested |
| Gemini CLI 0.49.0 | Authentication required (-32000) | Not tested |
| OpenHands CLI 1.16.0 | Authentication required (-32000) | Not tested |
| Cursor CLI 2026.06.15-18-00-12-6f5a2cf | Authentication required (-32000) | Not tested |

[Session and prompt evidence](evidence/acp-sessions-prompts-2026-09-06.json)
records these separate stages. The prompt asks for exactly `ACP_PROBE_OK` without
tools. Passing means the streamed text matches, the agent returns `end_turn`, and
the process exits during bounded shutdown. No tool or permission requests occurred
in those prompts. This is not a domain task or mixed-agent pipeline test.

The older Codex returned a provider error as assistant text followed by `end_turn`.
The exact-response assertion correctly rejected it. A stop reason alone is not
proof of useful work; production task advancement requires validated typed results.
The separately installed, lockfile-pinned Codex 0.153.4 passed the same request.

An authentication-required response does not prove that cached credentials are
absent. Some servers require explicit ACP authentication before using an existing
login. Manifests may specify `auth_method`; the supervisor verifies it was advertised
and calls `authenticate` before creating or loading a session. It does not guess a
method or silently choose another identity. Real explicit-auth flows remain untested.

## Restart, context, configuration, and cancellation

| Harness | Load after process restart | Recall previous-turn token | Cancel after first assistant text | Explicit config selection |
| --- | --- | --- | --- | --- |
| Codex CLI 0.153.4 / adapter 1.10.0 | Passed | Passed | `cancelled` | Model `gpt-6-astra`, mode `read-only` confirmed |
| Claude SDK 0.3.257 / adapter 0.75.1 | Passed | Passed | `cancelled` | Model `default`, mode `default` confirmed |
| Grok 1.0.13 | Passed | Passed | `cancelled` | No model config option advertised; not tested |

[Lifecycle evidence](evidence/acp-lifecycle-2026-09-06.json) records each stage.
Restart tests close the first agent process, then create another in the same
workspace and call `session/load` with the privately retained session ID. The
history test uses a different second prompt which does not contain the token.
Historical updates emitted during loading are excluded from the second response
assertion. This proves continuity for these individual harnesses; it does not
transfer conversation history across different harnesses.

Cancellation tests wait for nonempty assistant text before sending `session/cancel`.
All three returned `cancelled` and exited during bounded shutdown. Completion
before the cancellation request is explicitly reported as unconfirmed, not passed.
These tests do not yet cover cancellation while a tool or permission callback is
pending. The supervisor and probe share advertised model/mode selection code;
model configuration responses must confirm the requested value.

OpenHands and Cursor initially could not create their normal per-user runtime
directories under the filesystem-restricted test runner. Gemini initially could
not reach its OAuth refresh endpoint. Codex initially could not initialize its
SQLite runtime. Repeating initialization with normal filesystem/network access
resolved these startup failures. The test selected the already installed Codex
CLI explicitly; the adapter's separately installed dependency is Codex 0.153.4.
That bundled combination subsequently passed the session and minimal inference
stage above. The global CLI installation was not updated.

## Reproduce the probe

From an editable Greatminds checkout:

```bash
.venv/bin/python tools/acp_probe.py -- qwen --acp
.venv/bin/python tools/acp_probe.py --session -- kimi acp
CODEX_PATH=/absolute/path/to/codex .venv/bin/python tools/acp_probe.py \
  --env CODEX_PATH -- /absolute/path/to/codex-acp
.venv/bin/python tools/acp_probe.py --timeout 45 \
  --prompt 'Reply with exactly ACP_PROBE_OK and nothing else. Do not call any tools.' \
  --expect ACP_PROBE_OK -- /absolute/path/to/claude-agent-acp
.venv/bin/python tools/acp_probe.py --timeout 45 --resume \
  --prompt 'Remember TOKEN_42. Reply only SAVED.' --expect SAVED \
  --resume-prompt 'Return only the token from my previous message.' \
  --resume-expect TOKEN_42 -- /absolute/path/to/codex-acp
.venv/bin/python tools/acp_probe.py --timeout 45 --cancel-after 0.1 --cancel-on-output \
  --prompt 'Write the integers from 1 to 3000, one per line. Do not use tools.' \
  -- /absolute/path/to/codex-acp
```

The default probe sends only `initialize` and performs bounded transport shutdown.
`--session` additionally creates a session. `--auth-method ID` explicitly selects an
advertised authentication flow, which may require interactive login depending on
the server and cached account. `--prompt TEXT` explicitly sends a model request and
may consume provider usage; `--expect TEXT` checks the observed assistant response.
`--model ID` and `--mode ID` require advertised choices. `--resume` restarts the
process and loads its session; `--resume-prompt` and `--resume-expect` check a distinct
follow-up. `--cancel-after` tests cancellation of a pending prompt; add
`--cancel-on-output` to wait for streaming text first. Resume and cancellation are
separate scenarios and cannot be combined in one invocation.
It prints a versioned JSON report and returns nonzero on failure. Each probe retains
a private report, process identity, stderr, and any assistant response in its temporary
directory. Extra environment values are passed only through explicit `--env NAME`
references; their values, assistant text, and arbitrary protocol metadata are excluded
from stdout. Sensitive values must not be placed in executable arguments.

The [adapter package definition](../../tools/acp_adapters/package.json) and
[npm lockfile](../../tools/acp_adapters/package-lock.json) pin the Codex and Claude
test adapter installations, including resolved dependencies. Copy those two files
to a separate writable test directory and run `npm ci --ignore-scripts --no-audit
--no-fund` there. Select executables from that directory's `node_modules/.bin/`.
The Node runtime used for the recorded test was 22.13.0. Installing test dependencies
does not alter the global harness installations or a project's execution bindings.

## Distribution references

[Codex's upstream adapter](https://github.com/agentclientprotocol/codex-acp)
exposes ACP over the Codex App Server and supports an explicit `CODEX_PATH` override.
[Claude's upstream adapter package](https://github.com/agentclientprotocol/claude-agent-acp)
uses the Claude Agent SDK. These adapters stay at the executable boundary; the
Greatminds scheduler has no separate Codex or Claude wire protocol path in ACP mode.

[Grok documents its stdio ACP command and update suppression](https://docs.x.ai/build/cli/headless-scripting).
[OpenHands documents its ACP CLI entrypoint](https://docs.openhands.dev/openhands/usage/cli/ide/overview),
and [Cursor documents ACP sessions and extension requests](https://cursor.com/docs/cli/acp).
Blocking extension requests and provider-specific optional capabilities must be
explicitly negotiated or handled before the corresponding workflows can be declared
supported. A listed command or an accepted initialization response is not a substitute
for those tests.
