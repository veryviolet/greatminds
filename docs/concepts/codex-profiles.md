# Codex Profiles

Codex roles use generated per-role profile sources instead of Claude plugin
directories. During `greatminds setup`, greatminds creates one profile source
directory for each shipped Codex role:

```text
<project>/
  .greatminds/
    .codex-home/
      developer/
        config.toml
      tester/
        config.toml
      technical-writer/
        config.toml
```

Each `config.toml` is copied from
`src/greatminds/data/codex/profiles/<role>.config.toml` and then split for
Codex profile-v2 loading:

- `.greatminds/.codex-home/<role>/config.toml` contains the role posture,
  trusted-project entry, and skill registrations.
- `.greatminds/.codex-home/<role>/<role>.config.toml` contains the role model
  and execution settings.

These directories are profile sources only. They are not authentication homes
and must not contain `auth.json`.

Codex-backed roles authenticate through the single machine Codex home. By
default that is `~/.codex`; operators may override it with
`GREATMINDS_CODEX_HOME`. Greatminds sets `CODEX_HOME` to that machine home so
Codex reads the one valid login and refreshes tokens in one place.

Role-specific behavior is preserved without using a per-role auth home:

- The role bootstrap/contract is passed as the prompt or app-server
  `baseInstructions`.
- The role model is read from the generated profile source and injected with a
  `-c model="..."` override.
- Driven Codex turns also inject unattended execution settings with `-c`
  overrides.

To inspect or customize a project's Codex posture, edit the generated
`.greatminds/.codex-home/<role>/` files after setup. Do not copy or symlink
machine auth into those directories. To change the defaults for future
projects, update the canon templates under
`src/greatminds/data/codex/profiles/` before running `greatminds setup` in
those projects.
