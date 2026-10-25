"""Subprocess execution with streaming logs, timeouts and process-group cleanup."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import CiError
from .util import ensure_dir, isoformat, utc_now


@dataclass(slots=True)
class CommandResult:
    command: str
    cwd: Path
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    log_path: Path
    output_tail: str
    started_at: str
    environment_names: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        if self.timed_out:
            return "timed out"
        if self.exit_code is None:
            return "did not start"
        if self.exit_code == 0:
            return "passed"
        return f"exit {self.exit_code}"


def shell_executable() -> str:
    for candidate in ("/bin/bash", "/bin/sh"):
        if Path(candidate).is_file():
            return candidate
    fallback = shutil.which("sh")
    if fallback:
        return fallback
    raise CiError(
        "no POSIX shell found",
        remediation="ci-fabric command execution currently requires /bin/sh or /bin/bash.",
    )


def _shell_executable() -> str:
    return shell_executable()


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - POSIX is the supported runtime
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover
            process.kill()
    except ProcessLookupError:
        return
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)


def run_command(
    command: str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    log_path: Path,
    echo: Callable[[str], None] | None = None,
    extra_header: dict[str, str] | None = None,
) -> CommandResult:
    """Run one configured shell command.

    The command is a repository-authored string, not untrusted network input.
    Process-group termination ensures a timed-out command cannot leave children
    running and holding ports or files.
    """
    ensure_dir(log_path.parent)
    started = utc_now()
    started_monotonic = time.monotonic()
    tail: deque[str] = deque(maxlen=250)
    shell = _shell_executable()

    header_lines = [
        "# ci-fabric command log",
        f"# started: {isoformat(started)}",
        f"# cwd: {cwd}",
        f"# timeout_seconds: {timeout}",
        f"# environment: {', '.join(sorted(env))}",
    ]
    if extra_header:
        header_lines.extend(f"# {key}: {value}" for key, value in sorted(extra_header.items()))
    header_lines.extend(["# command:", command, "---"])

    process: subprocess.Popen[str] | None = None
    timed_out = False
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        log.write("\n".join(header_lines) + "\n")
        log.flush()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                shell=True,
                executable=shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            log.write(f"[ci-fabric] could not start command: {exc}\n")
            return CommandResult(
                command=command,
                cwd=cwd,
                exit_code=None,
                duration_seconds=time.monotonic() - started_monotonic,
                timed_out=False,
                log_path=log_path,
                output_tail=f"could not start command: {exc}",
                started_at=isoformat(started),
                environment_names=sorted(env),
            )

        def reader() -> None:
            stream = process.stdout
            if stream is None:
                return
            for line in stream:
                tail.append(line.rstrip("\n"))
                log.write(line)
                log.flush()
                if echo is not None:
                    echo(line.rstrip("\n"))

        thread = threading.Thread(target=reader, name="ci-fabric-log-reader", daemon=True)
        thread.start()
        exit_code: int | None
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            log.write(f"\n[ci-fabric] timed out after {timeout}s; terminating process group\n")
            _kill_process_group(process)
            exit_code = process.poll()
        finally:
            thread.join(timeout=5)
            stream = process.stdout
            if thread.is_alive():
                if stream is not None:
                    stream.close()
                thread.join(timeout=2)
            elif stream is not None:
                stream.close()
        log.write("\n---\n")
        log.write(
            f"# finished: {isoformat(utc_now())}\n"
            f"# exit_code: {exit_code}{' (timed out)' if timed_out else ''}\n"
        )

    duration = time.monotonic() - started_monotonic
    tail_text = "\n".join(tail)
    if timed_out:
        tail_text = (tail_text + f"\n[ci-fabric] timed out after {timeout}s").strip()
    return CommandResult(
        command=command,
        cwd=cwd,
        exit_code=exit_code,
        duration_seconds=duration,
        timed_out=timed_out,
        log_path=log_path,
        output_tail=tail_text,
        started_at=isoformat(started),
        environment_names=sorted(env),
    )


def exit_remediation(result: CommandResult) -> str | None:
    if result.timed_out:
        return "Increase stages.<name>.timeout only after confirming the command terminates."
    if result.exit_code == 127:
        return "The command or one of its tools was not found. Install it or fix PATH/venv."
    if result.exit_code == 126:
        return "The command is not executable. Check file permissions."
    if result.exit_code is None:
        return "The command could not be started. Verify the executable and its environment."
    if result.exit_code != 0:
        return (
            "Read the command log, reproduce the failing command in the same environment, "
            "and fix the reported problem before re-running this stage."
        )
    return None
