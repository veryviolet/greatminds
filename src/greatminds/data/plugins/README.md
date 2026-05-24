# Canon plugins

Layered plugin model for Claude Code agents in the Guardora coordination
system. Each plugin lives in its own directory with the standard layout:

    <plugin>/
      .claude-plugin/plugin.json      # plugin manifest
      skills/<skill-name>/SKILL.md    # Skills (auto-invoked by Claude)

## Layout

- `coordination-protocol/` — universal skills loaded by every role (FSM
  mechanics, plan/impl block protocols, stand workflow, blocked-deps,
  inbox/escalation).
- `role-<name>/` — per-role skills, loaded only when an agent runs as
  that `GREATMINDS_ROLE`.

Loading is driven by `greatminds start-agent`, which passes `--plugin-dir` per
session in the order: canon protocol → canon role → project overrides
(`<project>/coordination/plugins.local/project-overrides`). Last
`--plugin-dir` wins on skill name collision, so a project can shadow a
canon skill by providing a skill with the same `name:` frontmatter.

To opt OUT of a canon skill without overriding it, set
`skillOverrides: { "<skill-name>": "off" }` in `<project>/.claude/settings.local.json`.

See the greatminds documentation site at <https://veryviolet.github.io/greatminds/>
for the full bring-up procedure (or the repo README) and
`.claude-plugin/marketplace.json` for the plugin registry.
