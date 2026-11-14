# Contributing

Thanks for taking the time to improve cloudpanel-backup. Bug reports, new
storage backends, support for more application types and documentation fixes
are all welcome.

## Reporting a bug

Open an issue with:

- the command you ran and its output (`-vv` adds debug logging)
- your Python version, CloudPanel version (`clpctl --version` if available) and
  operating system
- the relevant part of your config file (it should not contain secrets; if it
  does, move them to the env file)
- for discovery problems: the vhost file and the directory layout, with real
  domain names replaced by `example.com`

Never paste access keys, passwords or database dumps.

## Development setup

You need Python 3.10 or newer. A CloudPanel server is not needed.

```console
$ git clone https://github.com/saizul/cloudpanel-backup.git
$ cd cloudpanel-backup
$ python -m venv .venv
$ . .venv/bin/activate
$ python -m pip install -e ".[dev]"
```

Before every commit:

```console
$ ruff check .
$ ruff format --check .
$ pytest
```

All three must pass; CI runs them on Python 3.10 to 3.13.

## Guidelines

- **Tests never run real programs against a server or use the network.** Use
  `FakeRunner` (fake `clpctl`, `mariadb-dump`, `age`, `rsync`, ...) and
  `FakeS3` from `tests/conftest.py`; the `server` fixture builds a CloudPanel-like
  directory tree in a temporary directory.
- **Every external program goes through `CommandRunner`** with an argument
  list, never a shell string.
- **No secrets in config files, logs or manifests.** New secrets get an `*_env`
  key.
- **Cite the source.** A CloudPanel command or an S3 API detail should point to
  the documentation it comes from.
- **The manifest format is a contract.** Changing it needs a new
  `format_version` and a changelog entry.
- **Every change comes with a test** that fails without it.
- Add a line to the `Unreleased` section of `CHANGELOG.md` for anything a user
  would notice.

## Project layout

```text
src/cloudpanel_backup/
    config.py      TOML config, validation, sample config
    discovery.py   sites from nginx vhosts and /home/*/htdocs, clpctl master credentials
    nginx.py       vhost tokenizer and parser
    appconfig.py   wp-config.php and .env parsing
    archive.py     tar.gz creation with excludes, safe extraction
    database.py    mariadb-dump / clpctl db:export, import
    crypto.py      age and GnuPG
    manifest.py    manifest.json
    storage/       local, rsync, S3 (built-in SigV4) and boto3 backends
    sigv4.py       AWS Signature Version 4
    catalog.py     finding backup sets in storage
    retention.py   GFS retention
    backup.py      the backup run
    restore.py     restore plan and execution
    verify.py      quick and deep verification
    schedule.py    systemd and cron
    notify.py      Telegram, webhook, email
    runner.py      subprocess wrapper (streaming gzip in and out)
    cli.py         command-line interface
tests/             pytest suite with fake runner, fake S3 and temporary site trees
examples/          configs for R2, B2, AWS, rsync, an offline demo server, a restore drill
```

## Pull requests

- Keep each pull request focused on one change.
- Describe what changed and why, and how you tested it.
- Do not bump the version number; that happens at release time.

By contributing you agree that your work is released under the MIT License.
