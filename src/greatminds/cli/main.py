"""greatminds — unified CLI root.

Single entry-point ``greatminds`` (wired in pyproject.toml as
``greatminds = "greatminds.cli.main:cli"``) registers every subcommand
into one click group. Replaces the 19 separate ``greatminds-*``
entry-points used in 0.1.x.

Usage::

    greatminds setup --venv /path        # bootstrap a project
    greatminds launch --target tmux      # start the fleet
    greatminds task list verified        # list tasks in a queue
    greatminds inbox send DEVELOPER --kind wake --body "..."
    greatminds stand request --request-type deploy --profile full
    greatminds coordd --verbose          # run the keystroke daemon
    greatminds wake-check
    greatminds watchdog

Three sub-groups carry multi-action commands (``task``, ``inbox``,
``stand``); the rest are flat single commands attached directly to the
root group.
"""

from __future__ import annotations

import click

from .. import __version__


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, prog_name="greatminds")
def cli() -> None:
    """File-based multi-agent coordination protocol — fleet orchestration,
    task pipeline, agent launcher.
    """


# Sub-group registration. Each module exposes a top-level ``click.Group``
# or ``click.Command`` named after its public API; we attach them here so
# the root group is the single source of truth for "what subcommands exist".

from . import task as _task_mod
from . import inbox as _inbox_mod
from . import stand as _stand_mod
from . import setup as _setup_mod
from . import launch as _launch_mod
from . import coordd as _coordd_mod
from . import wake_check as _wake_check_mod
from . import watchdog as _watchdog_mod
from . import render_role as _render_role_mod
from . import lint_tokens as _lint_tokens_mod
from . import migrate_task as _migrate_task_mod
from . import migrate_stand_history as _migrate_stand_history_mod
from . import intent_clean as _intent_clean_mod
from . import notify_from_journal as _notify_journal_mod
from . import journal as _journal_mod
from . import stop_decide as _stop_decide_mod
from . import pty_launch as _pty_launch_mod
from . import start_agent as _start_agent_mod
from . import plan as _plan_mod
from . import git_check as _git_check_mod
from . import gate_check as _gate_check_mod
from . import restart as _restart_mod
from . import report_upstream as _report_upstream_mod
from . import daemon as _daemon_mod
from . import update as _update_mod
from . import worktree as _worktree_mod
from . import role_contract as _role_contract_mod
from . import agent as _agent_mod

cli.add_command(_task_mod.task)
cli.add_command(_inbox_mod.inbox)
cli.add_command(_stand_mod.stand)
cli.add_command(_setup_mod.setup)
cli.add_command(_launch_mod.launch)
cli.add_command(_coordd_mod.coordd)
cli.add_command(_wake_check_mod.wake_check, name="wake-check")
cli.add_command(_watchdog_mod.watchdog)
cli.add_command(_render_role_mod.render_role, name="render-role")
cli.add_command(_lint_tokens_mod.lint_tokens, name="lint-tokens")
cli.add_command(_migrate_task_mod.migrate_task, name="migrate-task")
cli.add_command(_migrate_stand_history_mod.migrate_stand_history,
                name="migrate-stand-history")
cli.add_command(_intent_clean_mod.intent_clean, name="intent-clean")
cli.add_command(_notify_journal_mod.notify_journal, name="notify-journal")
cli.add_command(_journal_mod.journal, name="journal")
cli.add_command(_stop_decide_mod.stop_decide, name="stop-decide")
cli.add_command(_pty_launch_mod.pty_launch, name="pty-launch")
cli.add_command(_start_agent_mod.start_agent, name="start-agent")
cli.add_command(_plan_mod.plan)
cli.add_command(_git_check_mod.check_git_permission, name="check-git-permission")
cli.add_command(_gate_check_mod.gate_check, name="gate-check")
cli.add_command(_restart_mod.restart)
cli.add_command(_report_upstream_mod.report_upstream, name="report-upstream")
cli.add_command(_daemon_mod.daemon)
cli.add_command(_update_mod.update)
cli.add_command(_worktree_mod.worktree)
cli.add_command(_role_contract_mod.role)
cli.add_command(_agent_mod.agent)


if __name__ == "__main__":
    cli()
