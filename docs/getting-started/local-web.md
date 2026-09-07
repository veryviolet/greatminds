# Local web workspace

Run the browser workspace for an initialized project:

```bash
greatminds web --project-dir /path/to/project --port 8765
```

Open `http://127.0.0.1:8765`. The UI ships inside the Python package: no Node.js,
frontend build, CDN, or separate web service is required. One server manages one
project. Use another port for another project; `--port 0` selects a free port and
prints its URL.

By default the command attaches to the project's running daemon, or starts one
if an execution configuration exists. `--no-daemon` opens the UI without starting
execution; its toolbar can start the daemon later. Closing a browser tab or
stopping the web server never stops the daemon. Any web instance or CLI can
inspect and explicitly stop the same project daemon. No systemd installation is
required. See [daemon lifecycle](../operations/daemon-lifecycle.md).

There is **no application authentication**: no login, cookies, or access tokens.
The default bind address is `127.0.0.1`. Set `--host 0.0.0.0` to listen on other
interfaces, and supply any desired access controls in your own reverse proxy.
Proxy the application at `/`, preserve the external `Host`, and forward requests
to its local port. Browser writes require the same origin; this is not a login.

## Workspace controls

- **Overview:** active runs, task queues, configured roles, recent activity, and
  pending tool permissions. Permission decisions use the daemon's existing broker.
- **Tasks:** searchable queue cards and the underlying task YAML.
- **Interactive tabs:** every binding with `scheduling: on-demand` appears as a
  role tab. Create or select conversations, send messages, interrupt a turn, or
  close a conversation. History survives page reloads. Retried delivery uses the
  same request ID, so an uncertain HTTP response does not duplicate a turn.
- **Runs:** select batch or interactive executions; inspect public agent messages,
  tool activity, command output previews, results, and protocol metadata. Cancel
  active runs or request a retry through the same controls as the CLI. A retry
  remains subject to daemon admission and workflow rules.
- **Toolbar:** start/stop the project daemon, pause/resume new runs, and edit settings.
  Pausing dispatch does not interrupt existing runs or their open conversations.
  Use the turn/run interruption controls to stop work already in progress.

Settings edit `coordination/execution.yaml`. The form exposes role bindings,
harnesses, model/mode, permission policy, timeouts, and concurrency. The full YAML
editor covers commands, workspaces, accounts, and other execution options. New
agents and roles are staged until **Save**. Configuration validation runs before
writing; a stale browser cannot overwrite a newer saved version. Restart the
daemon to apply changes. Harness binaries and their credentials are configured
on the host as before; adding a manifest does not install or authenticate them.

## Chat input and appearance

Press **Enter** to send a message and **Shift+Enter** to insert a newline.
Ctrl/Cmd+Enter also sends. IME composition and held-key repeats do not submit.
The composer and existing history nodes stay in place during background updates,
preserving draft selection and manual scroll position.

Active conversations fetch updates more frequently than the dashboard and append
incoming text progressively. The activity indicator remains visible during an
agent turn, including pauses before text arrives; queued turns and permission or
login waits have distinct labels. The indicator stops when the turn ends. Reduced
motion preferences disable the text reveal and spinner animation.

Use the toolbar's light/dark theme switch. The first visit follows the system
color preference; an explicit choice is saved in this browser for the same origin.
The theme applies to the chat, dashboards, settings, forms and code previews.

## History and storage

The browser reads the same filesystem state as the CLI and daemon. Viewing a page
or reconnecting does not launch an agent. Dashboard requests are polled every 1.5 seconds; active chat requests every 350 ms;
conversation event cursors avoid replaying earlier text. The UI renders the last
50 conversation turns; the existing conversation journal remains on disk.

New batch runs retain public ACP message and tool updates in
`.greatminds/.runtime/activity/`. These records are supplementary observations,
not workflow authority or proof that a task passed. Each run keeps at most 512
records and 256 KiB; individual records are bounded to 16 KiB before framing.
The UI labels truncated history. Known credentials and sensitive structured
fields are redacted; private reasoning and arbitrary ACP metadata are excluded.
Older runs without this journal still expose their existing state and evidence.

Command previews verify the existing daemon-owned output artifacts and display
up to 8 KiB per stream. Missing or changed artifacts produce an explicit error.
Reading output does not rerun the command.
