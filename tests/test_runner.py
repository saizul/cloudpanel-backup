"""SubprocessRunner against the Python interpreter itself (a harmless local program)."""

import gzip
import sys
from pathlib import Path

import pytest

from cloudpanel_backup.runner import CommandError, RunOptions, SubprocessRunner, redact


def test_run_captures_output():
    result = SubprocessRunner().run([sys.executable, "-c", "print('hi')"])
    assert result.stdout.strip() == "hi" and result.returncode == 0


def test_failure_raises_with_stderr():
    with pytest.raises(CommandError, match="exited with 3: bad thing"):
        SubprocessRunner().run([sys.executable, "-c", "import sys; sys.stderr.write('bad thing'); sys.exit(3)"])
    result = SubprocessRunner().run([sys.executable, "-c", "raise SystemExit(2)"], RunOptions(check=False))
    assert result.returncode == 2


def test_gzip_stdout_streaming(tmp_path: Path):
    out = tmp_path / "out.sql.gz"
    SubprocessRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 300000)"],
        RunOptions(stdout_path=out, gzip_stdout=6),
    )
    assert gzip.decompress(Path(out).read_bytes()) == b"x" * 300000


def test_gunzip_stdin_streaming(tmp_path: Path):
    src = tmp_path / "in.gz"
    with gzip.open(src, "wb") as fh:
        fh.write(b"line\n" * 1000)
    out = tmp_path / "count"
    code = "import sys; open(sys.argv[1], 'w').write(str(len(sys.stdin.buffer.read())))"
    SubprocessRunner().run([sys.executable, "-c", code, str(out)], RunOptions(stdin_path=src, gunzip_stdin=True))
    assert out.read_text() == "5000"


def test_env_and_files(tmp_path: Path):
    out = tmp_path / "o"
    src = tmp_path / "i"
    src.write_text("abc")
    SubprocessRunner().run(
        [sys.executable, "-c", "import os, sys; print(os.environ['CPB_X'] + sys.stdin.read())"],
        RunOptions(stdin_path=src, stdout_path=out, env={"CPB_X": "env-"}),
    )
    assert out.read_text().strip() == "env-abc"


def test_which_and_redact():
    assert SubprocessRunner().which("definitely-not-a-program-xyz") is None
    assert redact(["clpctl", "--password=secret", "--name=x"]) == ["clpctl", "--password=***", "--name=x"]
