# Project-specific variables

Every file in this template uses **placeholder tokens** for the project-specific bits. Before adopting the protocol in a real project, replace each `<TOKEN>` with the project's value either by global search-replace or by leaving the tokens in place and providing a per-project cheatsheet that humans/agents read alongside the protocol.

## Filesystem

| Token                       | Meaning                                                                  | Example                                  |
|-----------------------------|--------------------------------------------------------------------------|------------------------------------------|
| `<PROJECT_ROOT>`            | Absolute path to the project's working tree.                             | `/opt/myorg/myproject`                   |
| `<COORDINATION_DIR>`        | Where `coordination/` lives, usually `<PROJECT_ROOT>/coordination`.      | `/opt/myorg/myproject/coordination`      |

## Stand / runtime

| Token                       | Meaning                                                                  | Example                                  |
|-----------------------------|--------------------------------------------------------------------------|------------------------------------------|
| `<STAND_DESCRIPTION>`       | One-line description of what "the stand" is for this project.            | `Docker Compose two-node back-end plus Vite UI dev servers` |
| `<STAND_BRINGUP_RUNBOOK>`   | Path or filename of the runbook that defines how to bring the stand up. | `RUN_DEV_STAND.md`                       |
| `<STAND_PROFILES>`          | List of Docker Compose profiles or equivalent service groups.            | `core, worker, ingest`                   |
| `<STAND_REBUILD_CMD>`       | Command pattern for rebuilding a profile.                                | `docker compose --profile <name> build && docker compose --profile <name> up -d --wait` |
| `<STAND_REMOTE_HOSTS>`      | Optional remote hosts managed by STAND-KEEPER.                           | `host-a, host-b`                         |
| `<STAND_REMOTE_SYNC>`       | How to sync code/artifacts to remote hosts.                               | `rsync only; no remote git`              |
| `<STAND_GPU_CHECK_CMD>`     | Command STAND-KEEPER uses to verify GPU/CUDA.                             | `nvidia-smi && uv run python -c ...`     |

## URLs and ports

| Token                       | Meaning                                                                  | Example                                  |
|-----------------------------|--------------------------------------------------------------------------|------------------------------------------|
| `<BACKEND_URLS>`            | URLs for the project's back-end APIs (one per role / cluster).           | `http://127.0.0.1:8090, http://127.0.0.1:8091` |
| `<UI_DEV_URLS>`             | URLs for the project's UI dev servers (one per role).                    | `http://a.localhost:4173, http://b.localhost:4174` |
| `<UI_LOGIN_FLOW>`           | If UI verification needs to log in: how (selectors + credentials).       | `#auth-username + #auth-password, button:has-text("Sign in"), creds=admin/11111111` |

## Test runners

| Token                       | Meaning                                                                  | Example                                  |
|-----------------------------|--------------------------------------------------------------------------|------------------------------------------|
| `<TEST_RUNNER_BACKEND>`     | Command to run back-end tests.                                           | `uv run -m pytest`                       |
| `<TEST_RUNNER_UI>`          | Command to run UI tests.                                                 | `npm --prefix ui run test:run`           |
| `<TEST_PATHS_BACKEND>`      | Where back-end tests live.                                               | `tests/unit/, tests/integration/`        |
| `<TEST_PATHS_UI>`           | Where UI tests live.                                                     | `ui/src/lib/__tests__/`                  |

## Browser DOM verification (optional)

If the protocol uses headless browser automation for UI verification:

| Token                       | Meaning                                                                  | Example                                  |
|-----------------------------|--------------------------------------------------------------------------|------------------------------------------|
| `<HEADLESS_BROWSER_RUNNER>` | Path to a venv or runner that has Playwright (or equivalent) available.  | `~/.cache/review-venv/bin/python`         |

## Git / commit conventions

| Token                       | Meaning                                                                  | Example                                  |
|-----------------------------|--------------------------------------------------------------------------|------------------------------------------|
| `<DEFAULT_BRANCH>`          | The active branch the protocol commits onto.                             | `main`                                   |
| `<COMMIT_PREFIX_PRODUCT>`   | Commit message prefix for approved product work.                         | `feature(<seq>): <slug>` / `fix(<seq>): <slug>` |
| `<COMMIT_PREFIX_FEATURE>`   | Backward-compatible alias if a project still uses this token.            | `feature(<seq>): <slug>`                 |

## Docs and bot coordination

| Token                       | Meaning                                                                  | Example                                  |
|-----------------------------|--------------------------------------------------------------------------|------------------------------------------|
| `<DOCS_STACK>`              | Documentation stack.                                                     | `mkdocs-material`                        |
| `<DOCS_BUILD_CMD>`          | Docs build/link-check command.                                           | `uv run mkdocs build --strict`           |
| `<BOT_TRUTH_LIST>`          | Bot behavior truth files, if BOT_* roles are enabled.                    | `CLAUDE.md, memory/*.md, skills/*/SKILL.md` |
| `<BOT_LOCAL_RETEST_CMD>`    | Local/dev command BOT-USER uses to query target bot.                     | `claude -p ...`                          |
| `<BOT_COMMIT_POLICY>`       | Whether BOT-DEVELOPER may commit/push/deploy.                            | `no commit in product repo`              |

## Replacement strategy

Two reasonable approaches:

1. **Global replace at install time.** Run a sed/text-replace pass over all installed files, substituting each `<TOKEN>` with its project-specific value. Pros: docs read cleanly. Cons: have to re-run if a token's value changes.
2. **Keep tokens, provide a cheat-sheet.** Drop the files in unchanged and add `coordination/PROJECT.md` with all the values. Agents read the cheat-sheet first. Pros: easy to update; the protocol remains a clean library. Cons: every agent has one extra file to read.

Either is fine. The protocol has no preference.

## Canon MCP and overlay environment

These env-var tokens are referenced by `mcp/canon.json` and by skill Bash blocks
(`${TOKEN}` shell form) and may appear in `**Tokens used:**` paragraphs of skill
bodies. Real values come from `<project>/coordination/PROJECT.env` (gitignored,
secrets) which `greatminds start-agent` sources before launching each Claude agent.

| Token                  | Meaning                                                                  | Example                                  |
|------------------------|--------------------------------------------------------------------------|------------------------------------------|
| `<COORD_POSTGRES_DSN>` | Postgres DSN (with credentials) consumed by the postgres MCP server.     | `postgresql://user:pass@host:5432/db`    |
| `<STAND_HOST_A>`       | Hostname of the first stand host (alpha node), if the project has a stand. | `lattice-a.example.com`                |
| `<STAND_HOST_B>`       | Hostname of the second stand host (beta), for peer-pair setups.          | `lattice-b.example.com`                  |
| `<STAND_URL_A>`        | REST API base URL for the first stand host.                              | `https://lattice-a.example.com/api/v1`   |
| `<STAND_URL_B>`        | REST API base URL for the second stand host.                             | `https://lattice-b.example.com/api/v1`   |
