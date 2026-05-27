---
deploy_prerequisites_only: false
---
# full-deploy — manual deploy recipe (prose form)

This profile mirrors `full-deploy.yaml`. Pick this format when you
need to read the recipe before SK runs it, or when the target host
doesn't have ansible-core available locally.

Substitution variables (filled in by the executor):

- `${host}` — target stand host
- `${user}` — SSH user
- `${deploy_path}` — remote install root (default `/srv/greatminds`)
- `${worktree}` — local worktree being deployed
- `${lease_id}`, `${task_id}` — bookkeeping
- Any other `${KEY}` defined in `coordination/PROJECT.env`

## Steps

1. Ensure `${deploy_path}` exists on `${host}` (create it with mode
   0755 if missing). This is the prerequisite step — when the lease
   sets `deploy_prerequisites_only=true`, stop after this.

2. `rsync` `${worktree}/` to `${user}@${host}:${deploy_path}/`,
   excluding `.venv*`, `.worktrees`, and `__pycache__`.

3. On `${host}`: `cd ${deploy_path}` and verify `uv` is on PATH
   (`uv --version` exits 0). Abort if missing — install `uv` first.

4. Create the venv if not present: `uv venv .venv-coord`.

5. Build the wheel: `uv build --wheel` (output lands in
   `${deploy_path}/dist/`).

6. Install: `.venv-coord/bin/pip install --force-reinstall
   dist/greatminds-*.whl`.

7. Run `greatminds setup --project-dir ${deploy_path}` so the
   remote tree gets its coordination/ scaffolding.

8. Smoke: `.venv-coord/bin/greatminds --version`. If
   `${expected_version}` is defined in PROJECT.env, verify the
   output contains it.

Mark the lease `ready` once step 8 passes; otherwise `stand down
--reason "<failing step>"`.
