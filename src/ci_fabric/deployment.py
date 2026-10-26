"""Local deployment, health verification and rollback.

State lives in ``.localci/deploy/<environment>/``:

* ``current.json`` — the deployment that represents the environment now;
* ``previous.json`` — the last known-good deployment, used by explicit rollback;
* ``last-failure.json`` — the most recent failed attempt, for diagnosis;
* ``history.jsonl`` — append-only attempt history;
* ``releases/<deploy_id>/app`` — extracted, immutable application content.

Deployment is deliberately separate from release creation. A deploy can only
start from a verified local release, so "what runs" is always traceable to a
specific artifact hash and source revision.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from .config import Config, DeployEnvConfig, expand_env_value
from .context import Context
from .errors import CiError
from .release import ReleaseBundle, resolve_release, select_artifact
from .runner import exit_remediation, run_command, shell_executable
from .util import (
    archive_kind,
    ensure_dir,
    extract_archive,
    file_sha256,
    find_free_port,
    is_within,
    isoformat,
    load_json,
    port_is_open,
    read_text,
    relpath,
    remove_path,
    sha256_file,
    slug,
    truncate,
    utc_now,
    write_json,
)

USER_AGENT = "ci-fabric/0.1.0"
_POLL_INTERVAL = 0.25
_BODY_LIMIT = 256 * 1024


@dataclass(slots=True)
class DeployCheck:
    name: str
    ok: bool
    detail: str
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass(slots=True)
class DeployRecord:
    deploy_id: str
    environment: str
    strategy: str
    version: str
    release_version: str
    release_dir: str
    deploy_dir: str
    release_revision: str | None
    artifact: str
    artifact_sha256: str
    app_dir: str
    public_dir: str
    host: str
    port: int
    base_url: str
    pid: int | None = None
    pid_start_ticks: int | None = None
    log: str = ""
    started_at: str = ""
    status: str = "starting"
    health: dict[str, Any] = field(default_factory=dict)
    smoke: dict[str, Any] = field(default_factory=dict)
    env_keys: list[str] = field(default_factory=list)
    stopped_at: str | None = None
    restarted_at: str | None = None
    superseded_at: str | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.status == "active" and self.pid is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeployRecord:
        required = {
            "deploy_id",
            "environment",
            "strategy",
            "version",
            "release_version",
            "release_dir",
            "deploy_dir",
            "artifact",
            "artifact_sha256",
            "app_dir",
            "public_dir",
            "host",
            "port",
            "base_url",
        }
        missing = sorted(required - set(data))
        if missing:
            raise CiError(f"deployment state is missing field(s): {', '.join(missing)}")
        pid = data.get("pid")
        return cls(
            deploy_id=str(data["deploy_id"]),
            environment=str(data["environment"]),
            strategy=str(data["strategy"]),
            version=str(data["version"]),
            release_version=str(data["release_version"]),
            release_dir=str(data["release_dir"]),
            deploy_dir=str(data["deploy_dir"]),
            release_revision=(
                str(data["release_revision"]) if data.get("release_revision") is not None else None
            ),
            artifact=str(data["artifact"]),
            artifact_sha256=str(data["artifact_sha256"]),
            app_dir=str(data["app_dir"]),
            public_dir=str(data["public_dir"]),
            host=str(data["host"]),
            port=int(data["port"]),
            base_url=str(data["base_url"]),
            pid=int(pid) if isinstance(pid, int) else None,
            pid_start_ticks=(
                int(data["pid_start_ticks"])
                if isinstance(data.get("pid_start_ticks"), int)
                else None
            ),
            log=str(data.get("log", "")),
            started_at=str(data.get("started_at", "")),
            status=str(data.get("status", "starting")),
            health=dict(data.get("health", {})) if isinstance(data.get("health"), dict) else {},
            smoke=dict(data.get("smoke", {})) if isinstance(data.get("smoke"), dict) else {},
            env_keys=[str(item) for item in data.get("env_keys", []) if isinstance(item, str)],
            stopped_at=str(data["stopped_at"]) if data.get("stopped_at") else None,
            restarted_at=str(data["restarted_at"]) if data.get("restarted_at") else None,
            superseded_at=str(data["superseded_at"]) if data.get("superseded_at") else None,
            error=str(data["error"]) if data.get("error") else None,
        )


@dataclass(slots=True)
class DeployOutcome:
    ok: bool
    environment: str
    action: str
    checks: list[DeployCheck] = field(default_factory=list)
    record: DeployRecord | None = None
    rolled_back: bool = False
    rollback_version: str | None = None
    error: str | None = None
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "environment": self.environment,
            "action": self.action,
            "checks": [check.to_dict() for check in self.checks],
            "record": self.record.to_dict() if self.record else None,
            "rolled_back": self.rolled_back,
            "rollback_version": self.rollback_version,
            "error": self.error,
            "remediation": self.remediation,
        }


@dataclass(slots=True)
class DeployPaths:
    env_dir: Path
    current: Path
    previous: Path
    last_failure: Path
    history: Path
    lock: Path
    releases: Path


def _deploy_paths(root: Path, environment: str) -> DeployPaths:
    env_dir = (root / ".localci" / "deploy" / environment).resolve()
    try:
        env_dir.relative_to((root / ".localci").resolve())
    except ValueError as exc:
        raise CiError(f"invalid deployment environment name: {environment!r}") from exc
    return DeployPaths(
        env_dir=env_dir,
        current=env_dir / "current.json",
        previous=env_dir / "previous.json",
        last_failure=env_dir / "last-failure.json",
        history=env_dir / "history.jsonl",
        lock=env_dir / "deploy.lock",
        releases=env_dir / "releases",
    )


class _DeployLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def __enter__(self) -> None:
        ensure_dir(self._path.parent)
        handle = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise CiError(
                f"another deployment is already running for {self._path.parent.name}",
                remediation="Wait for it to finish, or inspect .localci/deploy history.",
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nstarted_at={isoformat(utc_now())}\n")
        handle.flush()
        self._handle = handle

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _load_record(path: Path) -> DeployRecord | None:
    if not path.is_file():
        return None
    data = load_json(path)
    if not isinstance(data, dict):
        raise CiError(f"invalid deployment state in {path}: expected an object")
    return DeployRecord.from_dict(data)


def _save_record(path: Path, record: DeployRecord) -> None:
    write_json(path, record.to_dict())


def _append_history(paths: DeployPaths, entry: dict[str, Any]) -> None:
    ensure_dir(paths.env_dir)
    payload = json.dumps({"at": isoformat(utc_now()), **entry}, sort_keys=True, ensure_ascii=False)
    with paths.history.open("a", encoding="utf-8") as handle:
        handle.write(payload + "\n")


def _check_environment(environment: DeployEnvConfig) -> list[DeployCheck]:
    checks: list[DeployCheck] = []
    missing = [name for name in environment.env_required if name not in os.environ]
    if missing:
        checks.append(
            DeployCheck(
                "environment.required",
                False,
                f"missing required environment variable(s): {', '.join(missing)}",
            )
        )
    else:
        checks.append(
            DeployCheck(
                "environment.required",
                True,
                (
                    f"{len(environment.env_required)} required variable(s) present"
                    if environment.env_required
                    else "no required environment variables declared"
                ),
            )
        )
    return checks


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_start_ticks(pid: int) -> int | None:
    """Return the Linux process start time, guarding against PID reuse."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        remainder = raw.rsplit(")", 1)[1].strip().split()
        return int(remainder[19])
    except (IndexError, ValueError):
        return None


_LIVE_CHILDREN: dict[int, subprocess.Popen[bytes]] = {}


def _liveness(record: DeployRecord) -> bool:
    pid = record.pid
    if pid is None:
        return False
    child = _LIVE_CHILDREN.get(pid)
    if child is not None:
        return child.poll() is None
    if not _pid_alive(pid):
        return False
    expected_ticks = record.pid_start_ticks
    if expected_ticks is not None:
        actual = _pid_start_ticks(pid)
        if actual is not None and actual != expected_ticks:
            return False
    return True


def _terminate(record: DeployRecord, timeout: int) -> bool:
    pid = record.pid
    if pid is None:
        return True
    if not _liveness(record):
        _LIVE_CHILDREN.pop(pid, None)
        return True
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        _LIVE_CHILDREN.pop(pid, None)
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _liveness(record):
            break
        time.sleep(0.1)
    if _liveness(record):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _liveness(record):
            time.sleep(0.1)
    child = _LIVE_CHILDREN.pop(pid, None)
    if child is not None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=5)
    return not _liveness(record)


def _spawn(command: str, *, cwd: Path, env: dict[str, str], log_path: Path) -> int:
    ensure_dir(log_path.parent)
    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            executable=shell_executable(),
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise CiError(
            f"could not start deployment command: {exc}",
            remediation="Check start_command, file permissions, and available interpreters.",
        ) from exc
    finally:
        handle.close()
    time.sleep(0.25)
    code = process.poll()
    if code is not None:
        detail = _tail(read_text(log_path, limit=_BODY_LIMIT))
        raise CiError(
            f"deployment command exited immediately with code {code}: {detail}",
            remediation="Run the same command manually to see why it terminates.",
        )
    _LIVE_CHILDREN[process.pid] = process
    return process.pid


def _tail(text: str, limit: int = 2000) -> str:
    value = text.strip()
    return value if len(value) <= limit else "..." + value[-limit:]


def _probe_host(host: str) -> str:
    # A deployment configured to bind a wildcard address is still probed
    # over loopback; nothing in this helper binds a socket.
    if host in {"0.0.0.0", "::", "[::]"}:
        return "127.0.0.1"
    return host


def _base_url(environment: DeployEnvConfig, host: str, port: int) -> str:
    if environment.base_url:
        value = environment.base_url.rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise CiError(f"deploy.{environment.name}.base_url must start with http:// or https://")
        return value
    return f"http://{_probe_host(host)}:{port}"


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(_BODY_LIMIT).decode("utf-8", "replace")
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        body = exc.read(_BODY_LIMIT).decode("utf-8", "replace") if exc.fp else ""
        return int(exc.code), body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise OSError(str(reason)) from exc


def _check_http(
    *,
    url: str,
    expected: str | None,
    timeout: int,
    label: str,
    record: DeployRecord,
    probe_process: bool,
) -> DeployCheck:
    started = time.monotonic()
    deadline = started + timeout
    last = "no response"
    while True:
        if probe_process and not _liveness(record):
            return DeployCheck(
                label,
                False,
                f"process exited before {label} succeeded ({last})",
                time.monotonic() - started,
            )
        remaining = max(0.1, deadline - time.monotonic())
        try:
            status, body = _http_get(url, timeout=min(5.0, remaining))
        except OSError as exc:
            last = f"connection failed: {truncate(str(exc), 200)}"
        else:
            if 200 <= status < 300:
                if expected and expected not in body:
                    last = f"HTTP {status} but body did not contain {expected!r}"
                else:
                    detail = f"HTTP {status}"
                    if expected:
                        detail += f"; body contained {expected!r}"
                    return DeployCheck(label, True, detail, time.monotonic() - started)
            else:
                last = f"HTTP {status}: {truncate(body.strip(), 200)}"
        if time.monotonic() >= deadline:
            return DeployCheck(
                label,
                False,
                f"timed out after {timeout}s ({last})",
                time.monotonic() - started,
            )
        time.sleep(_POLL_INTERVAL)


def _render_extra(
    root: Path, record: DeployRecord, app_dir: Path, public_dir: Path
) -> dict[str, str]:
    return {
        "port": str(record.port),
        "host": record.host,
        "base_url": record.base_url,
        "app": str(app_dir),
        "deploy_id": record.deploy_id,
        "env": record.environment,
        "public_dir": str(public_dir),
        "release_version": record.version,
        "release_dir": str((root / record.release_dir).resolve()),
        "deploy_dir": str((root / record.deploy_dir).resolve()),
    }


def _deploy_environment(
    context: Context,
    environment: DeployEnvConfig,
    record: DeployRecord,
    paths: DeployPaths,
) -> dict[str, str]:
    shell_env = dict(context.base_env)
    for key, value in environment.env.items():
        shell_env[key] = expand_env_value(value, os.environ)
    root = context.root
    shell_env.update(
        {
            "PORT": str(record.port),
            "HOST": record.host,
            "BASE_URL": record.base_url,
            "LOCALCI_DEPLOY_ID": record.deploy_id,
            "LOCALCI_ENV": record.environment,
            "LOCALCI_RELEASE_VERSION": record.version,
            "LOCALCI_RELEASE_DIR": str((root / record.release_dir).resolve()),
            "LOCALCI_APP_DIR": str((root / record.app_dir).resolve()),
            "LOCALCI_DEPLOY_STATE": str(paths.env_dir),
        }
    )
    return shell_env


def _resolve_app_dir(extract_dir: Path, app_subdir: str | None) -> Path:
    if app_subdir:
        candidate = (extract_dir / app_subdir).resolve()
        if not is_within(candidate, extract_dir):
            raise CiError(f"deploy app_subdir escapes the extracted artifact: {app_subdir}")
        if not candidate.is_dir():
            raise CiError(
                f"deploy app_subdir does not exist in the artifact: {app_subdir}",
                remediation="Check deploy.<env>.app_subdir against the archive contents.",
            )
        return candidate
    entries = [
        entry for entry in extract_dir.iterdir() if entry.name not in {".DS_Store", "__MACOSX"}
    ]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0].resolve()
    return extract_dir.resolve()


def _select_port(
    environment: DeployEnvConfig,
    *,
    requested: int | None = None,
) -> int:
    if requested is not None:
        return requested
    if environment.port:
        if port_is_open(_probe_host(environment.host), environment.port):
            raise CiError(
                f"port {environment.port} is already in use for deploy.{environment.name}",
                remediation=(
                    "Stop the process using the port, choose another deploy.<env>.port, "
                    "or set port = 0 to allocate a free port automatically."
                ),
            )
        return environment.port
    return find_free_port()


def _static_command(
    context: Context,
    environment: DeployEnvConfig,
    record: DeployRecord,
    app_dir: Path,
    public_dir: Path,
) -> str:
    if environment.server_command:
        return context.render(
            environment.server_command,
            extra=_render_extra(context.root, record, app_dir, public_dir),
        )
    python = shlex.quote(str(context.python_executable))
    public = shlex.quote(str(public_dir))
    host = shlex.quote(record.host)
    return f"{python} -m http.server {record.port} --bind {host} --directory {public}"


def _new_record(
    *,
    context: Context,
    environment: DeployEnvConfig,
    bundle: ReleaseBundle,
    artifact: Path,
    artifact_sha256: str,
    deploy_id: str,
    deploy_dir: Path,
    app_dir: Path,
    public_dir: Path,
    host: str,
    port: int,
) -> DeployRecord:
    now = utc_now()
    root = context.root
    record = DeployRecord(
        deploy_id=deploy_id,
        environment=environment.name,
        strategy=environment.strategy,
        version=bundle.version,
        release_version=bundle.version,
        release_dir=relpath(bundle.directory, root),
        deploy_dir=relpath(deploy_dir, root),
        release_revision=(
            str(bundle.manifest.get("revision"))
            if bundle.manifest.get("revision") is not None
            else None
        ),
        artifact=relpath(artifact, root),
        artifact_sha256=artifact_sha256,
        app_dir=relpath(app_dir, root),
        public_dir=relpath(public_dir, root),
        host=host,
        port=port,
        base_url=_base_url(environment, host, port),
        started_at=isoformat(now),
        log=relpath(deploy_dir / "deploy.log", root),
        env_keys=sorted(environment.env.keys()),
        status="starting",
    )
    return record


def _activate(
    context: Context,
    environment: DeployEnvConfig,
    paths: DeployPaths,
    record: DeployRecord,
    *,
    echo: Any,
) -> tuple[bool, list[DeployCheck], str | None, str | None]:
    checks: list[DeployCheck] = []
    app_dir = (context.root / record.app_dir).resolve()
    public_dir = (context.root / record.public_dir).resolve()
    if not app_dir.is_dir():
        return False, checks, f"application directory is missing: {record.app_dir}", None
    if not is_within(app_dir, context.root):
        return False, checks, f"application directory escapes repository: {record.app_dir}", None
    if record.strategy == "static" and not public_dir.is_dir():
        return (
            False,
            checks,
            f"static public_dir is missing: {record.public_dir}",
            "Check deploy.<env>.public_dir relative to the extracted application.",
        )
    if record.strategy == "static" and not is_within(public_dir, app_dir):
        return (
            False,
            checks,
            f"static public_dir escapes the application: {record.public_dir}",
            None,
        )

    env = _deploy_environment(context, environment, record, paths)
    extra = _render_extra(context.root, record, app_dir, public_dir)
    if record.strategy == "process":
        if not environment.start_command:
            return (
                False,
                checks,
                "strategy=process requires start_command",
                "Set deploy.<env>.start_command.",
            )
        command = context.render(environment.start_command, extra=extra)
        cwd = app_dir
    elif record.strategy == "static":
        command = _static_command(context, environment, record, app_dir, public_dir)
        cwd = app_dir
    else:  # pragma: no cover - validated by config
        return False, checks, f"unsupported deployment strategy: {record.strategy}", None

    deploy_dir = (context.root / record.deploy_dir).resolve()
    index = 1
    log_path = deploy_dir / "deploy.log"
    while log_path.exists():
        index += 1
        log_path = deploy_dir / f"deploy-{index}.log"
    record.log = relpath(log_path, context.root)
    echo(f"  starting {record.strategy} deployment on {record.base_url}")
    try:
        pid = _spawn(command, cwd=cwd, env=env, log_path=log_path)
    except CiError as exc:
        checks.append(DeployCheck("process.start", False, str(exc), 0.0))
        return False, checks, str(exc), exc.remediation
    record.pid = pid
    record.pid_start_ticks = _pid_start_ticks(pid)
    checks.append(DeployCheck("process.start", True, f"pid {pid}"))
    time.sleep(0.25)
    if not _liveness(record):
        detail = _tail(read_text(log_path)) if log_path.is_file() else "process exited"
        checks.append(DeployCheck("process.alive", False, detail))
        record.pid = None
        return (
            False,
            checks,
            f"deployment process exited during startup: {detail}",
            "Run start_command manually and inspect " + record.log + ".",
        )
    checks.append(DeployCheck("process.alive", True, f"pid {pid} is running"))

    health = _check_http(
        url=record.base_url + environment.health_path,
        expected=environment.health_expect,
        timeout=environment.health_timeout,
        label="health",
        record=record,
        probe_process=True,
    )
    checks.append(health)
    record.health = health.to_dict()
    if not health.ok:
        _terminate(record, environment.stop_timeout)
        record.pid = None
        return (
            False,
            checks,
            f"health check failed: {health.detail}",
            "Fix the application startup or increase deploy.<env>.health_timeout only after "
            "confirming it eventually becomes healthy.",
        )

    if environment.readiness_path:
        ready = _check_http(
            url=record.base_url + environment.readiness_path,
            expected=None,
            timeout=environment.health_timeout,
            label="readiness",
            record=record,
            probe_process=True,
        )
        checks.append(ready)
        if not ready.ok:
            _terminate(record, environment.stop_timeout)
            record.pid = None
            return False, checks, f"readiness check failed: {ready.detail}", None

    smoke = _check_http(
        url=record.base_url + environment.smoke_path,
        expected=environment.smoke_expect,
        timeout=environment.health_timeout,
        label="smoke-http",
        record=record,
        probe_process=True,
    )
    checks.append(smoke)
    record.smoke = {"http": smoke.to_dict()}
    if not smoke.ok:
        _terminate(record, environment.stop_timeout)
        record.pid = None
        return False, checks, f"smoke check failed: {smoke.detail}", None

    if environment.smoke_commands:
        smoke_results: list[dict[str, Any]] = []
        for index, raw_command in enumerate(environment.smoke_commands, start=1):
            command = context.render(raw_command, extra=extra)
            echo(f"  $ {command}")
            log = context.paths.logs_dir / (f"deploy-{record.environment}-smoke-{index:02d}.log")
            result = run_command(
                command,
                cwd=app_dir,
                env=env,
                timeout=float(environment.smoke_timeout),
                log_path=log,
                echo=echo,
            )
            smoke_results.append(
                {
                    "command": command,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "duration_seconds": round(result.duration_seconds, 3),
                    "log": relpath(log, context.root),
                }
            )
            checks.append(
                DeployCheck(
                    f"smoke-command-{index:02d}",
                    result.ok,
                    result.summary()
                    if result.ok
                    else f"{result.summary()}: {_tail(result.output_tail, 500)}",
                    result.duration_seconds,
                )
            )
            if not result.ok:
                _terminate(record, environment.stop_timeout)
                record.pid = None
                record.smoke["commands"] = smoke_results
                return (
                    False,
                    checks,
                    f"smoke command failed: {command}",
                    exit_remediation(result)
                    or "Fix the failing smoke command; a deployment that cannot prove "
                    "itself healthy is not deployed.",
                )
        record.smoke["commands"] = smoke_results

    record.status = "active"
    record.error = None
    checks.append(DeployCheck("deployment.active", True, f"serving {record.base_url}"))
    return True, checks, None, None


def _restart_target(
    context: Context,
    environment: DeployEnvConfig,
    paths: DeployPaths,
    record: DeployRecord,
    *,
    echo: Any,
) -> tuple[bool, list[DeployCheck], str | None, str | None]:
    app = (context.root / record.app_dir).resolve()
    if not app.is_dir():
        return (
            False,
            [DeployCheck("rollback.app", False, f"missing application content: {record.app_dir}")],
            f"rollback target application is missing: {record.app_dir}",
            "Keep the release directory until its deployments are no longer needed.",
        )
    artifact = context.root / record.artifact
    if artifact.is_file():
        digest, _ = file_sha256(artifact)
        if digest != record.artifact_sha256:
            return (
                False,
                [DeployCheck("rollback.artifact", False, "artifact digest changed")],
                f"rollback artifact no longer matches its recorded digest: {record.artifact}",
                "Do not restart changed content; recreate the release and deploy it.",
            )
    if _liveness(record):
        return (
            False,
            [DeployCheck("rollback.process", False, "target process already running")],
            f"rollback target process {record.pid} is already running",
            "Inspect deployment state; manual intervention is required.",
        )
    record.pid = None
    ok, checks, error, remediation = _activate(context, environment, paths, record, echo=echo)
    if ok:
        record.status = "active"
        record.restarted_at = isoformat(utc_now())
        record.error = None
    else:
        record.status = "failed"
        record.error = error
    return ok, checks, error, remediation


def _prune_releases(paths: DeployPaths, keep: set[str]) -> None:
    if not paths.releases.is_dir():
        return
    for candidate in paths.releases.iterdir():
        if not candidate.is_dir() or candidate.name in keep:
            continue
        with contextlib.suppress(OSError):
            remove_path(candidate)


def deploy(
    context: Context,
    environment_name: str,
    *,
    version: str | None = None,
    echo: Any = print,
) -> DeployOutcome:
    config: Config = context.config
    environment = config.deploy.get(environment_name)
    if environment is None:
        available = ", ".join(sorted(config.deploy)) or "none"
        raise CiError(
            f"unknown deployment environment: {environment_name}",
            remediation=(
                f"Configure [deploy.{environment_name}] in ci.toml. Available: {available}."
            ),
        )
    paths = _deploy_paths(context.root, environment_name)
    with _DeployLock(paths.lock):
        current = _load_record(paths.current)
        previous = _load_record(paths.previous)

        bundle = resolve_release(context.root, config, version)
        artifact = select_artifact(bundle, environment.artifact_name)
        kind = archive_kind(artifact)
        if kind is None:
            raise CiError(
                f"deployment artifact is not an archive: {artifact.name}",
                remediation=(
                    "Package the application as tar.gz, tgz, tar, or zip so it can be "
                    "extracted and verified before deployment."
                ),
            )
        artifact_sha256 = sha256_file(artifact)

        checks = _check_environment(environment)

        # Bring-your-own signature verification: a bundle that does not verify
        # is never extracted or started, regardless of any other gate.
        if config.release.verify_command:
            verify_log = context.paths.logs_dir / f"deploy-{environment_name}-verify-01.log"
            verify_result = run_command(
                context.render(
                    config.release.verify_command,
                    extra={"bundle_dir": str(bundle.directory), "version": bundle.version},
                ),
                cwd=bundle.directory,
                env=dict(context.base_env),
                timeout=120,
                log_path=verify_log,
            )
            checks.append(
                DeployCheck(
                    "signature",
                    verify_result.ok,
                    (
                        "signature verified"
                        if verify_result.ok
                        else f"verification failed: {verify_result.summary()}"
                    ),
                    verify_result.duration_seconds,
                )
            )
            if not verify_result.ok:
                _append_history(
                    paths,
                    {
                        "action": "deploy",
                        "ok": False,
                        "environment": environment_name,
                        "version": bundle.version,
                        "error": "release signature verification failed",
                    },
                )
                return DeployOutcome(
                    ok=False,
                    environment=environment_name,
                    action="deploy",
                    checks=checks,
                    error=f"release signature verification failed: {verify_result.summary()}",
                    remediation=(
                        "Do not deploy a bundle whose signature does not verify. "
                        "Re-create the release from a trusted revision and re-sign it."
                    ),
                )
        failing_env = [check for check in checks if not check.ok]
        if failing_env:
            return DeployOutcome(
                ok=False,
                environment=environment_name,
                action="deploy",
                checks=checks,
                error=failing_env[0].detail,
                remediation=(
                    "Export the required variables in the shell that runs the deployment. "
                    "ci-fabric never reads secrets from ci.toml."
                ),
            )

        now = utc_now()
        deploy_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{slug(bundle.version)}-{os.urandom(3).hex()}"
        deploy_dir = paths.releases / deploy_id
        extract_dir = ensure_dir(deploy_dir / "app")
        echo(f"  extracting {artifact.name}")
        try:
            extract_archive(artifact, extract_dir)
        except CiError as exc:
            with contextlib.suppress(OSError):
                remove_path(deploy_dir)
            return DeployOutcome(
                ok=False,
                environment=environment_name,
                action="deploy",
                checks=checks,
                error=f"could not extract deployment artifact: {exc}",
                remediation="Do not deploy a corrupt archive; recreate the release.",
            )
        app_dir = _resolve_app_dir(extract_dir, environment.app_subdir)
        if environment.strategy == "static":
            public_dir = (app_dir / environment.public_dir).resolve()
        else:
            public_dir = app_dir

        old_current = current
        previous_was_running = bool(
            old_current is not None
            and old_current.status == "active"
            and old_current.pid is not None
        )
        if previous_was_running and old_current is not None and old_current.pid is not None:
            echo(f"  stopping {old_current.version} (pid {old_current.pid})")
            stopped = _terminate(old_current, environment.stop_timeout)
            checks.append(
                DeployCheck(
                    "previous.stop",
                    stopped,
                    (
                        f"stopped pid {old_current.pid}"
                        if stopped
                        else f"could not stop pid {old_current.pid}"
                    ),
                )
            )
            if not stopped:
                _append_history(
                    paths,
                    {
                        "action": "deploy",
                        "ok": False,
                        "environment": environment_name,
                        "version": bundle.version,
                        "error": "could not stop the previous process",
                    },
                )
                return DeployOutcome(
                    ok=False,
                    environment=environment_name,
                    action="deploy",
                    checks=checks,
                    record=old_current,
                    error=(f"could not stop the previous deployment process {old_current.pid}"),
                    remediation=(
                        f"Stop it manually (kill the process group) and inspect {old_current.log}."
                    ),
                )
            old_current.status = "stopped"
            old_current.stopped_at = isoformat(utc_now())
            old_current.pid = None

        port = _select_port(environment)
        record = _new_record(
            context=context,
            environment=environment,
            bundle=bundle,
            artifact=artifact,
            artifact_sha256=artifact_sha256,
            deploy_id=deploy_id,
            deploy_dir=deploy_dir,
            app_dir=app_dir,
            public_dir=public_dir,
            host=environment.host,
            port=port,
        )
        ok, activation_checks, error, remediation = _activate(
            context, environment, paths, record, echo=echo
        )
        checks.extend(activation_checks)

        if not ok:
            record.status = "failed"
            record.error = error
            write_json(paths.last_failure, record.to_dict())
            rolled_back = False
            rollback_version: str | None = None
            if (
                environment.auto_rollback
                and previous_was_running
                and old_current is not None
                and (context.root / old_current.app_dir).is_dir()
            ):
                echo(f"  deployment failed; rolling back to {old_current.version}")
                rollback_ok, rollback_checks, rollback_error, rollback_remediation = (
                    _restart_target(context, environment, paths, old_current, echo=echo)
                )
                checks.extend(
                    DeployCheck(
                        f"rollback.{check.name}",
                        check.ok,
                        check.detail,
                        check.duration_seconds,
                    )
                    for check in rollback_checks
                )
                if rollback_ok:
                    rolled_back = True
                    rollback_version = old_current.version
                    current = old_current
                    _save_record(paths.current, current)
                    if previous is not None:
                        _save_record(paths.previous, previous)
                    error = f"{error}; automatic rollback restored {rollback_version}"
                    remediation = (
                        "Investigate the failed release before deploying it again. "
                        "The environment is running the previous release."
                    )
                else:
                    current = record
                    _save_record(paths.current, current)
                    error = f"{error}; automatic rollback also failed: {rollback_error}"
                    remediation = rollback_remediation or (
                        "The environment may be down. Inspect process logs and deploy a "
                        f"known-good release explicitly with `./ci deploy --env "
                        f"{environment_name} --version {old_current.version}`."
                    )
            else:
                current = record
                _save_record(paths.current, current)
                if previous_was_running and old_current is not None:
                    remediation = (
                        "Automatic rollback is disabled. The previous release is available at "
                        f"`./ci deploy --env {environment_name} --version {old_current.version}`."
                    )
                else:
                    remediation = remediation or "Fix the startup failure and redeploy."
            _append_history(
                paths,
                {
                    "action": "deploy",
                    "ok": False,
                    "environment": environment_name,
                    "deploy_id": record.deploy_id,
                    "version": bundle.version,
                    "rolled_back": rolled_back,
                    "rollback_version": rollback_version,
                    "error": error,
                },
            )
            _prune_releases(
                paths,
                keep={
                    record.deploy_id,
                    *([old_current.deploy_id] if old_current is not None else []),
                    *([previous.deploy_id] if previous is not None else []),
                },
            )
            return DeployOutcome(
                ok=False,
                environment=environment_name,
                action="deploy",
                checks=checks,
                record=current,
                rolled_back=rolled_back,
                rollback_version=rollback_version,
                error=error,
                remediation=remediation,
            )

        record.status = "active"
        if old_current is not None:
            old_current.status = "superseded"
            old_current.superseded_at = isoformat(utc_now())
            old_current.pid = None
            previous = old_current
        _save_record(paths.current, record)
        if previous is not None:
            _save_record(paths.previous, previous)
        else:
            with contextlib.suppress(FileNotFoundError):
                paths.previous.unlink()
        _append_history(
            paths,
            {
                "action": "deploy",
                "ok": True,
                "environment": environment_name,
                "deploy_id": record.deploy_id,
                "version": bundle.version,
                "artifact_sha256": record.artifact_sha256,
                "base_url": record.base_url,
            },
        )
        _prune_releases(
            paths,
            keep={
                record.deploy_id,
                *([previous.deploy_id] if previous is not None else []),
            },
        )
        return DeployOutcome(
            ok=True,
            environment=environment_name,
            action="deploy",
            checks=checks,
            record=record,
        )


def rollback(
    context: Context,
    environment_name: str,
    *,
    echo: Any = print,
) -> DeployOutcome:
    config = context.config
    environment = config.deploy.get(environment_name)
    if environment is None:
        raise CiError(
            f"unknown deployment environment: {environment_name}",
            remediation="Add [deploy.<env>] to ci.toml first.",
        )
    paths = _deploy_paths(context.root, environment_name)
    with _DeployLock(paths.lock):
        current = _load_record(paths.current)
        previous = _load_record(paths.previous)
        if previous is None:
            return DeployOutcome(
                ok=False,
                environment=environment_name,
                action="rollback",
                record=current,
                error="no previous deployment is recorded",
                remediation=(
                    "A successful deployment creates the rollback target. If this is the "
                    "first deployment, deploy an earlier verified release explicitly."
                ),
            )
        if previous.status not in {"active", "stopped", "superseded"}:
            return DeployOutcome(
                ok=False,
                environment=environment_name,
                action="rollback",
                record=current,
                error=(
                    f"previous deployment is not a valid rollback target (status={previous.status})"
                ),
                remediation="Deploy a known-good release explicitly instead.",
            )
        checks: list[DeployCheck] = []
        restored_original = False
        if current is not None and current.status == "active" and current.pid is not None:
            stopped = _terminate(current, environment.stop_timeout)
            checks.append(
                DeployCheck(
                    "current.stop",
                    stopped,
                    f"stopped pid {current.pid}" if stopped else "could not stop current process",
                )
            )
            if not stopped:
                return DeployOutcome(
                    ok=False,
                    environment=environment_name,
                    action="rollback",
                    checks=checks,
                    record=current,
                    error=f"could not stop the current deployment process {current.pid}",
                    remediation="Stop it manually before rolling back.",
                )
            current.status = "stopped"
            current.stopped_at = isoformat(utc_now())
            current.pid = None

        previous.status = "stopped"
        ok, activation_checks, error, remediation = _restart_target(
            context, environment, paths, previous, echo=echo
        )
        checks.extend(
            DeployCheck(f"rollback.{check.name}", check.ok, check.detail, check.duration_seconds)
            for check in activation_checks
        )
        if not ok:
            if current is not None and current.status == "stopped":
                echo("  rollback failed; restoring the deployment that was running")
                restore_ok, restore_checks, restore_error, _ = _restart_target(
                    context, environment, paths, current, echo=echo
                )
                checks.extend(
                    DeployCheck(
                        f"restore.{check.name}", check.ok, check.detail, check.duration_seconds
                    )
                    for check in restore_checks
                )
                if restore_ok:
                    restored_original = True
                    current.status = "active"
                    _save_record(paths.current, current)
                else:
                    current.status = "failed"
                    current.error = restore_error
                    _save_record(paths.current, current)
            _append_history(
                paths,
                {
                    "action": "rollback",
                    "ok": False,
                    "environment": environment_name,
                    "target_version": previous.version,
                    "restored_original": restored_original,
                    "error": error,
                },
            )
            return DeployOutcome(
                ok=False,
                environment=environment_name,
                action="rollback",
                checks=checks,
                record=current,
                error=error,
                remediation=remediation,
            )

        previous.status = "active"
        previous.error = None
        if current is not None:
            current.status = "superseded"
            current.superseded_at = isoformat(utc_now())
            current.pid = None
        _save_record(paths.current, previous)
        if current is not None:
            _save_record(paths.previous, current)
        _append_history(
            paths,
            {
                "action": "rollback",
                "ok": True,
                "environment": environment_name,
                "deploy_id": previous.deploy_id,
                "version": previous.version,
            },
        )
        _prune_releases(
            paths,
            keep={
                previous.deploy_id,
                *([current.deploy_id] if current is not None else []),
            },
        )
        return DeployOutcome(
            ok=True,
            environment=environment_name,
            action="rollback",
            checks=checks,
            record=previous,
        )


def undeploy(
    context: Context,
    environment_name: str,
    *,
    echo: Any = print,
) -> DeployOutcome:
    environment = context.config.deploy.get(environment_name)
    if environment is None:
        raise CiError(f"unknown deployment environment: {environment_name}")
    paths = _deploy_paths(context.root, environment_name)
    with _DeployLock(paths.lock):
        current = _load_record(paths.current)
        if current is None:
            return DeployOutcome(
                ok=True,
                environment=environment_name,
                action="undeploy",
                error=None,
            )
        checks: list[DeployCheck] = []
        if current.pid is not None and _liveness(current):
            stopped = _terminate(current, environment.stop_timeout)
            checks.append(
                DeployCheck(
                    "process.stop",
                    stopped,
                    f"stopped pid {current.pid}" if stopped else "process did not stop",
                )
            )
            if not stopped:
                return DeployOutcome(
                    ok=False,
                    environment=environment_name,
                    action="undeploy",
                    checks=checks,
                    record=current,
                    error=f"could not stop deployment process {current.pid}",
                    remediation="Stop the process manually and retry undeploy.",
                )
        else:
            checks.append(DeployCheck("process.stop", True, "no running process"))
        current.pid = None
        current.status = "stopped"
        current.stopped_at = isoformat(utc_now())
        _save_record(paths.current, current)
        _append_history(
            paths,
            {
                "action": "undeploy",
                "ok": True,
                "environment": environment_name,
                "deploy_id": current.deploy_id,
                "version": current.version,
            },
        )
        echo(f"  stopped {environment_name} ({current.version})")
        return DeployOutcome(
            ok=True,
            environment=environment_name,
            action="undeploy",
            checks=checks,
            record=current,
        )


def status(
    context: Context,
    environment_name: str,
    *,
    check_http: bool = True,
) -> dict[str, Any]:
    environment = context.config.deploy.get(environment_name)
    if environment is None:
        raise CiError(f"unknown deployment environment: {environment_name}")
    paths = _deploy_paths(context.root, environment_name)
    current = _load_record(paths.current)
    if current is None:
        return {"environment": environment_name, "deployed": False}
    running = _liveness(current)
    result: dict[str, Any] = {
        "environment": environment_name,
        "deployed": True,
        "record": current.to_dict(),
        "running": running,
    }
    if check_http and running:
        health = _check_http(
            url=current.base_url + environment.health_path,
            expected=environment.health_expect,
            timeout=min(environment.health_timeout, 10),
            label="health",
            record=current,
            probe_process=True,
        )
        result["health"] = health.to_dict()
    return result


def health(context: Context, environment_name: str) -> DeployOutcome:
    environment = context.config.deploy.get(environment_name)
    if environment is None:
        raise CiError(f"unknown deployment environment: {environment_name}")
    paths = _deploy_paths(context.root, environment_name)
    current = _load_record(paths.current)
    if current is None:
        return DeployOutcome(
            ok=False,
            environment=environment_name,
            action="health",
            error="no deployment is recorded",
            remediation=f"Run `./ci deploy --env {environment_name}` first.",
        )
    if not _liveness(current):
        return DeployOutcome(
            ok=False,
            environment=environment_name,
            action="health",
            record=current,
            error="deployment process is not running",
            remediation="Inspect logs, then deploy or roll back explicitly.",
        )
    check = _check_http(
        url=current.base_url + environment.health_path,
        expected=environment.health_expect,
        timeout=min(environment.health_timeout, 15),
        label="health",
        record=current,
        probe_process=True,
    )
    return DeployOutcome(
        ok=check.ok,
        environment=environment_name,
        action="health",
        checks=[check],
        record=current,
        error=None if check.ok else check.detail,
    )


def smoke(context: Context, environment_name: str, *, echo: Any = print) -> DeployOutcome:
    environment = context.config.deploy.get(environment_name)
    if environment is None:
        raise CiError(f"unknown deployment environment: {environment_name}")
    paths = _deploy_paths(context.root, environment_name)
    current = _load_record(paths.current)
    if current is None:
        return DeployOutcome(
            ok=False,
            environment=environment_name,
            action="smoke",
            error="no deployment is recorded",
            remediation=f"Run `./ci deploy --env {environment_name}` first.",
        )
    checks: list[DeployCheck] = []
    if not _liveness(current):
        checks.append(DeployCheck("process.alive", False, "deployment process is not running"))
        return DeployOutcome(
            ok=False,
            environment=environment_name,
            action="smoke",
            checks=checks,
            record=current,
            error="deployment process is not running",
        )
    checks.append(DeployCheck("process.alive", True, f"pid {current.pid}"))
    http_check = _check_http(
        url=current.base_url + environment.smoke_path,
        expected=environment.smoke_expect,
        timeout=min(environment.health_timeout, 15),
        label="smoke-http",
        record=current,
        probe_process=True,
    )
    checks.append(http_check)
    env = _deploy_environment(context, environment, current, paths)
    app_dir = (context.root / current.app_dir).resolve()
    public_dir = (context.root / current.public_dir).resolve()
    extra = _render_extra(context.root, current, app_dir, public_dir)
    for index, raw_command in enumerate(environment.smoke_commands, start=1):
        command = context.render(raw_command, extra=extra)
        echo(f"  $ {command}")
        log = context.paths.logs_dir / f"deploy-{environment_name}-smoke-{index:02d}.log"
        result = run_command(
            command,
            cwd=app_dir,
            env=env,
            timeout=float(environment.smoke_timeout),
            log_path=log,
            echo=echo,
        )
        checks.append(
            DeployCheck(
                f"smoke-command-{index:02d}",
                result.ok,
                result.summary()
                if result.ok
                else f"{result.summary()}: {_tail(result.output_tail, 500)}",
                result.duration_seconds,
            )
        )
    ok = all(check.ok for check in checks)
    return DeployOutcome(
        ok=ok,
        environment=environment_name,
        action="smoke",
        checks=checks,
        record=current,
        error=None if ok else "one or more smoke checks failed",
    )


def deploy_summary(context: Context) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for name in sorted(context.config.deploy):
        paths = _deploy_paths(context.root, name)
        try:
            current = _load_record(paths.current)
        except CiError as exc:
            summaries.append({"environment": name, "error": str(exc)})
            continue
        summaries.append(
            {
                "environment": name,
                "deployed": current is not None,
                "version": current.version if current else None,
                "base_url": current.base_url if current else None,
                "running": _liveness(current) if current else False,
                "status": current.status if current else None,
            }
        )
    return summaries


def latest_deploy_log(context: Context, environment_name: str) -> str | None:
    paths = _deploy_paths(context.root, environment_name)
    current = _load_record(paths.current)
    if current is None or not current.log:
        return None
    return current.log
