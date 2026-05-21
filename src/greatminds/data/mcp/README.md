# Canon MCP

`canon.json` — universal MCP servers loaded by every Claude agent via
`greatminds start-agent --mcp-config /opt/coordination/mcp/canon.json`.

Projects may add or override servers by providing
`<project>/coordination/mcp.local.json`; both files are passed to
`--mcp-config` with the project file last (last-wins on server-name
collision).

Servers referencing `${TOKEN_NAME}` resolve those vars from the
environment, which `greatminds start-agent` exports from
`<project>/coordination/PROJECT.env` before launching Claude.

Required env vars and their semantics: see the token-contract in
`<canon>/templates/PROJECT.md.template`.
