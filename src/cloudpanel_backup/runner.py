"""Running external programs.

Everything that touches the system (``clpctl``, ``mariadb-dump``, ``age``, ``gpg``, ``rsync``) goes through a
:class:`CommandRunner`, so the tests can swap in a fake one and never execute anything.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Protocol

log = logging.getLogger(__name__)


class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.args_list = list(args)
        self.returncode = returncode
        self.stderr = stderr
        tail = stderr.strip().splitlines()[-3:] if stderr else []
        super().__init__(f"{args[0]} exited with {returncode}" + (": " + " | ".join(tail) if tail else ""))


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class RunOptions:
    stdin_path: Path | None = None
    stdout_path: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    check: bool = True
    # Compress stdout with gzip on the fly when writing to stdout_path (level 1-9), or decompress stdin_path.
    gzip_stdout: int | None = None
    gunzip_stdin: bool = False


class CommandRunner(Protocol):
    def run(self, args: Sequence[str], options: RunOptions | None = None) -> CommandResult: ...

    def which(self, program: str) -> str | None: ...


def redact(args: Sequence[str]) -> list[str]:
    """Hide values of options that look like secrets before logging a command line."""
    out: list[str] = []
    for arg in args:
        lowered = arg.lower()
        if lowered.startswith(("--password=", "--passphrase=", "--secret")):
            out.append(arg.split("=", 1)[0] + "=***")
        else:
            out.append(arg)
    return out


class SubprocessRunner:
    """Runs commands with :mod:`subprocess`, never through a shell."""

    def which(self, program: str) -> str | None:
        return shutil.which(program)

    def run(self, args: Sequence[str], options: RunOptions | None = None) -> CommandResult:
        opts = options or RunOptions()
        log.debug("running %s", " ".join(redact(args)))
        env = None
        if opts.env:
            import os

            env = {**os.environ, **opts.env}
        if opts.gzip_stdout or opts.gunzip_stdin:
            return self._run_streaming(args, opts, env)
        stdin: IO[bytes] | None = None
        stdout: IO[bytes] | int = subprocess.PIPE
        try:
            if opts.stdin_path is not None:
                stdin = opts.stdin_path.open("rb")
            if opts.stdout_path is not None:
                stdout = opts.stdout_path.open("wb")
            proc = subprocess.run(
                list(args),
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                env=env,
                cwd=opts.cwd,
                check=False,
            )
        finally:
            if stdin is not None:
                stdin.close()
            if not isinstance(stdout, int):
                stdout.close()
        out = proc.stdout.decode("utf-8", "replace") if isinstance(proc.stdout, bytes) else ""
        err = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        result = CommandResult(list(args), proc.returncode, out, err)
        if opts.check and proc.returncode != 0:
            raise CommandError(args, proc.returncode, err)
        return result

    def _run_streaming(self, args: Sequence[str], opts: RunOptions, env: dict[str, str] | None) -> CommandResult:
        """Pipe stdout through gzip (dumps) or feed gunzipped data to stdin (imports) without temporary files."""
        import gzip
        import tempfile
        import threading

        err_file = tempfile.TemporaryFile()  # noqa: SIM115 - closed below
        proc = subprocess.Popen(
            list(args),
            stdin=subprocess.PIPE if opts.gunzip_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE if opts.gzip_stdout else subprocess.DEVNULL,
            stderr=err_file,
            env=env,
            cwd=opts.cwd,
        )
        feeder: threading.Thread | None = None
        feed_error: list[BaseException] = []
        if opts.gunzip_stdin and opts.stdin_path is not None:
            stdin_path = opts.stdin_path

            def feed() -> None:
                assert proc.stdin is not None
                try:
                    with gzip.open(stdin_path, "rb") as src:
                        shutil.copyfileobj(src, proc.stdin, 1024 * 1024)
                except BrokenPipeError:
                    pass
                except BaseException as exc:
                    feed_error.append(exc)
                finally:
                    proc.stdin.close()

            feeder = threading.Thread(target=feed, daemon=True)
            feeder.start()
        if opts.gzip_stdout and opts.stdout_path is not None:
            assert proc.stdout is not None
            with gzip.open(opts.stdout_path, "wb", compresslevel=opts.gzip_stdout) as dst:
                shutil.copyfileobj(proc.stdout, dst, 1024 * 1024)
            proc.stdout.close()
        returncode = proc.wait()
        if feeder is not None:
            feeder.join()
        err_file.seek(0)
        err = err_file.read().decode("utf-8", "replace")
        err_file.close()
        if feed_error:
            raise feed_error[0]
        result = CommandResult(list(args), returncode, "", err)
        if opts.check and returncode != 0:
            raise CommandError(args, returncode, err)
        return result
