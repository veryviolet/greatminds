# bot issue template

---
id: <seq>-<short-slug>
stream: bot
reporter: bot-user-agent
opened_at: <ISO-8601 UTC>
severity: critical | high | medium | low
area: <prompt | memory | skill | tool | bridge | other:<name>>
bot_state_at_report:
  head_sha: <git short sha or unknown>
  deployed: yes | partial | no | unknown
  notes: <brief notes>
---

## Summary
One-sentence behavior issue.

## Reproduction
Exact prompt/channel and observed answer.

## Expected behavior
Expected behavior according to the bot truth files.

## Actual behavior
Observed behavior, with a short excerpt.

## Diagnosis (optional)
Hypothesis only. Do not prescribe the fix.

## Verification plan
Exact retest scenario and pass/fail criteria.

## Notes
Duplicates, related issues, context.
