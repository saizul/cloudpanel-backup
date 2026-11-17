# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Exclusive run lock for `backup`, `restore` and `prune` (an advisory `flock` on
  `<staging_dir>/cloudpanel-backup.lock`), so a timer run and a manual run never overlap. A refused run names the
  holder (command, PID, host, start time) and exits with the new code `3`. New `[backup] lock_file` and
  `lock_timeout` settings and global `--lock-timeout SECONDS` / `--no-lock` options.
- Heartbeat pings for Healthchecks.io-style monitors: `[notify] heartbeat_url_env` names a variable with the ping
  URL; `backup` pings `/start`, success or `/fail`, and `test-notify` sends a test ping.
- `CODE_OF_CONDUCT.md`, issue templates for bug reports and feature requests, and a pull request template.

### Fixed

- Discovery skips home and `htdocs` folders it cannot read (permission denied) with a warning instead of stopping.

## [0.1.0] - 2026-11-14

### Added

- Site discovery from CloudPanel's nginx vhosts (`server_name`, `root`, PHP-FPM, `proxy_pass`) and
  `/home/*/htdocs/*`, skipping CloudPanel's own `clp` user; per-site overrides with `[[site]]`.
- WordPress (`wp-config.php`), Laravel and `.env` detection for database names and credentials; CloudPanel master
  credentials (`clpctl db:show:master-credentials`) as a fallback.
- File archives with a built-in exclude list (caches, `node_modules`, `.git`, logs) and custom patterns.
- Database dumps with `mariadb-dump --single-transaction` compressed on the fly, or `clpctl db:export`; passwords
  passed through a temporary option file.
- Storage backends: S3-compatible with a built-in Signature V4 client (multipart upload, retries), boto3 as an
  optional extra, a local directory, and rsync over SSH.
- Optional encryption with age or GnuPG.
- `manifest.json` with sizes and SHA-256 checksums of stored and plaintext files, written last.
- Grandfather-father-son retention and cleanup of incomplete backups.
- `verify` (quick and `--deep`), `restore` with dry run, checksum checks, safe extraction, `--db-map`,
  `--from-host` and moving existing files aside.
- systemd service/timer and cron generator (`schedule`), Telegram, webhook and email notifications.
- `discover`, `list`, `prune`, `init-config` and `test-notify` commands, `--env-file` for secrets.
- Docker image published to `ghcr.io/saizul/cloudpanel-backup`.

[Unreleased]: https://github.com/saizul/cloudpanel-backup/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/saizul/cloudpanel-backup/releases/tag/v0.1.0
