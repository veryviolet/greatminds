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
method or silently choose another identity. Fresh explicit-auth attempts and their limitations are recorded below.

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

## Live permission callbacks and operator responses

[Permission evidence](evidence/acp-permissions-2026-09-06.json) records a synthetic
shell command that writes `permission-ok` to a file in a temporary project. Claude
0.75.1/SDK 0.3.257 requested approval; `greatminds run permission ID --option ID`
answered once, the same run consumed it, and the file appeared before turn completion.
Grok and Codex also passed with the explicit settings below. For those two explicit
tests the file was directly observed absent while the request was pending.

The initial Grok run inherited `ui.permission_mode = auto` and performed the write
without an ACP callback. Using `grok --no-auto-update --permission-mode default
agent --no-leader stdio` produced the callback without changing user configuration.
Codex's default `agent` mode also performed the ordinary workspace write without
a callback. Its `read-only` mode delegates escalated approvals to the user but still
allows ordinary workspace writes in this adapter version. The successful callback
test selected that mode and explicitly requested `sandbox_permissions=require_escalated`
for the harmless command. These are different approval triggers, not proof that
every shell command in every harness requires an ACP approval.

The client `ask` policy routes callbacks to the operator; it does not override a
harness's internal approval defaults. The executable's mode, cached settings, and
execution boundary remain part of compatibility configuration. Complete permission
policy parity and negotiated filesystem/terminal callbacks remain pending.

The supervisor permission probe (`tools/acp_permission_probe.py` in the source checkout) makes the
scenario reproducible. It prints a temporary project/runtime location, submits one
synthetic model request, and waits for an operator. It never approves a request.
From that project's operator terminal, inspect `greatminds run status`, then inspect
and answer its exact permission ID. The probe requires an observed pending request
before the marker exists, a consumed one-time approval, and the expected file.

```bash
.venv/bin/python tools/acp_permission_probe.py \
  --adapter-version 0.75.1 --harness-version sdk-0.3.257 \
  -- /absolute/path/to/claude-agent-acp
```

Fixture tests additionally cover explicit rejection, cancellation during a pending
callback, stale revisions, expired requests, conflicting replies, and SIGKILL before
delivery of both pending and answered requests. Restart cancels those requests and
preserves the input hold without spawning another agent. These crash/denial cases
have not yet been repeated across all real executables.

OpenHands and Cursor initially could not create their normal per-user runtime
directories under the filesystem-restricted test runner. Gemini initially could
not reach its OAuth refresh endpoint. Codex initially could not initialize its
SQLite runtime. Repeating initialization with normal filesystem/network access
resolved these startup failures. The test selected the already installed Codex
CLI explicitly; the adapter's separately installed dependency is Codex 0.153.4.
That bundled combination subsequently passed the session and minimal inference
stage above. The global CLI installation was not updated.

## Mixed domain pipeline

The [first real pipeline evidence](evidence/acp-mixed-pipeline-2026-09-06.json)
is **partial**. A local feature task uses a task worktree and six configured
unit tests for an inclusive numeric clamp function, including invalid bounds.
Codex 0.153.4 implemented the function, requested daemon-run tests, and submitted
a typed implementation handoff. Claude SDK 0.3.257 independently inspected it,
requested its own test execution, and submitted a typed tests handoff. Both
receipts passed domain gates and the task reached `feature_review`.

Grok 1.0.13 initially exhausted its 240-second prompt budget while waiting for an
operator permission. The [completed continuation](evidence/acp-mixed-pipeline-completed-2026-09-06.json)
records an explicit reviewer retry with a 600-second budget and one-time operator
approvals. Grok independently ran all six tests and submitted an approved review
through `run submit --json`. The daemon applied all three results, merged the
implementation and transitioned the task to `verified`, with worktree cleanup. All six
tests pass in the main checkout; an idle daemon restart changes no runs, results,
or commands. This is operator-assisted evidence for one synthetic local task,
not an unattended pipeline or a controlled performance comparison.

The pipeline probe (`tools/acp_pipeline_probe.py` in the source checkout) takes `--config FILE`
with three ACP bindings for DEVELOPER, TESTER, and ARCHITECT-REVIEWER. It copies
the manifests into a fresh temporary Git project, seeds a synthetic local plan,
and replaces configured commands with the known local unit-test command. It
never approves permissions or silently retries a held run. Its full fixture
regression covers all three role transitions, evidence validation, daemon merge,
worktree cleanup, main-branch test execution, and restart without duplicate work.
That fixture result does not establish completion of the real pipeline.

The real run exposed two integration issues. Assigned context now identifies the
CLI through the daemon's Python executable rather than assuming `greatminds` is
on PATH; the final implementation also isolates module lookup from workspace
files. Explicit `stand_required: false` plans no longer require fabricated stand
observations. Local test blocks and configured command evidence still pass domain
validation; stand-required or unspecified plans retain scope-specific stand gates.

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

The adapter package definition (`tools/acp_adapters/package.json`) and
npm lockfile (`tools/acp_adapters/package-lock.json`) pin the Codex and Claude
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

## Cline revalidation on 2026-09-07

The executable currently on this host reports Cline **3.0.61**, whereas the earlier
session-only evidence records 3.0.50. A fresh common-client probe created a session
and sent a synthetic exact-response request without tools. `session/prompt` returned
JSON-RPC `-32603` (`Internal error`) with no assistant text. The private stderr
diagnostic identified required re-authentication. The process exited during bounded
cleanup, and the requested restart/load stage was skipped because the initial prompt
failed. No account or model selection was changed.

[Sanitized evidence](evidence/acp-cline-auth-2026-09-07.json) records this failure.
Cline task execution, resumption and cancellation remain unverified until login is
restored and the same scenarios pass. Creating an ACP session does not establish
that the configured provider credentials can execute a prompt.

## One-harness local preset on 2026-09-07

A fresh temporary Git repository completed the local clamp task using ordinary
`greatminds setup`, `project preset local --agent codex --apply`, and the common
daemon. The manifest used Codex 0.153.4 with codex-acp 1.10.0. The preset kept
`permission: ask`; this task generated no permission callbacks. One manifest
served developer, tester and reviewer in separate runs, while the planner remained
on demand. The task supplied an explicit synthetic plan with no stand requirement.

[Recorded evidence](evidence/acp-local-preset-2026-09-07.json) confirms three applied
typed handoffs, three successful daemon command receipts, SYSTEM provenance for
all resulting decision blocks, a merge changing only `clamp.py`, removal of the
task worktree, six independently rerun unit tests, and restart without additional
runs/results/commands. No native transport, deployment or publication was involved.

| Role | Compiled context bytes | Start to first prompt | Run duration |
| --- | ---: | ---: | ---: |
| Developer | 7,887 | 2.691 s | 45.051 s |
| Tester | 9,347 | 1.090 s | 46.685 s |
| Reviewer | 10,419 | 1.716 s | 32.709 s |

These are observed client byte counts and elapsed durations for this task, not
provider token counts, billed cost, or proof of useful work at the first activity
boundary. The source environment was used for live inference; wheel installation
and interactive planning are separate checks. Reproduce from a checkout with an
execution YAML containing the named manifest:

```bash
.venv/bin/python tools/acp_pipeline_probe.py --config /path/to/execution.yaml --local-agent codex
```

This opt-in command consumes provider usage, creates a new temporary repository,
and keeps operator permissions explicit. Its validation commands are deliberately
replaced by the synthetic task's Python unit tests.

A separate [installed-wheel fixture run](evidence/installed-local-preset-2026-09-07.json)
used a fresh isolated environment with the base wheel and no Ansible extra. The
module resolved from site-packages. The public setup/preset commands and a synthetic
ACP server completed the same three-role task, command checks, merge, cleanup and
restart assertions. A separate installed dependency smoke confirmed idempotent setup
and one SYSTEM resume with zero agent runs. This packaging evidence is distinct
from the live Codex result above.

## Authentication revalidation, 2026-09-07

[Fresh evidence](evidence/acp-auth-revalidation-2026-09-07.json) separates session
creation, explicit authentication and inference. Existing installations and account
settings were used; no provider or identity was selected implicitly.

| Harness | Fresh result | Remaining prerequisite |
| --- | --- | --- |
| Qwen 0.19.6 | Only `openai` advertised; explicit authentication returned -32603 | Installed method requires `OPENAI_API_KEY`, absent in the probe environment |
| Kimi 0.39.1 | Session and explicit `login` returned -32000 | Complete native login; doctor found no local configuration |
| Cline 3.0.61 | Session created; synthetic prompt returned -32603 without assistant text | Native re-authentication, identified by private stderr |
| OpenHands 1.16.0 | Local session returned -32000 | Configure agent/model: installed local handler maps missing agent specification to this response |
| Gemini 0.49.0 | Configured `oauth-personal` explicitly rejected | Individual access through this client was discontinued; an applicable supported access method is required |
| Cursor 2026.06.15-18-00-12-6f5a2cf | Native status reports cached login; explicit `cursor_login` timed out after 30 seconds | Resolve ACP authentication; underlying cause remains inconclusive |

Gemini's response agrees with the [upstream individual-access announcement](https://github.com/google-gemini/gemini-cli/discussions/28017).
It is not evidence of missing cached credentials. Cursor's documented
[ACP authentication sequence](https://cursor.com/docs/cli/acp) was attempted;
a cached native login does not prove an ACP session works. No inference was
requested in these auth checks except the separate synthetic Cline attempt.
All probe processes exited during bounded cleanup.

## Cancellation while permission is pending, 2026-09-07

[Live cancellation evidence](evidence/acp-pending-permission-cancel-2026-09-07.json)
confirms Claude adapter 0.75.1 / SDK 0.3.257, Codex adapter 1.10.0 / CLI 0.153.4,
and native Grok 1.0.13. The common supervisor task was cancelled after observing
an unanswered permission. Every permission became cancelled, the requested write
marker remained absent, and the process group exited. The probe supports this
scenario with `--cancel-while-pending`; its fixture covers the same assertions.

Codex required an explicit escalated shell request; selecting `read-only` alone
is not evidence that ordinary workspace writes ask for permission. Grok used
explicit default permission mode. Claude emitted `$/cancel_request`, which the
pinned SDK logged as method-not-found; required cleanup still passed. This scope
proves local cancellation and cleanup, not provider billing cancellation, an ACP
cancelled stop response, public CLI cancellation, or optional extension parity.

## Declared role coverage, 2026-09-07

This table records live evidence, not restrictions on manifest role assignment.
**D** = completed domain role with accepted typed result; **C** = synthetic live
conversation through the public CLI, including restart and recall; **P** = only a synthetic
DEVELOPER-bound permission test; **U** = no live domain-role proof; **B** = broader
live campaign cannot proceed past the auth/configuration issue above. USER is a
human role and SYSTEM actions are deterministic daemon work, so neither is a
harness assignment. Generic lifecycle tests do not fill domain-role cells.

| Role | Codex | Claude | Grok | Qwen | Kimi | Cline | OpenHands | Gemini | Cursor |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| ARCHITECT-PLANNER | C | C | C | B | B | B | B | B | B |
| ARCHITECT-REVIEWER | D | U | D | B | B | B | B | B | B |
| DEVELOPER | D | P | P | B | B | B | B | B | B |
| UI-DEVELOPER | U | U | U | B | B | B | B | B | B |
| LIVE-DEVELOPER | C | C | C | B | B | B | B | B | B |
| TECHNICAL-WRITER | U | U | U | B | B | B | B | B | B |
| TESTER | D | D | U | B | B | B | B | B | B |
| READER | U | U | U | B | B | B | B | B | B |
| EXPLORER | U | U | U | B | B | B | B | B | B |
| MAINTAINER | U | U | U | B | B | B | B | B | B |

D cells derive from the [single-harness local preset](evidence/acp-local-preset-2026-09-07.json)
and [operator-assisted mixed pipeline](evidence/acp-mixed-pipeline-completed-2026-09-06.json).
P cells derive from [permission approval](evidence/acp-permissions-2026-09-06.json)
and pending-permission cancellation above. This is deliberately narrower than
all roles on all harnesses. G6 and overall modernization acceptance remain open.

The offline conversation acceptance case separately exercises all ten agent roles:
two FIFO user turns share one daemon-owned ACP session, preserve streamed events,
create no domain result and do not relaunch after an idle restart. All ten cases
passed on the subprocess fixture. This confirms the common role-independent
conversation mechanism, not live domain coverage; the matrix above is unchanged.

## Public conversation restart and reconnect, 2026-09-07

[Live evidence](evidence/acp-public-conversations-2026-09-07.json) records six
scenarios: ARCHITECT-PLANNER and LIVE-DEVELOPER on each tested Codex, Claude and
Grok installation. The reproducible tool is `tools/acp_conversation_probe.py`: pass
`--config` with a manifest file and `--agent` with its name. It creates a disposable
project and uses public `chat create`, `chat send`, `chat attach --after` and
separate `coordd --once` processes. It uses model usage and denies permissions.

Every first reply matched a random token. The next daemon process loaded the same
ACP session and recalled that token, which the second prompt did not contain.
Duplicate delivery of the second request ID created no extra turn. Cursor reads
left the conversation journal unchanged and excluded the first turn's events.
Both runs completed, their process groups exited, and an idle restart preserved
runs, results and commands. No domain result was created.

The subprocess fixture verifies this orchestration and rejects a peer without
loadSession. Live evidence is specific to synthetic conversations: it does not
prove domain plan generation, live code editing, mid-turn crash recovery, streaming
terminal detach, Quick Picks or vendor extensions. C cells therefore remain
distinct from D cells. G6's other installations still need their recorded access
or configuration issues resolved before full live scenarios can run.
