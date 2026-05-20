# docs implementation block snippet

Append this block to a `scope: docs` feature before moving
`feature_docs/X.md -> feature_docs_review/X.md`.

---
implementation:
  closed_by: technical-writer-agent
  closed_at: <ISO>
  base_commit: <git short sha before docs edits>
  files:
    - <docs/path.md>
  docs_system: mkdocs-material | sphinx | docusaurus | typst | other:<name>
  build_command: <command or "not available">
  build_result: pass | fail | not_run
  ready_for_reader: true
---

## Implementation (technical-writer)
- <path:line> concise summary.

## Docs verification notes
- <build/link/screenshot/publication notes>
