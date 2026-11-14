"""Optional encryption of backup files with age or GnuPG (both run as external programs)."""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path

from cloudpanel_backup.config import ConfigError, EncryptionSettings, require_env
from cloudpanel_backup.runner import CommandRunner
from cloudpanel_backup.util import write_private

SUFFIXES = {"age": ".age", "gpg": ".gpg"}


class Encryptor:
    def __init__(self, settings: EncryptionSettings, runner: CommandRunner) -> None:
        self.settings = settings
        self.runner = runner

    @property
    def method(self) -> str:
        return self.settings.method

    @property
    def enabled(self) -> bool:
        return self.settings.method != "none"

    @property
    def suffix(self) -> str:
        return SUFFIXES.get(self.settings.method, "")

    def check_available(self) -> None:
        program = {"age": "age", "gpg": "gpg"}.get(self.method)
        if program and self.runner.which(program) is None:
            raise ConfigError(f"encryption method {self.method!r} needs the '{program}' program")

    def _gpg_base(self) -> list[str]:
        args = ["gpg", "--batch", "--yes", "--no-tty"]
        if self.settings.gpg_homedir:
            args += ["--homedir", str(self.settings.gpg_homedir)]
        return args

    def encrypt(self, source: Path) -> Path:
        """Encrypt ``source`` to ``source + suffix`` and delete the plaintext."""
        if not self.enabled:
            return source
        target = source.with_name(source.name + self.suffix)
        if self.method == "age":
            args = ["age", "--encrypt"]
            for recipient in self.settings.recipients:
                args += ["--recipient", recipient]
            if self.settings.recipients_file:
                args += ["--recipients-file", str(self.settings.recipients_file)]
            args += ["--output", str(target), str(source)]
        else:
            args = [
                *self._gpg_base(),
                "--trust-model",
                "always",
                "--encrypt",
                "--recipient",
                str(self.settings.gpg_recipient),
                "--output",
                str(target),
                str(source),
            ]
        self.runner.run(args)
        source.unlink()
        return target

    @contextlib.contextmanager
    def _passphrase_file(self) -> Iterator[Path | None]:
        if not self.settings.gpg_passphrase_env:
            yield None
            return
        secret = require_env(self.settings.gpg_passphrase_env, "GnuPG passphrase")
        with tempfile.TemporaryDirectory(prefix="cpb-") as tmp:
            path = Path(tmp) / "passphrase"
            write_private(path, secret)
            yield path

    def decrypt(self, source: Path, method: str, identity_file: Path | None = None) -> Path:
        """Decrypt a file made by :meth:`encrypt` (``method`` comes from the backup manifest)."""
        if method == "none":
            return source
        suffix = SUFFIXES[method]
        if not source.name.endswith(suffix):
            raise ValueError(f"{source} does not end with {suffix}")
        target = source.with_name(source.name[: -len(suffix)])
        if method == "age":
            identity = identity_file or self.settings.identity_file
            if identity is None:
                raise ConfigError("restoring an age backup needs [encryption] identity_file or --identity")
            self.runner.run(["age", "--decrypt", "--identity", str(identity), "--output", str(target), str(source)])
        else:
            with self._passphrase_file() as pass_file:
                args = self._gpg_base()
                if pass_file is not None:
                    args += ["--pinentry-mode", "loopback", "--passphrase-file", str(pass_file)]
                args += ["--decrypt", "--output", str(target), str(source)]
                self.runner.run(args)
        source.unlink()
        return target
