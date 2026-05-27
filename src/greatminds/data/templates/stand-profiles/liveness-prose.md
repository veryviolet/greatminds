---
deploy_prerequisites_only: false
---
# liveness-prose — md-only profile

No yaml twin. Exercises the md execution path: `execute_md_profile`
substitutes `${host}`, `${user}`, `${deploy_path}`, `${task_id}`,
`${lease_id}` plus any `${KEY}` from `coordination/PROJECT.env`,
returns the rendered prose to SK. SK as LLM reads the rendered text
and acts.

Substitution variables: `${host}`, `${user}`, `${deploy_path}`,
`${task_id}`, `${lease_id}`, plus any `${KEY}` from
`coordination/PROJECT.env`.

## Steps

1. SSH to `${user}@${host}` succeeds: `ssh ${user}@${host} /bin/true`
   exits 0.

2. On `${host}`, `greatminds --version` (system or
   `${deploy_path}/.venv-coord/bin/greatminds --version`) responds.

Pass the lease if both succeed; otherwise `stand down --reason
"liveness-prose step <N>"`.
