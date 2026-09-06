"""Deterministic task workspace preparation before ACP initialization."""

from dataclasses import asdict
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

import yaml

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import task_lock
from .store import TaskRevision


async def prepare_workspace_async(*args):
    # Git must not stall ACP callbacks for other runs. Drain preparation before
    # cancellation releases ownership: a worker may still be changing Git state.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="greatminds-workspace") as pool:
        future = asyncio.get_running_loop().run_in_executor(pool, prepare_workspace, *args)
        cancelled = False
        while not future.done():
            try:
                await asyncio.wait({future}, timeout=0.1)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            # Retrieve a possible exception before honoring cancellation.
            future.exception()
            raise asyncio.CancelledError
        return future.result()


def prepare_workspace(store, run: dict, binding, schema, owner_id: str) -> dict:
    from greatminds.cli.worktree import load_worktree_policy, worktree_create

    project = store.runtime.parent
    if run.get('conversation_id'):
        store._check_revision(TaskRevision(run['task_id'], run['task_path'], run['task_revision']))
        workspace = binding.workspace_path(project)
        store.workspace_plan(run['id'], owner_id=owner_id,
                             plan={'kind': 'conversation', 'path': str(workspace)})
        return store.workspace_ready(run['id'], owner_id=owner_id, path=str(workspace), identity={})
    with task_lock(store.runtime, run["task_id"]):
        revision = TaskRevision(run["task_id"], run["task_path"], run["task_revision"])
        store._check_revision(revision)
        data = yaml.safe_load((store.runtime / revision.path).read_text())
        if not isinstance(data, dict):
            raise GreatMindsError("task is not a mapping", exit_code=2)
        policy = load_worktree_policy(project, schema_document=schema.document)
        required = data.get("kind") in policy.required_for_task_kinds
        requested = binding.workspace_path(project)
        if required:
            expected = policy.worktree_path_for(project, run["task_id"]).resolve()
            if binding.workspace != "." and not requested.is_relative_to(expected):
                raise GreatMindsError("binding workspace conflicts with required task worktree", exit_code=2)
            plan = {"kind": "task_worktree", "path": str(expected), "policy": asdict(policy)}
        else:
            plan = {"kind": "binding", "path": str(requested)}
        store.workspace_plan(run["id"], owner_id=owner_id, plan=plan)
        if required:
            if not (project / ".git").exists():
                raise GreatMindsError("required task worktree needs a Git project", exit_code=2)
            workspace = worktree_create(project, run["task_id"], policy=policy).resolve()
            def git(*args, cwd=workspace):
                try:
                    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=15)
                except subprocess.TimeoutExpired as exc:
                    raise GreatMindsError("task worktree identity check timed out", exit_code=2) from exc
                if result.returncode:
                    raise GreatMindsError("cannot verify task worktree identity", exit_code=2)
                return result.stdout.strip()
            if workspace != expected or git("symbolic-ref", "--short", "HEAD") != policy.branch_for(run["task_id"]):
                raise GreatMindsError("existing task workspace has a different branch identity", exit_code=2)
            common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
            project_common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=project)).resolve()
            if common != project_common:
                raise GreatMindsError("task workspace belongs to a different repository", exit_code=2)
            identity = {"head": git("rev-parse", "HEAD"), "branch": policy.branch_for(run["task_id"]),
                        "git_common_dir": str(common)}
            if binding.workspace != ".":
                workspace = requested
        else:
            workspace, identity = requested, {}
        return store.workspace_ready(run["id"], owner_id=owner_id, path=str(workspace), identity=identity)
