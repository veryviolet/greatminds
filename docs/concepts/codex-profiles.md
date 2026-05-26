# Codex Profiles

Codex roles use generated per-role homes instead of Claude plugin
directories. During `greatminds setup`, greatminds creates one Codex home for
each shipped Codex profile:

```text
coordination/
  .codex-home/
    developer/
      config.toml
    tester/
      config.toml
    technical-writer/
      config.toml
```

Each `config.toml` is copied from
`src/greatminds/data/codex/profiles/<role>.config.toml`. The generated file
contains `developer_instructions = """..."""` for the role posture and a
`[profiles.<role>]` section with the Codex model, approval policy, and sandbox
settings.

When a Codex-backed role starts, `start_agent.py` points Codex at that role's
home:

```bash
CODEX_HOME=<project>/coordination/.codex-home/<role> \
  codex --profile <role-lower>
```

Codex then reads `$CODEX_HOME/config.toml` and selects the matching
`[profiles.<role>]` section. The older pattern of storing per-role profile
files under a user's home directory is not part of the greatminds launch path.

To inspect or customize a project's Codex posture, edit the generated
`coordination/.codex-home/<role>/config.toml` files after setup. To change the
defaults for future projects, update the canon templates under
`src/greatminds/data/codex/profiles/` before running `greatminds setup` in
those projects.
