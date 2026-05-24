"""greatminds report-upstream — file a bug against the upstream greatminds repo.

Three submission modes, configurable via ``--mode`` or
``coord.yaml: report.mode``:

  ``url``        Default. URL-encode the body into
                 ``https://github.com/<repo>/issues/new?...`` and open it in
                 the user's browser (also printed to stdout). Zero creds,
                 zero side effects beyond a browser tab.
  ``gh``         Shell out to the local ``gh`` CLI (``gh issue create``);
                 requires ``gh auth login`` already done.
  ``api-token``  POST to the GitHub REST API directly, using a PAT read
                 from ``$<token_env>`` first, then
                 ``coordination/PROJECT.env``. No token is bundled in the
                 PyPI artifact.

Designed for MAINTAINER use only — other roles file inbox/maintainer/
asks; MAINTAINER triages \"наш баг vs апстрим\" and invokes this command
when the bug is upstream.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

import click
import yaml

from greatminds import __version__
from greatminds.cli._colors import err, info


# Default upstream repo. Sourced from this project's `pyproject.toml`
# ``[project.urls] Repository`` field, not guessed. Override in
# ``coord.yaml: report.upstream_repo: owner/name`` if a fork or mirror is
# the real bug-tracking destination.
DEFAULT_UPSTREAM_REPO = "veryviolet/greatminds"
DEFAULT_TOKEN_ENV = "GREATMINDS_UPSTREAM_TOKEN"
BODY_SIZE_CAP = 7000
JOURNAL_TAIL_LINES = 50


def _load_coord_yaml(project_root: Path) -> dict:
    for p in (project_root / "coord.yaml",
              project_root / "coordination" / "coord.yaml"):
        if p.is_file():
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
            except yaml.YAMLError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _resolve_project_root(project_dir: Path | None) -> Path:
    if project_dir:
        return project_dir.resolve()
    cfg = _load_coord_yaml(Path.cwd())
    pd = cfg.get("project_dir")
    if isinstance(pd, str) and Path(pd).is_dir():
        return Path(pd).resolve()
    return Path.cwd().resolve()


def _read_project_env(coord_dir: Path) -> dict[str, str]:
    p = coord_dir / "PROJECT.env"
    if not p.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or \
           (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _project_name(coord_dir: Path) -> str:
    p = coord_dir / "PROJECT.md"
    if not p.is_file():
        return "unknown"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    m = re.search(r"`<PROJECT_NAME>`\s*\|\s*`([^`]+)`", text)
    return m.group(1) if m else "unknown"


def _venv_install_kind() -> str:
    """Classify how the running greatminds was installed (PEP 610).

    Reads the importlib.metadata distribution for the currently-imported
    ``greatminds`` package — i.e. the one running this diagnostic. No
    assumption about *which* directory holds the venv; downstream users
    have a single ``.venv/`` from ``uv add greatminds`` or
    ``pip install greatminds``, and that's what gets reported.

    Returns one of: ``"PyPI wheel"`` (registry install — no
    direct_url.json), ``"editable from <url>"``, ``"local wheel/sdist"``,
    ``"VCS"``, ``"direct URL"``, ``"greatminds not installed"``, or
    ``"unknown"`` (malformed direct_url.json).
    """
    try:
        dist = importlib.metadata.distribution("greatminds")
    except importlib.metadata.PackageNotFoundError:
        return "greatminds not installed"
    durl_text = dist.read_text("direct_url.json")
    if durl_text is None:
        # Modern pip/uv only write direct_url.json for direct-URL installs;
        # absence means the package came from an index — typically PyPI.
        return "PyPI wheel"
    try:
        data = json.loads(durl_text)
    except json.JSONDecodeError:
        return "unknown"
    dir_info = data.get("dir_info") or {}
    if dir_info.get("editable") is True:
        src = data.get("url", "")
        return f"editable from {src}" if src else "editable"
    if data.get("archive_info"):
        return "local wheel/sdist"
    if data.get("vcs_info"):
        return "VCS"
    return "direct URL"


def _venv_layout() -> str:
    """One-line summary of the venv where greatminds is installed.

    Reports ``sys.prefix`` (the active python's venv root) plus the
    install kind. Downstream users get a single canonical line like
    ``/home/u/proj/.venv (PyPI wheel)``. The greatminds-dev repo uses a
    two-venv layout for self-bootstrap isolation, but that is a
    dev-only convention and is not assumed by shipped diagnostics.
    """
    return f"{sys.prefix} ({_venv_install_kind()})"


def _journal_tail(coord_dir: Path, n: int = JOURNAL_TAIL_LINES) -> str:
    p = coord_dir / "journal.ndjson"
    if not p.is_file():
        return ""
    try:
        return "\n".join(p.read_text(encoding="utf-8").splitlines()[-n:])
    except OSError:
        return ""


def _heartbeat_snapshot(coord_dir: Path) -> str:
    if not coord_dir.is_dir():
        return ""
    rows: list[str] = []
    now = time.time()
    for f in sorted(coord_dir.glob("heartbeat.*")):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime))
        rows.append(f"{f.name}: {iso} ({int(now - mtime)}s ago)")
    return "\n".join(rows)


def _coord_yaml_content(project_root: Path) -> str:
    p = project_root / "coord.yaml"
    if not p.is_file():
        return "(coord.yaml not found)"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return "(coord.yaml unreadable)"


def _assemble(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"## {h}\n\n{c}" for h, c in sections)


def _build_body(
    title: str,
    severity: str,
    user_body: str,
    project_root: Path,
    coord_dir: Path,
    include_diagnostics: bool,
) -> tuple[str, Path | None]:
    """Build the markdown report body.

    Returns ``(body, full_local_copy_path)``. The path is non-None iff the
    body was truncated to fit ``BODY_SIZE_CAP``; it points at a temp file
    holding the full pre-truncation body so the user can attach it manually
    after filing.
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    sections: list[tuple[str, str]] = [
        ("Header",
         f"- Title: {title}\n- Severity: {severity}\n- Generated: {ts}\n"),
    ]

    if include_diagnostics:
        sections.append(("Reporter context",
                         f"- greatminds: {__version__}\n"
                         f"- project_root: {project_root}\n"
                         f"- PROJECT_NAME: {_project_name(coord_dir)}\n"))
        sections.append(("Environment",
                         f"- platform: {platform.platform()}\n"
                         f"- python: {sys.version.split()[0]} "
                         f"({sys.executable})\n"
                         f"- venv: {_venv_layout()}\n"))

    body_text = user_body.strip() or "(no symptom body provided)"
    sections.append(("Symptom", body_text))

    if include_diagnostics:
        tail = _journal_tail(coord_dir)
        sections.append(("Journal tail (last 50 lines)",
                         f"```\n{tail}\n```" if tail
                         else "(journal.ndjson not found)"))
        hb = _heartbeat_snapshot(coord_dir)
        sections.append(("Heartbeats", hb or "(no heartbeat files)"))
        sections.append(("coord.yaml",
                         f"```yaml\n{_coord_yaml_content(project_root)}\n```"))

    sections.append(("Footer",
                     f"_Generated by `greatminds report-upstream` v{__version__}._"))

    body = _assemble(sections)
    if len(body) <= BODY_SIZE_CAP:
        return body, None

    # Write the full body once before any truncation.
    fd, tmp = tempfile.mkstemp(prefix="greatminds-report-", suffix=".md")
    os.close(fd)
    tmp_path = Path(tmp)
    tmp_path.write_text(body, encoding="utf-8")
    marker = f"... [truncated, full local copy at {tmp_path}]"

    def _set(prefix: str, content: str) -> str | None:
        """Replace the first section whose header starts with ``prefix``.

        Returns the prior content (so callers can compute budgets) or None
        if no section matched.
        """
        for i, (h, prior) in enumerate(sections):
            if h.startswith(prefix):
                sections[i] = (h, content)
                return prior
        return None

    # Step 1: drop the bulky diagnostics sections in the plan-mandated
    # order. Symptom + Environment are preserved by this pass.
    for prefix in ("Journal tail", "coord.yaml", "Heartbeats"):
        _set(prefix, marker)
        body = _assemble(sections)
        if len(body) <= BODY_SIZE_CAP:
            return body, tmp_path

    # Step 2: still over cap → the Symptom block itself is the overflow.
    # Truncate Symptom to fit while preserving as much prefix as possible,
    # appending the marker inside the section so the truncation is visible.
    symptom_prior = _set("Symptom", "")
    overhead = len(_assemble(sections))
    # Padding: section heading already accounted for by _assemble; reserve
    # space for marker + a blank line + safety.
    budget = BODY_SIZE_CAP - overhead - len(marker) - 8
    if symptom_prior is not None and budget > 0:
        truncated_symptom = symptom_prior[:budget].rstrip() + "\n\n" + marker
        _set("Symptom", truncated_symptom)
    else:
        _set("Symptom", marker)
    body = _assemble(sections)
    if len(body) <= BODY_SIZE_CAP:
        return body, tmp_path

    # Step 3: absolute safety net — Header + Reporter context + Environment +
    # Footer + four markers somehow still exceed the cap. Hard-cut the
    # assembled body and append a brief tail marker.
    tail = f"\n... [truncated to fit cap, full copy at {tmp_path}]"
    keep = max(0, BODY_SIZE_CAP - len(tail))
    body = body[:keep] + tail
    return body, tmp_path


def _submit_url(repo: str, title: str, body: str, labels: list[str]) -> int:
    params = [("title", title), ("body", body)]
    if labels:
        params.append(("labels", ",".join(labels)))
    qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    url = f"https://github.com/{repo}/issues/new?{qs}"
    click.echo(url)
    try:
        webbrowser.open(url)
    except (webbrowser.Error, OSError):
        pass
    return 0


def _submit_gh(repo: str, title: str, body: str, labels: list[str]) -> int:
    if not shutil.which("gh"):
        err("gh CLI not on PATH — install it or switch to `--mode url`")
        return 2
    auth = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True,
    )
    if auth.returncode != 0:
        err("gh not authenticated — run `gh auth login` or switch to `--mode url`")
        return 2
    fd, tmp = tempfile.mkstemp(
        prefix="greatminds-report-body-", suffix=".md",
    )
    os.close(fd)
    Path(tmp).write_text(body, encoding="utf-8")
    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body-file", tmp,
    ]
    for label in labels:
        cmd.extend(["--label", label])
    cp = subprocess.run(cmd, capture_output=True, text=True)
    if cp.stdout:
        click.echo(cp.stdout, nl=False)
    if cp.stderr:
        click.echo(cp.stderr, nl=False, err=True)
    return cp.returncode


def _submit_api(
    repo: str, title: str, body: str, labels: list[str], token: str,
) -> int:
    url = f"https://api.github.com/repos/{repo}/issues"
    payload = json.dumps(
        {"title": title, "body": body, "labels": labels}
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": f"greatminds-report-upstream/{__version__}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            data = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            detail = ""
        err(f"GitHub API error {exc.code}: {detail[:300]}")
        return 1
    except urllib.error.URLError as exc:
        err(f"network error: {exc.reason}")
        return 1
    if status != 201:
        err(f"unexpected status {status}: {data[:300]}")
        return 1
    try:
        html_url = json.loads(data).get("html_url", "")
    except json.JSONDecodeError:
        html_url = ""
    click.echo(html_url or f"(issue created, status={status})")
    return 0


@click.command(
    "report-upstream",
    short_help="file a bug against the upstream greatminds repo",
    help=__doc__,
)
@click.option("--title", required=True, help="issue title (short)")
@click.option("--body", "body_inline", default=None,
              help="issue body text (inline)")
@click.option("--body-file", "body_file",
              type=click.Path(dir_okay=False, path_type=Path),
              default=None,
              help="read body from FILE (use '-' for stdin)")
@click.option("--severity",
              type=click.Choice(["low", "normal", "high"]),
              default="normal", show_default=True)
@click.option("--label", "labels", multiple=True,
              help="label to attach (repeatable)")
@click.option("--mode",
              type=click.Choice(["url", "gh", "api-token"]),
              default=None,
              help="submission mode (overrides coord.yaml report.mode)")
@click.option("--dry-run", is_flag=True,
              help="print the assembled body to stdout and exit")
@click.option("--no-diagnostics", is_flag=True,
              help="skip auto-collected diagnostics (manual body only)")
@click.option("--project-dir",
              type=click.Path(file_okay=False, path_type=Path),
              default=None,
              help="override project root (default: cwd / coord.yaml)")
def report_upstream(
    title: str,
    body_inline: str | None,
    body_file: Path | None,
    severity: str,
    labels: tuple[str, ...],
    mode: str | None,
    dry_run: bool,
    no_diagnostics: bool,
    project_dir: Path | None,
) -> None:
    # Body source: --body OR --body-file (mutually exclusive), or stub.
    if body_inline is not None and body_file is not None:
        err("--body and --body-file are mutually exclusive")
        raise click.exceptions.Exit(2)
    if body_file is not None:
        if str(body_file) == "-":
            user_body = sys.stdin.read()
        else:
            try:
                user_body = body_file.read_text(encoding="utf-8")
            except OSError as exc:
                err(f"--body-file unreadable: {exc}")
                raise click.exceptions.Exit(2)
    else:
        user_body = body_inline or ""

    project_root = _resolve_project_root(project_dir)
    coord_dir = project_root / "coordination"
    cfg = _load_coord_yaml(project_root)
    report_cfg = cfg.get("report") if isinstance(cfg, dict) else None
    if not isinstance(report_cfg, dict):
        report_cfg = {}

    effective_mode = mode or report_cfg.get("mode") or "url"
    if effective_mode not in ("url", "gh", "api-token"):
        err(f"invalid mode: {effective_mode!r} "
            "(must be url | gh | api-token)")
        raise click.exceptions.Exit(2)

    repo = report_cfg.get("upstream_repo") or DEFAULT_UPSTREAM_REPO
    if (not isinstance(repo, str)
            or "/" not in repo
            or repo.startswith("<")):
        err("upstream_repo not configured — set "
            "`report.upstream_repo: owner/name` in coord.yaml")
        raise click.exceptions.Exit(2)

    default_labels = report_cfg.get("default_labels") or []
    if not isinstance(default_labels, list):
        default_labels = []
    final_labels: list[str] = []
    seen: set[str] = set()
    for raw in list(default_labels) + list(labels):
        if isinstance(raw, str) and raw and raw not in seen:
            seen.add(raw)
            final_labels.append(raw)

    body, full_path = _build_body(
        title=title,
        severity=severity,
        user_body=user_body,
        project_root=project_root,
        coord_dir=coord_dir,
        include_diagnostics=not no_diagnostics,
    )
    if full_path is not None:
        info(f"full body written to {full_path} "
             "(body truncated to fit size cap)")

    if dry_run:
        click.echo(body)
        return

    if effective_mode == "url":
        rc = _submit_url(repo, title, body, final_labels)
    elif effective_mode == "gh":
        rc = _submit_gh(repo, title, body, final_labels)
    else:  # api-token
        token_env = report_cfg.get("token_env") or DEFAULT_TOKEN_ENV
        token = os.environ.get(token_env)
        if not token:
            token = _read_project_env(coord_dir).get(token_env)
        if not token:
            err(f"no token found (env: {token_env}, "
                f"PROJECT.env key: {token_env}) — set it or switch mode")
            raise click.exceptions.Exit(2)
        rc = _submit_api(repo, title, body, final_labels, token)

    if rc != 0:
        raise click.exceptions.Exit(rc)


if __name__ == "__main__":
    report_upstream()
