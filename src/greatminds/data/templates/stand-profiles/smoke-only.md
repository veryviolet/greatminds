---
deploy_prerequisites_only: false
---
# smoke-only — quick liveness probe (prose form)

Mirrors `smoke-only.yaml`. Use when you just want to confirm the
stand is reachable and the greatminds CLI responds — no install,
no rsync, no setup re-run.

Substitution variables: `${host}`, `${user}`, `${deploy_path}`,
plus any `${KEY}` from `coordination/PROJECT.env`.

## Steps

1. SSH to `${user}@${host}` succeeds and the remote shell is
   responsive (`ssh ${user}@${host} /bin/true` exits 0). This is
   the prerequisite step.

2. On the host, `${deploy_path}/.venv-coord/bin/greatminds
   --version` exits 0 with a recognizable version string.

Pass the lease if both succeed; otherwise `stand down --reason
"smoke step <N>"`.
