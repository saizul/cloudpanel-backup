# cloudpanel-backup

[![CI](https://github.com/saizul/cloudpanel-backup/actions/workflows/ci.yml/badge.svg)](https://github.com/saizul/cloudpanel-backup/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Back up and restore the sites of a [CloudPanel](https://www.cloudpanel.io/) server. It finds every site,
archives its files without the cache and `node_modules`, dumps its MariaDB databases with
`--single-transaction`, optionally encrypts everything with age or GnuPG, uploads it to Cloudflare R2, Backblaze
B2, AWS S3 (or any S3-compatible service), a local directory or an rsync target, and writes a manifest with SHA-256
checksums. Old backups are pruned with a daily/weekly/monthly (GFS) policy, `verify` proves the backups are intact,
and `restore` puts a site back, with a dry run first.

CloudPanel's built-in remote backup is a good start, but it backs up whole home directories in one job. On a VPS
with twenty WordPress sites you usually want one backup set per site, a database dump that is consistent, a
retention policy that keeps last month's copy, a notification when something breaks, and a restore you can test
without overwriting production. That is what this tool does.

```console
$ cloudpanel-backup discover
shop.example.com                         wordpress  user=shop             db=shop_wp
    /home/shop/htdocs/shop.example.com  (vhost /etc/nginx/sites-enabled/shop.example.com.conf)
api.example.com                          proxy      user=api              db=-
    /home/api/htdocs/api.example.com  (vhost /etc/nginx/sites-enabled/api.example.com.conf)

$ cloudpanel-backup backup
shop.example.com                         ok      412.6 MiB (94s, pruned 1)
api.example.com                          ok      3.1 MiB (2s)
2/2 site(s) backed up, 415.7 MiB uploaded
```

## Contents

- [Features](#features)
- [Installation](#installation)
- [Docker image](#docker-image)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [How sites are discovered](#how-sites-are-discovered)
- [Storage layout and manifest](#storage-layout-and-manifest)
- [Retention](#retention)
- [Restore](#restore)
- [Scheduling](#scheduling)
- [Run lock](#run-lock)
- [Notifications](#notifications)
- [Heartbeat monitoring](#heartbeat-monitoring)
- [Examples](#examples)
- [Testing](#testing)
- [Security notes](#security-notes)
- [Limitations](#limitations)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Site discovery**: reads the nginx vhosts CloudPanel writes to `/etc/nginx/sites-enabled/` (`server_name`,
  `root`, PHP-FPM or `proxy_pass`) and scans `/home/*/htdocs/*` for anything the vhosts do not mention.
  CloudPanel's own `clp` user is never treated as a site.
- **Application detection**: WordPress (`wp-config.php` in the document root or one level above, as WordPress
  itself looks for it), Laravel and other `.env` applications. Database names and credentials come from those
  files, so no extra configuration is needed for a typical site.
- **Files**: one `files.tar.gz` per site with a built-in exclude list (WordPress caches, `node_modules`, `.git`,
  Laravel and Magento caches, `*.log`) plus your own patterns per server or per site. Symlinks are stored as links,
  sockets and device files are skipped.
- **Databases**: `mariadb-dump --single-transaction --quick --routines --triggers --hex-blob`, compressed on the
  fly (no uncompressed dump on disk), with the password in a temporary `--defaults-extra-file` (mode 0600) so it
  never shows up in `ps`. `clpctl db:export` is available as an alternative, and the CloudPanel master credentials
  (`clpctl db:show:master-credentials`) are the fallback when a site has no credentials of its own.
- **Storage**: S3-compatible object storage with a built-in Signature V4 client (no dependencies, multipart upload
  for large files, retries on 5xx), or boto3 as an optional extra; a local directory or mount; rsync over SSH.
- **Encryption**: optional, with [age](https://age-encryption.org/) recipients or a GnuPG public key. The server
  only needs the public key; the private key stays somewhere else and is only needed for restore.
- **Manifest**: every backup ends with a `manifest.json` that lists each file with its size and SHA-256 (of the
  stored file and of the plaintext). A backup without a manifest is incomplete.
- **Retention**: grandfather-father-son (`daily`, `weekly`, `monthly`), applied after each successful backup;
  leftovers of failed runs are cleaned up after 24 hours.
- **Verify**: quick (manifest against the stored sizes) or `--deep` (download, hash, decrypt if the key is
  available, and read the whole gzip/tar stream).
- **Restore**: dry-run plan, checksum verification before anything is touched, extraction into a fresh directory
  with protection against path traversal, the old files moved aside instead of deleted, database import into the
  same or another database (`--db-map old=new`), ownership fixed for the site user.
- **Scheduling**: prints or installs a systemd service and timer, or a `/etc/cron.d` line. An exclusive run lock
  keeps a timer run and a manual run (or a long backup and the next timer run) from overlapping.
- **Notifications**: Telegram, a JSON webhook (Slack/Discord bridges, n8n, healthchecks) and email over SMTP, on
  failure only or always, plus heartbeat pings (start, success, fail) for Healthchecks.io-style monitors that also
  alert when a scheduled run never happens.

## Installation

Python 3.10 or newer, as root on the CloudPanel server. There are no required dependencies on Python 3.11+
(`tomli` on 3.10).

```bash
python3 -m venv /opt/cloudpanel-backup
/opt/cloudpanel-backup/bin/pip install "cloudpanel-backup @ git+https://github.com/saizul/cloudpanel-backup"
ln -s /opt/cloudpanel-backup/bin/cloudpanel-backup /usr/local/bin/cloudpanel-backup

# optional: boto3 backend
/opt/cloudpanel-backup/bin/pip install "cloudpanel-backup[s3] @ git+https://github.com/saizul/cloudpanel-backup"
```

The programs it calls are already on a CloudPanel server (`mariadb-dump`, `mariadb`, `clpctl`, `rsync`, `ssh`).
Install `age` (`apt install age`) or `gnupg` only if you want encryption.

## Docker image

A multi-arch image (linux/amd64, linux/arm64) is published to GitHub Packages. It contains the MariaDB client,
age, GnuPG, rsync and the SSH client.

```bash
docker pull ghcr.io/saizul/cloudpanel-backup:latest

docker run --rm ghcr.io/saizul/cloudpanel-backup:latest schedule --cron

# A real backup needs to read every site, so it runs as root with the site directories mounted read-only.
# Use --network host so mariadb-dump reaches the database on 127.0.0.1.
docker run --rm --user 0 --network host \
  -v /home:/home:ro -v /etc/nginx:/etc/nginx:ro \
  -v /etc/cloudpanel-backup:/etc/cloudpanel-backup:ro \
  -v /var/backups/cloudpanel-backup:/var/backups/cloudpanel-backup \
  ghcr.io/saizul/cloudpanel-backup:latest --env-file /etc/cloudpanel-backup/env backup
```

Release images are also tagged with the version (`ghcr.io/saizul/cloudpanel-backup:0.1.0`) and every build with
its commit SHA. The image runs as an unprivileged user (uid 10001) unless you pass `--user`. `clpctl` is not
available inside the container, so keep `db_method = "mariadb-dump"` and give the database credentials through
the site config files or `[database] user` / `password_env`.

## Quick start

```bash
mkdir -p /etc/cloudpanel-backup
cloudpanel-backup init-config --output /etc/cloudpanel-backup/config.toml   # then edit [storage]
install -m 600 /dev/null /etc/cloudpanel-backup/env                         # then add the S3 keys
cloudpanel-backup discover                 # what would be backed up
cloudpanel-backup --env-file /etc/cloudpanel-backup/env backup --dry-run
cloudpanel-backup --env-file /etc/cloudpanel-backup/env backup
cloudpanel-backup --env-file /etc/cloudpanel-backup/env verify --deep
cloudpanel-backup schedule --install && systemctl daemon-reload && systemctl enable --now cloudpanel-backup.timer
```

## Usage

```text
cloudpanel-backup [--config FILE] [--env-file FILE] [--host NAME] [--lock-timeout SECONDS] [--no-lock] [-v|-q]
                  COMMAND

  init-config   print or write a sample config file (--output, mode 0600)
  discover      list the sites and databases that would be backed up (--json)
  backup        back up all sites, or --site DOMAIN (repeatable); --dry-run, --no-prune, --no-notify, --json
  list          list backups in storage (--site, --all-hosts, --json)
  restore       restore a site: DOMAIN [--timestamp T] [--from-host H] [--files-only|--db-only]
                [--database NAME] [--target DIR] [--db-map OLD=NEW] [--identity FILE] [--dry-run]
                [--force] [--no-keep-previous] [--no-chown]
  verify        check the latest backup of each site (--all, --deep, --site, --identity, --json)
  prune         apply the retention policy (--site, --dry-run)
  schedule      print systemd units (or --cron); --install writes them, --verify-after adds a verify run
  test-notify   send a test message to every configured channel (and a heartbeat ping, if configured)
```

Exit codes: `0` success, `1` a backup, verify or restore failed, `2` configuration or usage error, `3` another run
holds the [run lock](#run-lock).

## Configuration

The config is TOML, by default `/etc/cloudpanel-backup/config.toml`. Secrets are never written in it: every secret
is the *name* of an environment variable (`*_env` keys), and a key such as `password = "..."` is rejected. Put the
variables in `/etc/cloudpanel-backup/env` (`KEY=value`, mode 0600); the systemd unit loads it and so does
`--env-file`.

| Section | Key | Default | Meaning |
| ------- | --- | ------- | ------- |
| `[backup]` | `staging_dir` | `/var/backups/cloudpanel-backup` | where archives and dumps are built (needs free space for the largest site) |
| | `prefix` | `cloudpanel` | first segment of every object key |
| | `host` | short hostname | second segment; lets several servers share a bucket |
| | `sites` / `exclude_sites` | `["*"]` / `[]` | shell patterns of domains |
| | `exclude` | `[]` | extra exclude patterns, added to the built-in list |
| | `use_default_excludes` | `true` | turn the built-in list off |
| | `databases` | `true` | back up databases |
| | `db_method` | `mariadb-dump` | or `clpctl` |
| | `compression_level` | `6` | gzip level 0-9 |
| | `home_root` / `nginx_dir` | `/home` / `/etc/nginx/sites-enabled` | where to look for sites |
| | `lock_file` | `<staging_dir>/cloudpanel-backup.lock` | the [run lock](#run-lock) |
| | `lock_timeout` | `0` | seconds to wait for another run before giving up |
| `[database]` | `user`, `password_env` | unset | one account for every database (for example a backup user) |
| | `host`, `port` | `127.0.0.1`, `3306` | used with `user` |
| | `use_master_credentials` | `true` | fall back to `clpctl db:show:master-credentials` |
| | `extra_dump_options` | `[]` | added to the `mariadb-dump` command line |
| `[storage]` | `type` | `local` | `s3`, `local` or `rsync` |
| | `bucket`, `endpoint`, `region` | | S3; leave `endpoint` out for AWS |
| | `access_key_env`, `secret_key_env` | `CPB_S3_ACCESS_KEY_ID`, `CPB_S3_SECRET_ACCESS_KEY` | names of the variables |
| | `backend` | `builtin` | or `boto3` |
| | `path_style`, `multipart_threshold_mb`, `storage_class` | `true`, `64`, unset | S3 details |
| | `path` | `/var/backups/cloudpanel-backup-archive` | local |
| | `target`, `ssh_port`, `ssh_key`, `rsync_options` | | rsync (`user@host:/path`) |
| `[encryption]` | `method` | `none` | `age` or `gpg` |
| | `recipients`, `recipients_file`, `identity_file` | | age |
| | `gpg_recipient`, `gpg_homedir`, `gpg_passphrase_env` | | GnuPG |
| `[retention]` | `daily`, `weekly`, `monthly`, `min_keep` | `7`, `4`, `6`, `1` | see [Retention](#retention) |
| `[notify]` | `on` | `failure` | `always`, `failure` or `never` |
| | `heartbeat_url_env` | unset | variable holding a [heartbeat](#heartbeat-monitoring) ping URL |
| `[[site]]` | `domain`, `skip`, `databases`, `exclude`, `path` | | per-site overrides |

Exclude patterns are relative to the site directory. A pattern without `/` (`node_modules`, `*.log`) matches a
file or directory name at any depth; a pattern with `/` (`wp-content/cache`) matches that path at any depth; a
leading `/` (`/private`) anchors it to the site directory.

Endpoints for common providers:

| Provider | `endpoint` | `region` |
| -------- | ---------- | -------- |
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` | `auto` |
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` | the bucket's region, e.g. `us-west-004` |
| AWS S3 | leave out | the bucket's region |
| MinIO, Wasabi, others | the HTTPS endpoint of the service | its region |

## How sites are discovered

`clpctl` has no command that lists sites; the documented list commands are `user:list` and
`vhost-templates:list` ([root user commands](https://www.cloudpanel.io/docs/v2/cloudpanel-cli/root-user-commands/)).
So discovery works from the files CloudPanel maintains:

1. Every vhost in `nginx_dir` whose `root` is below `/home/<site-user>/htdocs/<dir>` becomes a site. The domain is
   the vhost file name (`<domain>.conf`) or its first `server_name`. The site directory is
   `/home/<site-user>/htdocs/<dir>`, so a Laravel `root .../public` still backs up the whole application.
2. Every other directory in `/home/*/htdocs/` becomes a site too (static sites without a vhost, leftovers).
3. `wp-config.php` or `.env` in the site gives the database name and credentials. `[[site]] databases = [...]`
   overrides or extends the list.

`clpctl` is used where it has a documented command: `db:export` / `db:import` (with `db_method = "clpctl"`) and
`db:show:master-credentials` for the fallback credentials.

## Storage layout and manifest

```text
<prefix>/<host>/<domain>/<UTC timestamp>/files.tar.gz[.age|.gpg]
                                        vhost.conf[.age|.gpg]
                                        db-<database>.sql.gz[.age|.gpg]
                                        manifest.json
```

For example `cloudpanel/vps1/shop.example.com/20261114T023000Z/manifest.json`. The manifest is written last, and
looks like this (shortened):

```json
{
  "tool": "cloudpanel-backup",
  "format_version": 1,
  "domain": "shop.example.com",
  "host": "vps1",
  "timestamp": "20261114T023000Z",
  "encryption": "age",
  "site": {"site_dir": "/home/shop/htdocs/shop.example.com", "user": "shop", "kind": "wordpress",
           "databases": ["shop_wp"]},
  "entries": [
    {"name": "files.tar.gz.age", "kind": "files", "size": 432611328, "sha256": "...",
     "plain_size": 432600110, "plain_sha256": "...", "details": {"files": 18204, "excluded": 7}},
    {"name": "db-shop_wp.sql.gz.age", "kind": "database", "database": "shop_wp", "size": 9150421,
     "sha256": "...", "plain_size": 9150300, "plain_sha256": "...", "details": {"method": "mariadb-dump"}}
  ]
}
```

The manifest holds no secrets (database names and paths only) and is not encrypted, so `list` and quick `verify`
work without the private key.

## Retention

After each successful backup of a site, the policy keeps:

- the newest `min_keep` backups,
- the newest backup of each of the last `daily` days that have a backup,
- the newest backup of each of the last `weekly` ISO weeks that have a backup,
- the newest backup of each of the last `monthly` months that have a backup.

Everything else is deleted, manifest first, so an interrupted delete leaves an incomplete backup rather than a
broken complete one. Counting "periods that have a backup" means a server that was down for a week still keeps
seven daily copies. With the defaults (7/4/6) a site keeps about 15 backups, because the periods overlap. `prune --dry-run` shows what would go.

## Restore

```bash
# What would happen (nothing is downloaded or changed)
cloudpanel-backup restore shop.example.com --dry-run

# Restore the latest backup in place: files are verified, extracted next to the site, the current directory is
# moved to shop.example.com.before-restore-<time>, then the database is imported
cloudpanel-backup restore shop.example.com --force --identity /root/age-identity.txt

# Restore a specific backup into a scratch directory and database (a restore drill)
cloudpanel-backup restore shop.example.com --timestamp 20261101T023000Z \
  --target /var/tmp/drill --db-map shop_wp=drill_wp --no-chown

# Move a site to a new server: restore what the old server ("vps1") uploaded
cloudpanel-backup restore shop.example.com --from-host vps1
```

Create the site in CloudPanel first (so the site user, vhost and database exist), then restore into it. The
database import replaces the tables in the dump; it does not create the database or the database user. The
vhost from the backup is saved next to the site directory for reference and is never activated automatically.

## Scheduling

```bash
cloudpanel-backup schedule                   # print the units
cloudpanel-backup schedule --install         # write /etc/systemd/system/cloudpanel-backup.{service,timer}
systemctl daemon-reload && systemctl enable --now cloudpanel-backup.timer
systemctl list-timers cloudpanel-backup.timer

cloudpanel-backup schedule --cron --cron-schedule "15 3 * * *" > /etc/cron.d/cloudpanel-backup
```

The service is a `Type=oneshot` unit with `Nice=10`, `IOSchedulingClass=idle`, `UMask=0077` and a 6 hour timeout;
the timer runs daily at 02:30 with a random delay of up to 15 minutes and catches up after downtime
(`Persistent=true`). `--on-calendar` changes the time, `--verify-after` adds a `verify` run.

## Run lock

`backup`, `restore` and `prune` take an exclusive lock before they touch the staging directory or the storage, so
two runs never build the same staging folder, upload into the same backup set, or prune a backup that is being
restored. Read-only commands (`list`, `verify`, `discover`) and `--dry-run` runs do not take it.

The lock is an advisory `flock` on `<staging_dir>/cloudpanel-backup.lock` (mode 0600; set `[backup] lock_file` to
move it, for example to `/run/cloudpanel-backup.lock`). The kernel drops it when the process exits, even after a
crash or `kill -9`, so a leftover file never blocks later runs. While held, the file names the command, PID, host
and start time, and a refused run prints them:

```text
error: another cloudpanel-backup run holds /var/backups/cloudpanel-backup/cloudpanel-backup.lock
(backup, pid 4121, on vps1, since 2026-11-17T02:31:09Z)
```

By default a second run gives up at once with exit code `3`. `[backup] lock_timeout = 600` (or `--lock-timeout
600`) makes it wait up to ten minutes first. `--no-lock` skips the lock; use it only when you are sure nothing else
is running. With `schedule --cron --verify-after`, a refused backup also skips the verify run.

## Notifications

```toml
[notify]
on = "failure"                          # "always", "failure" or "never"
telegram_bot_token_env = "CPB_TELEGRAM_TOKEN"
telegram_chat_id = "123456789"
webhook_url_env = "CPB_WEBHOOK_URL"     # or webhook_url = "https://..."
email_to = ["ops@example.com"]
email_from = "backups@example.com"
smtp_host = "smtp.example.com"
smtp_port = 587                         # STARTTLS; smtp_ssl = true for port 465
smtp_user = "backups@example.com"
smtp_password_env = "CPB_SMTP_PASSWORD"
```

The webhook receives a JSON body with `source`, `action` (`backup` or `verify`), `ok`, `host`, `title`, `summary`,
`text` and `details` (one entry per site). Webhooks must use HTTPS. A failing notification is logged and never
fails the backup. `cloudpanel-backup test-notify` checks the setup.

## Heartbeat monitoring

Notifications only arrive when a run finishes. If the timer is disabled, the server is down or a run hangs, nothing
is sent at all. A heartbeat monitor ([Healthchecks.io](https://healthchecks.io), a self-hosted Healthchecks, or an
Uptime Kuma push monitor) catches that: it expects a ping on a schedule and alerts when one is late.

```toml
[notify]
heartbeat_url_env = "CPB_HEARTBEAT_URL"
```

```bash
# /etc/cloudpanel-backup/env (mode 0600): the ping URL includes the check's secret id
CPB_HEARTBEAT_URL=https://hc-ping.com/<your-check-uuid>
```

`backup` sends `POST <url>/start` when it begins (after taking the run lock), `POST <url>` when every site
succeeded and `POST <url>/fail` otherwise, or when the run stops with an error. The body is the plain-text run
summary (the first 10 kB), so it shows up in the monitor's event log. The URL must use HTTPS and is never logged.
A failed ping is logged as a warning and does not fail the backup. Dry runs and `--no-notify` runs send no pings,
and neither does a run refused by the run lock (the run holding the lock pings when it finishes).
`cloudpanel-backup test-notify` sends one success ping to check the URL.

## Examples

`examples/` contains:

- `demo/`: a tiny fake server (home directory tree and nginx vhost) with `config.toml`, for an offline run from a
  checkout: `cloudpanel-backup --config examples/demo/config.toml backup`
- `config-r2.toml`: Cloudflare R2 with age encryption and Telegram alerts
- `config-b2.toml`: Backblaze B2 with GnuPG and a webhook
- `config-aws-boto3.toml`: AWS S3 through boto3 with email alerts
- `config-rsync.toml`: rsync over SSH, `clpctl db:export`, per-site overrides
- `env.example`: the environment file
- `restore-drill.sh`: a monthly restore test into a scratch directory and database

## Testing

```console
$ python -m venv .venv && . .venv/bin/activate
$ pip install -e ".[dev]"
$ pytest
$ ruff check . && ruff format --check .
```

The tests never touch a real server or the network. A fake command runner stands in for `clpctl`, `mariadb-dump`,
`mariadb`, `age`, `gpg`, `rsync` and `ssh`; a fake S3 endpoint keeps objects in memory, paginates listings,
implements multipart upload and checks the SigV4 signature and payload hash of every request; the signer itself is
checked against the published AWS Signature V4 examples. Site trees are built in a temporary directory. CI runs
the suite on Python 3.10 to 3.13, plus an end-to-end backup, verify and restore of the demo server and a Docker
build.

## Security notes

- Run it as root (it has to read every site and dump every database), and keep `/etc/cloudpanel-backup/env`
  at mode 0600. The config can be world-readable; it contains no secrets.
- Database passwords go to `mariadb-dump` through a temporary option file (mode 0600, removed afterwards), never
  on the command line. Credentials are not logged; `DbCredentials` hides the password in its `repr`.
- Use a storage key that can only reach the backup bucket. With R2 and B2 you can create keys limited to one
  bucket. If an attacker gets root on the server, they get that key too: enable bucket versioning or object lock
  if you need protection against deletion.
- With encryption, only the public key is on the server. Keep the age identity or GnuPG private key offline and
  test a restore with it.
- Restore checks the SHA-256 of every file before using it, refuses archive members with absolute paths, `..`,
  device files, links that escape the target or members below a symlink, and drops setuid/setgid bits.
- Staging directories are created with mode 0700, uploaded local files with 0600, and the systemd unit uses
  `UMask=0077`.
- The S3 client refuses plain-HTTP endpoints except `localhost`/`127.0.0.1` (for a local MinIO).
- The run lock file is opened with `O_NOFOLLOW`, so a symlink planted in its place is refused instead of followed.

## Limitations

- Linux only, and built around CloudPanel's layout (`/home/<user>/htdocs/<domain>`, vhosts in
  `/etc/nginx/sites-enabled`). Other layouts work through `home_root`, `nginx_dir` and `[[site]] path`.
- The output format of `clpctl db:show:master-credentials` is not documented; the parser accepts `Key: value`
  lines and console tables. If it fails, set `[database] user` and `password_env`.
- Databases are found through `wp-config.php` and `.env` files. Other applications need `[[site]] databases`.
  PostgreSQL is not supported.
- Files are archived as they are read; a site that changes during the backup can produce an archive that mixes
  old and new files (the database dump itself is consistent thanks to `--single-transaction` on InnoDB tables;
  MyISAM tables are not locked).
- Each archive is built in the staging directory before upload, so it needs free space for the largest site.
- Multipart uploads read one part (default 64 MiB) into memory at a time. Every request carries the SHA-256 of
  its payload, which means each file is read once more for hashing.
- The rsync backend needs rsync 3.2.3 or newer (`--mkpath`) and uses `ssh ... rm` to delete old backups.
- Restore does not create CloudPanel sites, site users or databases; create them first (`clpctl site:add:*`,
  `clpctl db:add`).
- Incremental or deduplicated backups are out of scope; every run is a full backup of each site.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and
guidelines, [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards, and [SECURITY.md](SECURITY.md)
for reporting vulnerabilities.

## License

[MIT](LICENSE), Copyright (c) 2026 Saizul Amin.
