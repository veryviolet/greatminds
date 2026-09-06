"""Daemon-owned configured commands and revision-bound execution evidence.

A durable launch intent and gated process identity precede exec. Recovery never
repeats a command whose completion is uncertain. Output is a local, size-bounded
artifact; it is not injected into operator snapshots or interpreted as approval.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from acp.transports import default_environment

from greatminds.core.errors import GreatMindsError
from greatminds.core.storage import (_ensure_directory, _sync_directory, atomic_bytes,
                                    file_lock, safe_name, task_lock)
from .config import CommandDefinition, fingerprint
from .processes import process_identity, terminate_group
from .store import TaskRevision


UNRESOLVED = frozenset({"queued", "starting", "running", "needs_recovery"})


async def _blocking(function, *args):
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="greatminds-evidence") as pool:
        future = asyncio.get_running_loop().run_in_executor(pool, function, *args)
        cancelled = False
        while not future.done():
            try:
                await asyncio.wait({future}, timeout=0.1)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            future.exception()
            raise asyncio.CancelledError
        return future.result()


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity(workspace: Path, runtime: Path) -> dict:
    """Hash tracked and nonignored source, including uncommitted file contents.

    Git's ignore rules delimit generated artifacts. A non-Git workspace hashes
    all its files. Runtime metadata and Git internals are never source inputs.
    External symlinks are rejected rather than attested by their link name alone.
    """
    try:
        result = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                cwd=workspace, capture_output=True, timeout=15)
        if result.returncode == 0:
            names = set(os.fsdecode(item) for item in result.stdout.split(b"\0") if item)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace,
                                  capture_output=True, text=True, timeout=15)
            commit = head.stdout.strip() if head.returncode == 0 else None
        else:
            names, commit = set(), None
            for root, dirs, files in os.walk(workspace):
                if any((Path(root) / name).is_symlink() for name in dirs):
                    raise GreatMindsError("command source contains a directory symlink")
                dirs[:] = [name for name in dirs if name not in {".git", ".greatminds", ".worktrees"}
                           and not (Path(root) / name).resolve().is_relative_to(runtime)]
                names.update(str((Path(root) / name).relative_to(workspace)) for name in files)
    except subprocess.TimeoutExpired as exc:
        raise GreatMindsError("source identity Git check timed out") from exc
    records = []
    for name in sorted(names):
        path = workspace / name
        if ".git" in Path(name).parts or path.resolve().is_relative_to(runtime):
            continue
        if not path.resolve().is_relative_to(workspace):
            raise GreatMindsError("command source contains an external symlink")
        if path.is_dir():
            if path.is_symlink():
                raise GreatMindsError("command source contains a directory symlink")
            # In particular, a submodule cannot be reduced to its directory name.
            records.append((name, source_identity(path.resolve(), runtime)))
        elif path.is_file():
            records.append((name, _sha(path), path.stat().st_mode & 0o777,
                            os.readlink(path) if path.is_symlink() else None))
        else:
            records.append((name, "missing"))
    return {"commit": commit, "source_sha256": fingerprint(records)}


class CommandService:
    def __init__(self, store, *, environment=None):
        self.store = store
        self.environment = dict(os.environ if environment is None else environment)
        self.active: dict[str, asyncio.Task] = {}

    def _definition(self, run_id, command_id):
        raw = next((item for item in self.store.contracts(run_id)["execution"].get("commands", [])
                    if item["id"] == command_id), None)
        if raw is None:
            raise GreatMindsError("command is not in the run's pinned execution contract")
        return CommandDefinition(**raw)

    def request(self, run_id, command_id, *, token, request_id=None):
        run = self.store.authorize(run_id, token)
        request_id = safe_name(request_id or uuid.uuid4().hex)
        definition = self._definition(run_id, command_id)
        if run["role"] not in definition.roles:
            raise GreatMindsError("command is not authorized for the assigned role", exit_code=3)
        if definition.purpose != "validation" and not definition.authorized:
            raise GreatMindsError("deployment/publication command requires explicit project authorization", exit_code=3)
        with task_lock(self.store.runtime, run["task_id"]), self.store._transaction() as state:
            self.store.authorize(run_id, token)
            commands = state.setdefault("commands", {})
            existing = commands.get(request_id)
            if existing:
                if (existing["run_id"], existing["command_id"]) != (run_id, command_id):
                    raise GreatMindsError("command request identity was reused with different contents")
                return copy.deepcopy(existing)
            self.store._check_revision(TaskRevision(run["task_id"], run["task_path"], run["task_revision"]))
            if any(item["envelope"]["run_id"] == run_id for item in state["results"].values()):
                raise GreatMindsError("cannot execute commands after result submission")
            if any(item["run_id"] == run_id and item["status"] in UNRESOLVED for item in commands.values()):
                raise GreatMindsError("resolve the run's previous command before requesting another")
            record = {"id": request_id, "run_id": run_id, "task_id": run["task_id"],
                      "task_revision": run["task_revision"], "command_id": command_id,
                      "definition_sha256": definition.sha256, "status": "queued",
                      "requested_at": self.store.clock(), "workspace": run["workspace"],
                      "schema_sha256": run["schema_sha256"], "config_sha256": run["config_sha256"]}
            commands[request_id] = record
            self.store._event(state, "command_requested", run_id, {"command_request_id": request_id})
            return copy.deepcopy(record)

    def _update(self, request_id, **updates):
        with self.store._transaction() as state:
            record = state["commands"][request_id]
            record.update(copy.deepcopy(updates))
            self.store._event(state, "command_" + record["status"], record["run_id"],
                              {"command_request_id": request_id})
            return copy.deepcopy(record)

    def get(self, request_id):
        try:
            return self.store.snapshot().get("commands", {})[request_id]
        except KeyError as exc:
            raise GreatMindsError("unknown command request") from exc

    def resolve(self, request_id, *, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise GreatMindsError("command resolution requires an operator explanation")
        with self.store._transaction() as state:
            record = state.get("commands", {}).get(request_id)
            if not record or record["status"] != "needs_recovery":
                raise GreatMindsError("only an uncertain command can be resolved")
            record.update(status="resolved", resolution=reason, resolved_at=self.store.clock())
            self.store._event(state, "command_resolved", record["run_id"],
                              {"command_request_id": request_id, "reason": reason})
            return copy.deepcopy(record)

    def _environment(self, definition):
        if any(not self.environment.get(name) for name in definition.required_env):
            raise GreatMindsError("command requires missing environment variables", exit_code=3)
        env = {key: value for key, value in default_environment().items()}
        env.update({dest: self.environment[ref] for dest, ref in definition.environment
                    if ref in self.environment})
        return env

    def _environment_identity(self, env, argv, cwd):
        keyfile = self.store.directory / "evidence.key"
        with file_lock(self.store.directory / "evidence-key.lock", label="command evidence key"):
            if not keyfile.exists():
                atomic_bytes(keyfile, os.urandom(32))
            key = keyfile.read_bytes()
        search_path = os.pathsep.join(str((cwd / entry).resolve())
                                      for entry in env.get("PATH", os.defpath).split(os.pathsep))
        executable = shutil.which(argv[0], path=search_path)
        if "/" in argv[0]:
            executable = str((cwd / argv[0]).resolve())
        if not executable or not Path(executable).is_file():
            raise GreatMindsError("configured command executable is unavailable")
        return {"environment_hmac": hmac.new(key, json.dumps(env, sort_keys=True).encode(), hashlib.sha256).hexdigest(),
                "executable": str(Path(executable).resolve()), "executable_sha256": _sha(Path(executable))}

    async def recover(self, owner_id):
        for item in self.store.snapshot().get("commands", {}).values():
            if item["status"] not in {"queued", "starting", "running"}:
                continue
            if item.get("process"):
                await terminate_group(item["process"])
            # Even a recorded gate may have been released before a crash. No
            # exit receipt means unknown execution, never a successful check.
            uncertain = item.get("process") is not None
            self._update(item["id"], status="needs_recovery" if uncertain else "cancelled",
                         reason="supervisor_restart", finished_at=self.store.clock(), owner_id=owner_id)

    async def poll(self, owner_id):
        for request_id, future in list(self.active.items()):
            if future.done():
                future.result()
                del self.active[request_id]
        for item in self.store.snapshot().get("commands", {}).values():
            if item["status"] == "queued" and item["id"] not in self.active:
                self.active[item["id"]] = asyncio.create_task(self.execute(item["id"], owner_id))

    async def finish_run(self, run_id):
        for item in self.store.snapshot().get("commands", {}).values():
            if item["run_id"] != run_id:
                continue
            future = self.active.get(item["id"])
            if future and not future.done():
                future.cancel()
                await asyncio.gather(future, return_exceptions=True)
                # Persistence failures are fatal, not an excuse to release a
                # run while its command has an unrecorded outcome.
                if not future.cancelled():
                    future.result()
            if self.get(item["id"])["status"] == "queued":
                self._update(item["id"], status="cancelled", reason="run_ended", finished_at=self.store.clock())

    async def execute(self, request_id, owner_id):
        item = self.get(request_id)
        run = self.store.snapshot()["runs"][item["run_id"]]
        if item["status"] != "queued":
            return item
        if run["owner_id"] != owner_id or run["state"] != "running" or run.get("control", {}).get("status") in {"pending", "processing"}:
            return self._update(request_id, status="cancelled", reason="run_not_running", finished_at=self.store.clock())
        definition = self._definition(run["id"], item["command_id"])
        workspace = Path(run["workspace"])
        cwd = (workspace / definition.cwd).resolve()
        process, identity, drains = None, None, []
        output_records = {}
        status, reason, exit_code = "failed", "preflight_failed", None
        before, after, environment_identity = None, None, None
        self._update(request_id, status="starting", owner_id=owner_id,
                     started_at=self.store.clock(), argv=list(definition.argv), cwd=str(cwd))
        try:
            if not cwd.is_relative_to(workspace) or not cwd.is_dir():
                raise GreatMindsError("command cwd must exist inside its workspace")
            self.store._check_revision(TaskRevision(run["task_id"], run["task_path"], run["task_revision"]))
            env = self._environment(definition)
            environment_identity = await _blocking(self._environment_identity, env, definition.argv, cwd)
            before = await _blocking(source_identity, workspace, self.store.runtime)
            output_dir = self.store.directory / "command-output" / request_id
            _ensure_directory(output_dir)

            async def drain(stream, name):
                path = output_dir / name
                count, kept = 0, 0
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := await stream.read(65536):
                        count += len(chunk)
                        remaining = max(0, definition.max_output_bytes - kept)
                        output.write(chunk[:remaining])
                        kept += min(len(chunk), remaining)
                    output.flush()
                    os.fsync(output.fileno())
                output_records[name] = {"path": str(path), "sha256": _sha(path),
                                        "bytes": count, "stored_bytes": kept, "truncated": count > kept}

            gate_read, gate_write = os.pipe()
            try:
                try:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, str(Path(__file__).with_name("agent_exec.py")), str(gate_read),
                        *definition.argv, cwd=cwd, env=env, pass_fds=(gate_read,),
                        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, start_new_session=True)
                finally:
                    os.close(gate_read)
                identity = process_identity(process.pid)
                if identity is None:
                    raise RuntimeError("command launch gate exited before recording its identity")
                drains = [asyncio.create_task(drain(process.stdout, "stdout")),
                          asyncio.create_task(drain(process.stderr, "stderr"))]
                self._update(request_id, status="running", process=identity,
                             source_before=before, environment_identity=environment_identity)
                os.write(gate_write, b"1")
            finally:
                os.close(gate_write)
            reason = "exit"
            await asyncio.wait_for(process.wait(), timeout=definition.timeout_seconds)
            exit_code = process.returncode
            status = "succeeded" if exit_code == 0 else "failed"
        except asyncio.CancelledError:
            status, reason = "cancelled", "run_cancelled"
        except TimeoutError:
            status, reason = "failed", "timeout"
        except (GreatMindsError, OSError, ValueError, RuntimeError) as exc:
            reason = "execution_error:" + type(exc).__name__
        finally:
            if identity:
                await terminate_group(identity)
            if process:
                await process.wait()
            if drains:
                await asyncio.gather(*drains)
                _sync_directory(output_dir)
        if before:
            try:
                after = await _blocking(source_identity, workspace, self.store.runtime)
                self.store._check_revision(TaskRevision(run["task_id"], run["task_path"], run["task_revision"]))
                if status == "succeeded" and before != after:
                    status, reason = "stale", "source_changed_during_command"
            except asyncio.CancelledError:
                status, reason = "cancelled", "run_cancelled"
            except (GreatMindsError, OSError) as exc:
                status, reason = "stale", "identity_check_failed:" + type(exc).__name__
        return self._update(request_id, status=status, reason=reason, exit_code=exit_code,
                            finished_at=self.store.clock(), source_before=before, source_after=after,
                            environment_identity=environment_identity, output=output_records)

    def output_preview(self, request_id, *, limit=8192):
        """Return bounded text only from intact daemon-owned output artifacts."""
        if type(limit) is not int or not 0 <= limit <= 65536:
            raise GreatMindsError("output preview limit must be between 0 and 65536 bytes")
        item = self.get(request_id)
        previews = {}
        for name, record in item.get("output", {}).items():
            expected = self.store.directory / "command-output" / request_id / name
            if (name not in {"stdout", "stderr"} or Path(record["path"]) != expected
                    or expected.resolve() != expected):
                raise GreatMindsError("command output path is not the recorded local artifact", exit_code=3)
            try:
                fd = os.open(expected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise GreatMindsError("command output is not a regular file", exit_code=3)
                    digest, size, preview = hashlib.sha256(), 0, bytearray()
                    while chunk := stream.read(65536):
                        digest.update(chunk)
                        size += len(chunk)
                        if size > record["stored_bytes"]:
                            raise GreatMindsError("command output artifact changed", exit_code=3)
                        preview.extend(chunk[:max(0, limit - len(preview))])
            except OSError as exc:
                raise GreatMindsError("cannot read command output artifact", exit_code=4) from exc
            if size != record["stored_bytes"] or digest.hexdigest() != record["sha256"]:
                raise GreatMindsError("command output artifact changed", exit_code=3)
            previews[name] = {"text": preview.decode("utf-8", errors="replace"),
                              "truncated": record["truncated"] or size > limit,
                              "bytes": record["bytes"], "sha256": record["sha256"]}
        return previews

    def evidence(self, run, request_id, *, require_success=True):
        item = self.get(request_id)
        if item["run_id"] != run["id"] or item["task_revision"] != run["task_revision"]:
            raise GreatMindsError("command evidence belongs to a different assignment", exit_code=3)
        known_failure = (not require_success and item["status"] == "failed" and item.get("reason") == "exit"
                         and isinstance(item.get("exit_code"), int) and item["exit_code"] != 0)
        if item["status"] != "succeeded" and not known_failure:
            raise GreatMindsError("command evidence is not a successful, stable execution")
        if item.get("source_before") != item.get("source_after"):
            raise GreatMindsError("command evidence is stale: source changed during execution")
        definition = self._definition(run["id"], item["command_id"])
        workspace = Path(run["workspace"])
        try:
            current_source = source_identity(workspace, self.store.runtime)
        except OSError as exc:
            raise GreatMindsError("command evidence is stale: cannot inspect source") from exc
        if current_source != item["source_after"]:
            raise GreatMindsError("command evidence is stale: workspace changed")
        env = self._environment(definition)
        if self._environment_identity(env, definition.argv, Path(item["cwd"])) != item["environment_identity"]:
            raise GreatMindsError("command evidence is stale: environment or executable changed")
        for record in item["output"].values():
            try:
                valid = _sha(Path(record["path"])) == record["sha256"]
            except OSError:
                valid = False
            if not valid:
                raise GreatMindsError("command evidence output artifact changed")
        return item
