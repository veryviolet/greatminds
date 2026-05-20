---
name: commit-and-push-protocol
description: Use when AR approves a task and needs to commit declared_files + push to both repos in the same tick. Covers exact-paths commit discipline, declared-files commit, dual-repo push when canon AND lattice are touched, push proof artifacts, the unpushed-verified-is-not-done rule. Trigger on "commit declared files", "push origin main", "AR push per tick", "dual repo push", "push proof", "commit-and-push".
---

# Commit-and-push protocol

When AR approves a task into verified/, AR must commit the declared
files AND push to all touched repos **in the same tick**. Unpushed
verified = not done. This is a hard rule, codified in PROJECT.md and
enforced by the protocol — agents reading verified history rely on
the commits being actually live on origin.

## Commit by EXACT declared_files paths

`git commit -m '...'` with no paths captures the entire staged area —
which on a multi-tenant working tree includes whatever ELSE happens
to be staged. WRONG.

Instead: stage and commit **exactly** the implementation's
declared_files:

```bash
# Get the list
declared=$(bin/task show <id> | yq -r '
  [.blocks[] | select(.kind=="implementation" or .kind=="iteration")] |
  reverse | .[0].declared_files[]
')

# Stage exactly those paths
echo "$declared" | xargs git add --

# Commit by paths (in case anything else snuck in)
git commit -m "$(cat <<EOF
<feat|fix|docs>(<id>): <one-line summary>

<body explaining what changed and why, referencing the parent task if any>

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)" -- $declared
```

The trailing `-- $declared` re-asserts paths to commit. If somehow a
file was already in the working tree from a co-tenant task,
declared_files filter excludes it; the co-tenant work survives intact
for THEIR commit.

Precedent: feedback_declared_files_discipline (0373/0374). Skipping
this rule = co-tenant work bleeds into AR's commit = next AR
attempting to commit gets "nothing to commit" and the chain breaks.

## Commit-message convention

```
<type>(<task-id>): <short summary, present tense>

<body — what changed, why, references>

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

- type: `feat` / `fix` / `docs` / `refactor` / `chore` per the
  project's commit prefix (PROJECT.md has the canonical prefix
  format)
- task-id: the verified task's id (0042-foo-bar). Lets `git log` be
  filtered by id.
- summary: imperative, lowercase, no trailing period, ≤72 chars
- body: as long as needed; explain the why

## Dual-repo push when canon AND lattice are touched

Some tasks touch both repos:
- A canon-side change (`schema.yaml`, a `bin/*` script, a SKILL.md)
- A lattice-side change (`app/`, `ui/`, project-specific config)

For these, AR commits in BOTH repos and pushes BOTH in the same tick:

```bash
# In /opt/coordination — canon commit + push
cd /opt/coordination
git add <canon-paths-from-declared_files> && git commit -m "..." -- <canon-paths>
canon_push=$(git push origin main 2>&1 | tail -1)
canon_head=$(git rev-parse --short HEAD)

# In <project> (lattice) — project commit + push
cd /opt/guardora/lattice
git add <lattice-paths-from-declared_files> && git commit -m "..." -- <lattice-paths>
lattice_push=$(git push origin main 2>&1 | tail -1)
lattice_head=$(git rev-parse --short HEAD)

# Record both in the review block
echo "canon: $canon_push (HEAD=$canon_head)"
echo "lattice: $lattice_push (HEAD=$lattice_head)"
```

Both pushes happen BEFORE writing `outcome: approved` to the review
block — that way, if one push fails, AR knows before declaring done.

## Push proof artifact

The exact line `git push origin main` prints on success is the proof
the push happened:

```
   a032302..dc51b23  main -> main
```

`<old-sha>..<new-sha>  main -> main`. Capture this into `push_proof:`
in the review block (see `review-block-craft`). The line includes
both shas — AR can verify push succeeded AND record both shas without
re-querying.

Failures look different:

```
 ! [rejected]        main -> main (non-fast-forward)
```

If you see anything other than the `..  main -> main` success line,
the push didn't succeed. Diagnose:

- `non-fast-forward`: someone else pushed since you fetched. `git pull
  --rebase` (or fetch + reset for clean tree), redo the commit on top.
- `permission denied`: auth issue. Check ssh-agent / gh auth.
- `connection refused`: network issue. Wait, retry.

If you can't resolve in the tick, the review block does NOT get
`outcome: approved`. The task stays in feature_review/ until next
tick.

## Unpushed verified = not done

Hard rule:
- Approved + committed + pushed: ship. verified/ contains the task.
- Approved + committed + NOT pushed: NOT DONE. Don't mv to verified.

The reason: agents on other machines (or after restart) read `git log`
to understand history. If verified/<id>.yaml exists but the commit
isn't on origin, they see a YAML referencing a SHA they can't
checkout. Broken.

Special case: if the change is canon-only and lattice has no commit
to make, AR records `lattice: not_touched` in push_refs — explicitly,
not by omission. The audit trail shows AR considered both repos.

## Edge case: amending after push

Sometimes after push AR notices a typo in the commit message. DON'T
amend a pushed commit (force-push rewrites history visible to others).
Either:
- Live with the typo (it's a typo, life goes on)
- Make a follow-up commit `chore: fix typo in <previous-sha>` if it's
  worth the noise

Force-pushing main is banned outside MAINTAINER + emergency context.

## Don't

- Don't commit with `git commit -am` (bypasses path-list). Always `--`
  with paths.
- Don't push without committing (`git push` with empty staged area
  silently no-ops; the review block claims push but nothing
  happened).
- Don't write `outcome: approved` before push completed successfully.
  Write outcome at the very end, after you've seen the success line.
- Don't push canon and skip lattice when both changed. Both in the
  same tick.

**Tokens used:** none.
