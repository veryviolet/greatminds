# Rehabilitation audit — 2026-09-06

Status: initial code audit and proposed direction, not an implemented redesign.
Baseline: `b8b13f1`, package version `2.6.0`, Python `3.13.7`.

## Product objective

Run the supported modern coding harnesses in any product role. Keep everything
that does not require an LLM in deterministic daemon/domain code. Agents should
spend their turns planning, implementing, reviewing, and interpreting evidence.
Scheduling, recovery, state bookkeeping, and mechanically decidable workflow
steps must not depend on an agent remembering a prompt instruction.

The repository's June solo handoff agrees with developing greatminds as a normal
Python project. Historical fleet transcripts are background, not current task
state. This audit did not start a fleet or run inference through external agents.

## Current architecture worth preserving

- Filesystem task state: queue directories, typed YAML blocks, transition
  validation, task locks, intent files, and an append-only journal.
- Separation of editable `coordination/` configuration from `.greatminds/`
  runtime data, with legacy layout fallbacks.
- `coordd`: event watching, backlog reconciliation, driven subprocesses,
  pending work, persisted retries, hang reporting, and stand deployment.
- Worktrees and singleton stand leases, with Ansible profiles and evidence gates.
- CLI operator surfaces, driven event logs, tmux launching, and a small VS Code
  extension using the same CLI backend.
- A useful beginning of an adapter boundary in `src/greatminds/agents/`.

No database or orchestration framework replacement is justified by this audit.
First make the existing filesystem transactions and recovery behavior explicit.

## Existing harness support

This table describes the checked-in implementation, not live compatibility with
every currently released binary.

| Harness | Interactive launch | Driven execution | Greatminds session continuity |
| --- | --- | --- | --- |
| Claude Code | Yes | `claude -p` | Session IDs and resume |
| Codex | Yes | app-server over stdio | Thread IDs; separate interactive discovery |
| Cursor | Yes, systemd scope | Headless subprocess | Interactive `--continue`; driven is one-shot |
| Cline | Yes | Headless JSON subprocess | Driven is one-shot |
| Gemini CLI | Yes | Headless subprocess | Driven is one-shot |
| OpenHands | Chat advertised | Headless subprocess | Driven is one-shot |
| Qwen Code | Absent | Absent | Absent |
| Kimi Code CLI | Absent | Absent | Absent |
| Grok Build | Absent | Absent | Absent |

The registry lists support statically. It does not establish compatibility with
an installed binary version or negotiate transport capabilities.

## Deterministic work still delegated to agents

| Mechanism | Current boundary | Proposed owner |
| --- | --- | --- |
| Periodic recovery | `MAINTAINER` self-loop and prompt-driven wake rearming | Daemon scheduler; OS supervisor restarts the daemon itself |
| Dead process recovery | Daemon reports; maintainer diagnoses and invokes restart | Supervisor executes known bounded recovery; LLM handles unexplained failures |
| Blocked dependency completion | `wake-check` reports readiness; reviewer performs resume | Dependency reconciler validates and applies the declared resume transition |
| Liveness/progress | Successful CLI calls touch role heartbeats | Process/transport observations, separate activity and domain-progress timestamps |
| Stand lease expiry | Maintainer contract includes reclaiming expired dead-holder leases | Lease controller with holder identity and race-safe checks |
| Workflow bookkeeping | Agent reads graph, appends blocks, chooses and invokes moves | Shared domain service validates structured results and commits eligible transitions |
| Test/deployment evidence | Agents invoke commands and write evidence fields; stand executor already runs deployment | Deterministic runners record command, exit status, revision, artifacts, and timestamps |
| Tick context | Agent rereads the full schema and coordination guide | Context builder supplies role, assigned work, relevant rules, and changes |

Automatic transitions must preserve role attribution, review decisions, and
existing gates. The daemon must not impersonate a reviewer by setting an
environment variable. Introduce an explicit system actor for mechanical
transitions, retaining the originating agent decision and evidence.

Running an agreed test command and recording its exit status is deterministic.
Choosing whether those tests are adequate, explaining failures, and deciding
whether an implementation meets intent still require judgment.

## Findings and concrete code seams

1. **The driver abstraction is thin.** `cli/coordd.py` is 4,125 lines and
   contains subprocess management, retries, locks, Codex JSON-RPC, stand work,
   and notifications. `agents/driven_drivers.py` chooses between callbacks into
   this module. Extract execution lifecycle and transports behind an actual
   runtime contract, retaining existing CLI entrypoints during migration.

2. **Role and execution mode remain coupled.** Driven dispatch requires both
   `schema.roles[role].lifecycle == driven` and a driven window mode
   (`coordd._maybe_drive_driven_role`). Maintainer is fixed to `self-loop`.
   Move scheduling policy into role bindings, separate from role obligations.
   `AgentToolSpec.start_modes` is displayed, but `start-agent` does not validate
   the requested mode against it; for example OpenHands advertises chat only.

3. **Context is oversized and uneven.** `data/bootstrap.md` explicitly demands
   rereading the whole schema and coordination guide every tick: 1,437 and 720
   lines respectively. Generic driven turns receive this bootstrap instead of
   an assigned work item. Generic interactive launches also inherit the common
   short “continue” prompt on subsequent starts despite not receiving a session
   ID. Build a harness-neutral, versioned work/context envelope.

4. **Config delivery has drifted.** Setup generates Codex role instructions and
   skill configuration under role homes, while active launch code uses the
   machine home and reads only the role model through `codex_auth.py`.
   Interactive session discovery still prefers historical per-role rollout
   directories. Review delivery of role instructions and skills, and record
   session identity explicitly. Claude, Codex, and Cursor model selection also
   follow different paths; generic driven argv builders have no common
   per-binding provider/model/options input.

5. **Schema authority is inconsistent with the documented model.** Agents are
   told to read `.greatminds/schema.yaml`, while `cli/task.py::schema()` reads
   `find_canon_dir()/schema.yaml` and caches tables at import. The daemon also
   reads packaged canon for role dispatch. A project copy can consequently
   describe different rules from those enforced. Resolve one explicit schema
   version/snapshot for both execution and agent context.

6. **Process completion is not task completion.** Generic headless workers
   capture output only after exit. `_classify_turn_outcome` uses a Claude JSON
   shape plus whole-output keyword scanning; a successful answer discussing
   “rate limit” can be classified as a failure, while rc=0 with no domain
   progress can be accepted as `ok`. Add adapter-specific event parsers and
   separate transport success, agent result, and verified task progress.

7. **Crash recovery needs stronger ownership semantics.** Startup removes all
   driven locks/pending markers, assuming workers died with the daemon.
   Run-lock acquisition checks existence and then writes a file. Audit concurrent
   dispatch and surviving subprocesses, especially foreground daemon mode.
   Use atomic claims, run identity, process-group ownership, and stale-result
   rejection. These are code-review risks, not reproduced race failures here.

8. **Some shipped documentation describes missing content.** The plugin tree
   contains seven manifests and a README, with no `skills/**/SKILL.md` files,
   although the README and setup helpers describe bundled role/protocol skills.
   Decide which contracts remain after deterministic responsibilities move out
   of prompts, then generate and verify the actual artifacts.

## Proposed execution contract

Keep the role, harness, model/provider, scheduling, session policy, permissions,
and resource limits as distinct concepts. Existing `windows[].tool` values
should migrate into role bindings without changing task ownership semantics.

Use ACP as the primary common agent interface, including Claude and Codex
through their existing upstream adapters. Greatminds should own one ACP client,
not a parallel hand-written control protocol for each harness. Launch manifests
and capability/configuration handling retain the necessary per-agent differences.
Native transports in the current implementation are migration compatibility
paths, not the desired permanent architecture.

The runtime should accept a work item containing project/worktree, task and
revision, role contract version, context, expected result kind, and execution
policy. An adapter supplies preflight/capabilities, session creation/resumption,
turn start, normalized events, cancellation, and terminal outcome.

The daemon persists run state before dispatch, applies retries and concurrency
limits, handles timer and dependency events, and commits eligible domain
changes through the same service used by the CLI. Late results from superseded
runs must not change task state. Re-delivery must be idempotent; do not promise
exactly-once external command execution across crashes.

Interactive terminals remain useful operator interfaces. Correct scheduling
and recovery should also work without a live tmux pane or an LLM sleeping in it.
MCP can expose domain operations; it does not replace the agent lifecycle
transport.

## Current upstream integration directions

Official documentation checked on 2026-09-06; these are integration candidates,
not live-tested adapters or a promise of identical features across harnesses.

- [codex-acp](https://github.com/agentclientprotocol/codex-acp) exposes Codex
  through ACP and translates to/from the underlying
  [Codex app-server](https://learn.chatgpt.com/docs/app-server). Prefer this
  existing bridge to maintaining the translation in Greatminds. Pin the adapter
  and its compatible Codex dependency together.
- [claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp)
  exposes the official Claude Agent SDK through ACP, including permission
  requests, tool activity, MCP, and terminal interactions. These are upstream
  ACP integration projects, not evidence of a built-in `--acp` flag in the
  ordinary Claude/Codex CLI binaries.
- A shared ACP client can serve [Qwen Code](https://qwenlm.github.io/qwen-code-docs/en/developers/architecture/),
  [Kimi CLI](https://moonshotai.github.io/kimi-cli/en/reference/kimi-acp.html),
  and [Grok Build](https://docs.x.ai/build/cli/headless-scripting).
  Their documented entrypoints include `qwen --acp`, `kimi acp`, and
  `grok agent stdio`. Each still needs its own capability/auth/config tests.
- [Cline documents JSONL headless execution](https://docs.cline.bot/usage/cli-overview)
  and [ACP integration](https://docs.cline.bot/getting-started/installing-cline).
  Its current one-shot adapter should parse events and expose binding options.
- [OpenHands provides a Python/REST Software Agent SDK](https://docs.openhands.dev/sdk).
  Evaluate that alongside the existing CLI adapter for persistent sessions and
  remote workspaces, rather than assuming headless CLI is the only interface.
- [ACP's lifecycle](https://agentclientprotocol.com/protocol/v1/overview)
  provides initialization, authentication, new sessions, prompts, streamed
  updates, permissions, and cancellation. Session loading is optional and must
  be negotiated. A shared client must also implement the filesystem and
  terminal callbacks it advertises. Uniform transport does not imply identical
  session recovery, model options, usage telemetry, or extension support.
- Evaluate ACP paths for OpenHands, Gemini, and Cursor before adding another
  native transport. Keep exceptions explicit and justify them with a concrete
  capability gap. No live ACP compatibility tests were performed in this audit.

## Verification performed

- `.venv/bin/python -m pytest -q`: **1,640 passed, 2 skipped, 7 failed**, 106.80 s.
- Failures are in `test_appserver_unit_0320.py` (3) and
  `test_daemon_install_enable_0307.py` (4). Their stubs do not isolate newer
  install side effects. A focused reproduction traced failure through
  `daemon.install_cmd -> install_project_dropin -> Path.write_text` to the real
  user's systemd configuration, which is read-only in this sandbox. Later
  assertions expecting `enable` calls also fail because install exited early.
- `npm test --prefix vscode-extension`: passed the syntax checks and Node test
  harness. This does not validate a real VS Code extension host.
- No live harness inference, full mixed-fleet scenario, wheel build, remote
  host validation, GitHub issue-status verification, or release was performed.
- Full Python output for this session: `/tmp/greatminds-audit-pytest.log`.

## Implementation sequence and acceptance criteria

1. **Establish an isolated baseline.** Repair daemon-test path/env isolation;
   reconcile schema authority and record the initial compatibility matrix.
   Tests must run without writing real user configuration or requiring logins.
2. **Extract deterministic runtime ownership.** Move run lifecycle, scheduling,
   cancellation, retries, and recovery out of harness-specific branches. Verify
   duplicate events, daemon restart, surviving children, timeout, and stale
   results with fake adapters and subprocess fixtures, without LLM calls.
3. **Migrate mechanical role duties.** Begin with maintainer recovery and
   dependency unblocking; then compiled tick context and structured task
   completion. Preserve semantic review gates and evidence provenance.
   Idle fleets and routine recovery must require zero LLM turns.
4. **Unify adapters and bindings.** Implement one ACP client and prove it with
   Claude and Codex's upstream ACP adapters plus one native ACP agent. Add Qwen,
   Kimi, Grok and normalize Cline, OpenHands, Gemini, and Cursor through that
   interface wherever supported. Keep current drivers during migration only.
   Unsupported capabilities should fail preflight with a concrete reason.
5. **Prove role interchangeability.** Parameterize role-binding contract tests
   over every adapter, then run selected real mixed-fleet pipelines with pinned
   versions. Verify restart, cancellation, auth expiry, rate limits, worktree
   correctness, domain handoff, and measured no-progress behavior. Publish
   measured compatibility rather than treating argv-construction tests as E2E.

The first implementation slice should be baseline isolation plus deterministic
run supervision. Adding names to the tool registry alone would preserve the
current dependence on agent-managed orchestration.
