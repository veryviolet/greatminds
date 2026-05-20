---
name: ui-visual-verification
description: Use when verifying UI bugfix/feature tasks — verifying changes are actually rendered in a real browser against the deployed stand, NOT inferring from bundle string contents. Covers rendered-DOM probe patterns (Playwright headless), a11y tree inspection, why bundle-string grep is forbidden as evidence, and screenshot evidence when warranted. Trigger on "UI verification", "visual verify", "Playwright probe", "rendered DOM", "bundle grep" (anti-pattern), "accessibility tree probe".
---

# UI visual verification

When TESTER verifies a UI change, the question is "does the user
actually see/use the new behaviour in a real browser?" — NOT "does
the source code contain a string that looks like the change".
Verifying via bundle grep is a precedent-violation (incident 0185) and
will be rejected by AR.

## The bundle-grep trap (don't)

```bash
# WRONG — bundle string grep is NOT evidence of UI behaviour
curl ${STAND_URL_A}/index.js | grep 'New Feature Header' && echo "feature ships"
```

This proves the string exists in the build artifact. It does NOT prove:
- The string is rendered on the right page
- The DOM tree contains the element at the right place
- Users can actually see / interact with it
- Hidden CSS doesn't suppress it
- Conditional rendering doesn't skip it

0185 was falsely-accepted on a bundle-grep TESTER block. AR caught it
in review; the UI was actually broken. Don't repeat.

## Rendered-DOM probe with Playwright

The right pattern: launch a headless browser, navigate to the
affected URL, query the rendered DOM:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context()
    page = context.new_page()

    page.goto(f"{STAND_URL_A}/items")

    # Assert by accessible role + name — same way a user finds things
    heading = page.get_by_role('heading', name='Items')
    heading.wait_for(state='visible', timeout=5000)

    # The actual claim from the bug-fix: column header should say "Price USD"
    col = page.get_by_role('columnheader', name='Price USD')
    col.wait_for(state='visible', timeout=5000)

    # Optional: take a screenshot for the tests-block evidence
    page.screenshot(path='/tmp/items-header.png', full_page=False)

    browser.close()
```

Key points:
- `get_by_role` + accessible name — same way a screen-reader user
  finds things. If it's not findable this way, that's an a11y bug
  alongside the UI bug.
- `wait_for(state='visible')` — handles async rendering. Don't sleep,
  wait for the actual condition.
- Capture screenshots when the bug was visual (positioning, theme,
  layout). Reference the screenshot path in the tests-block body.

## A11y tree probe

For "is this element actually accessible?" probing:

```python
# Snapshot the accessibility tree at the focused area
snap = page.accessibility.snapshot()
# Find the heading
def find(node, role, name):
    if node.get('role') == role and node.get('name') == name:
        return node
    for child in node.get('children', []) or []:
        if (r := find(child, role, name)):
            return r
    return None
assert find(snap, 'heading', 'Items') is not None
```

The a11y snapshot is what assistive tech sees. If your element is
visible to sighted users but doesn't appear in the snapshot, it has
no accessible name or wrong role — fix in the component, then re-probe.

## For UI flows (multi-step user interactions)

```python
page.goto(f"{STAND_URL_A}/items")
page.get_by_role('button', name='New item').click()

# Modal appears — wait for its title
page.get_by_role('dialog').wait_for(state='visible')

page.get_by_label('Name').fill('widget')
page.get_by_label('Price').fill('9.99')
page.get_by_role('button', name='Create').click()

# Modal closes, item appears in list
page.get_by_role('dialog').wait_for(state='hidden', timeout=3000)
new_row = page.get_by_role('row').filter(has_text='widget')
new_row.wait_for(state='visible', timeout=3000)
```

For lifecycle/CRUD probes with mutations, remember:
**TESTER does read-only probes; mutating flows are SK/EXPLORER per
plan's `live-mutating verify owner:` line.** If a UI flow probe
mutates state (creates, deletes), and the task is stand_required, you
need to either:
- Find a read-only assertion that's still meaningful (just verify
  the form exists / opens correctly without submitting), OR
- Cite EXPLORER/SK's mutating-flow evidence in the tests block.

## Headless vs headed during dev

When writing probes, run with `headless=False` and `slow_mo=500` so
you can SEE what's happening:

```python
browser = p.chromium.launch(headless=False, slow_mo=500)
```

For the actual verification run cited in the tests-block, headless is
fine.

## Evidence to put in the tests block

For a UI verification, the `tests` block body should include:
- Which Playwright probes ran (function names or descriptions)
- What URL was probed
- What was asserted (in plain English: "page contained the
  'Price USD' column header at index 4")
- Screenshot path if captured

```yaml
- kind: tests
  outcome: approved
  body: |
    UI probes against ${STAND_URL_A}/items:

    1. test_column_header_renamed — PASS
       Navigated to /items, asserted column heading by role+name
       'Price USD' is visible. Screenshot: /tmp/0193-header.png.

    2. test_new_item_modal_opens — PASS
       Clicked 'New item' button, modal with role=dialog appears,
       name='Create item' is the accessible name.

    3. test_a11y_tree_includes_new_columnheader — PASS
       Accessibility snapshot at /items contains
       columnheader[name='Price USD'].

    Did NOT exercise mutating create flow: plan declared
    live-mutating verify owner: EXPLORER; cited
    review_sessions/0042's evidence for the create→edit→delete cycle.
```

## Don't

- Don't grep the bundle. Ever, as evidence.
- Don't use brittle CSS-selector finds (`page.locator('.btn-primary-3')`)
  when role-based queries work — if you must, justify why in a comment.
- Don't disable timeouts to "make tests pass" — investigate the
  flakiness; usually it's an async-rendering issue or a real bug.
- Don't mutate state in TESTER probes when the plan says
  live-mutating belongs elsewhere.

**Tokens used:** STAND_URL_A (PROJECT.env).
