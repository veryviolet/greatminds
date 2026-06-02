# Inbox

The inbox is a per-role mailbox under `coordination/inbox/<role>/`. It carries
messages that do not need a task move.

Message kinds:

- `wake`: notify a role that a queue transition or dependency changed.
- `ask`: request a decision or clarification.
- `info`: send context that does not require a task handoff.

Use the CLI:

```bash
greatminds inbox send DEVELOPER --kind ask --task TASK_ID --body "Question text"
greatminds inbox list
greatminds inbox show MESSAGE_FILE
greatminds inbox ack MESSAGE_FILE
```

Inbox messages complement the queue FSM. They do not replace task moves, plan
blocks, implementation blocks, tests blocks, or review blocks.
