# greatminds

File-based multi-agent coordination protocol. Per-role queues, atomic `mv` handoff,
layered plugin set for [Claude Code](https://docs.claude.com/en/docs/claude-code) and
profile-v2 setup for [OpenAI Codex CLI](https://github.com/openai/codex).

## Status

**Alpha** — `0.1.0.dev0`. Public foundation commit; CLI entry-points are wired
incrementally as scripts are ported from the original `/opt/coordination/` layout.

## What it is

A fleet-orchestration substrate for agents running in `tmux` panes:

- **R8 finite-state pipeline** — tasks move through typed queues (`feature_inbox/`,
  `feature_plan/`, `feature_dev/`, `feature_ui_dev/`, `feature_docs/`, `feature_test/`,
  `feature_review/`, `verified/`, …) via atomic `mv` — no daemon, no broker, no DB.
- **Per-role identity** — each agent owns one role (e.g. `ARCHITECT-PLANNER`,
  `DEVELOPER`, `TESTER`, `STAND-KEEPER`); roles claim from their queue, hand off to
  the next.
- **Append-only task files** — every transition adds a block; full audit trail.
- **Layered plugins** for Claude Code — universal `coordination-protocol` plugin
  loads for every role, per-role plugins layer on top.
- **profile-v2 setup** for Codex — equivalent per-role config, allowing
  hot-swapping `claude ↔ codex` per role from one fleet config.
- **`coordd`** — keystroke pusher daemon that watches per-role wake-files and
  forwards them to the live tmux pane only when the agent is genuinely idle
  (heartbeat-freshness guard prevents interrupting active work).
- **Stand evidence + gate-check** — tasks marked `stand_required` cannot reach
  `verified/` until matching `stand_done/<id>.yaml` records the live-stand result.

## Design philosophy

- Files, not state machines in memory. If the orchestrator crashes, the FS still
  tells you exactly where every task is.
- Atomic `mv` is the only handoff primitive — same filesystem, same volume.
- Each task is human-readable YAML/Markdown — no opaque blobs.
- Roles are interchangeable between Claude Code and Codex via the same plugin /
  skill / data layer.

## Install

```bash
pip install greatminds   # not yet published — first release pending
```

## License

Apache-2.0. See [LICENSE](LICENSE).

## History

`greatminds` is the public, packaged form of the coordination protocol developed
in `gitverse.ru/veryviolet/coordination`. Pre-0.1.0 development history (92
commits, R8 FSM design, role splits, plugin layering, MCP integration) is
archived there.
