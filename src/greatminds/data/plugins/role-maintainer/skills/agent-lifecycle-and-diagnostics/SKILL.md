---
name: agent-lifecycle-and-diagnostics
description: Use when an agent is dead / hung / needs restart, when diagnosing coordd dead-pid reports, when switching an agent's tool (claude↔codex), or when verifying a restart succeeded. Covers bin/start_agent invocation, the .agent_registry layout, tmux send-keys patterns for driving role windows, rate-limit vs crash diagnosis, and post-restart verification. Trigger on "agent dead", "restart agent", "agent_registry", "tool switch", "pty-launch", "rate limit", "coordd dead-pid", "stuck agent".
---

# Agent lifecycle and diagnostics

MAINTAINER operates the agent fleet: starting, stopping, restarting,
diagnosing. This is exclusively MAINTAINER's responsibility — no
other role touches `bin/start_agent` or `.agent_registry/` files.

## Standard restart

```bash
# Identify which tmux window holds the dead/stuck role
tmux list-windows -t agents -F '#W' | grep -i <role>

# Drive the window: Ctrl-C any stuck process, then re-launch
tmux send-keys -t agents:<window> C-c
tmux send-keys -t agents:<window> "bin/start_agent <ROLE> <tool> --mode <loop|chat>" Enter
```

`bin/start_agent` is idempotent: if the role's session-id exists
in `.agent_registry/`, it resumes; otherwise it generates a new
session. Per-tool resume mechanics:
- claude: `--resume <session-id>` from `.agent_registry/<role>.session-id`
- codex: `codex resume <session-id>` from `.agent_registry/<role>.codex-session-id`
- cursor: `--continue` (uses latest cwd session; no UUID file)

## .agent_registry layout

For each role:
- `<role>.json` — live pid + tool + tty + session_id snapshot
- `<role>.session-id` — claude session UUID (persistent across restarts)
- `<role>.codex-session-id` — codex session UUID (if ever ran codex)
- `<role>.sock` — pty-launch unix socket (coordd writes wakes here)

## Intentional tool switch

To switch a role from claude → codex (or vice versa), do NOT just
restart with the other tool — the session files for the old tool
would still exist and confuse anyone. Clean up:

```bash
# Stop current agent
tmux send-keys -t agents:<window> C-c
sleep 1
pkill -f "pty-launch <ROLE>"   # nuke pty-launch process if it lingers

# Clear stale registry for old tool (keeps the other tool's session-id
# intact so you could switch back later)
rm -f coordination/.agent_registry/<role>.{json,sock}
# Drop old-tool session-id ONLY if switching is permanent:
# rm -f coordination/.agent_registry/<role>.session-id     # claude
# rm -f coordination/.agent_registry/<role>.codex-session-id  # codex

# Update coord.yaml: tool: <new>
$EDITOR coord.yaml

# Restart on new tool — fresh session
tmux send-keys -t agents:<window> "bin/start_agent <ROLE> <newtool> --mode loop" Enter
```

## Reading coordd dead-pid reports

When coordd detects an agent's pid is dead, it files a `dead-report`
ask to inbox/maintainer/ with role + last-known pid + tool +
started_at. Diagnostic flow:

1. **Crash vs rate-limit vs intentional**:
   - Crash: look at the tmux pane (`tmux capture-pane -t agents:<window> -p`)
     for error traceback. claude crashes leave a "Process terminated"
     line; codex usually exits with a stderr message.
   - Rate-limit: pane shows "rate limit" / "429" / "quota" / "too many
     requests" near the bottom. Agent self-recovers via backoff
     usually — wait one cycle (~120s) before intervening.
   - Intentional: human killed it (you, or USER). No traceback, clean
     exit.

2. **Restart logic**:
   - Crash → restart with same tool, same session id (auto-resumed
     via `bin/start_agent`).
   - Rate-limit + self-recovered → just ack the dead-report, no
     restart needed.
   - Intentional → if expected to stay down, `rm
     coordination/.agent_registry/<role>.json` so coordd stops
     reporting it dead each scan.

3. **Verification after restart**:
   ```bash
   pgrep -af "pty-launch <ROLE>"          # process exists
   ls -la coordination/.agent_registry/<role>.{json,sock}  # registry refreshed
   tmux capture-pane -t agents:<window> -p | tail -10      # tool prompt visible
   ```

## Stuck without rate-limit signal

If an agent is "alive" (pgrep finds it) but heartbeat is stale and no
work moves:

```bash
# What does its pane currently show?
tmux capture-pane -t agents:<window> -p | tail -20

# Last journal entry from this role?
grep "\"actor\":\"<role>\"" coordination/journal.ndjson | tail -3

# Inbox messages for it (unconsumed)?
ls coordination/inbox/<role-lowercase>/wake-* 2>/dev/null | head
```

If pane shows "Cooked for Ns" or similar long stuck status without
output movement, the agent may be in a deep stall. Restart via
the standard procedure.

## When NOT to intervene

- During rate-limit backoff: agent self-recovers.
- During legitimate idle (queue empty, watchdog reports
  `agent pids: all alive` and `0 stale tasks`): just resting.
- Right after a fresh fleet bringup: heartbeats may be stale because
  agents haven't done their first tick yet — wait ~120s.

**Tokens used:** none directly; tmux/pgrep/etc. are POSIX baseline.
