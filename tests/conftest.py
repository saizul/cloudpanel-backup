"""Fakes shared by the tests: a command runner, an S3 server and a CloudPanel-like directory tree.

Nothing here runs a real program or opens a network connection.
"""

from __future__ import annotations

import gzip
import shutil
import xml.sax.saxutils as sx
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest

from cloudpanel_backup.config import Config, parse_config
from cloudpanel_backup.http import HttpResponse
from cloudpanel_backup.runner import CommandError, CommandResult, RunOptions
from cloudpanel_backup.sigv4 import sign

ACCESS_KEY = "TESTACCESSKEY"
SECRET_KEY = "test-secret-key-not-real"

WP_CONFIG = """<?php
/** The name of the database for WordPress */
define( 'DB_NAME', 'shop_wp' );
define( 'DB_USER', 'shop_user' );
define( 'DB_PASSWORD', 'p@ss\\'word"x' );
define( 'DB_HOST', 'localhost' );
// define( 'DB_NAME', 'commented_out' );
$table_prefix = 'wpx_';
require_once ABSPATH . 'wp-settings.php';
"""

VHOST_TEMPLATE = """server {{
  listen 80;
  listen [::]:80;
  listen 443 quic;
  listen 443 ssl;
  ssl_certificate_key /etc/nginx/ssl-certificates/{first}.key;
  server_name {names};
  root {root};
  # comment with ; and {{ braces }}
  location ~ \\.php$ {{
    include fastcgi_params;
    fastcgi_pass 127.0.0.1:{port};
  }}
  location / {{
    try_files $uri $uri/ /index.php?$args;
  }}
}}
"""


class FakeRunner:
    """Pretends to be clpctl, mariadb-dump, mariadb, age, gpg, rsync, ssh and chown."""

    def __init__(self, remote_root: Path | None = None, programs: Sequence[str] | None = None) -> None:
        self.calls: list[tuple[list[str], RunOptions]] = []
        self.programs = (
            set(programs)
            if programs is not None
            else {"clpctl", "mariadb-dump", "mariadb", "age", "gpg", "rsync", "ssh", "chown"}
        )
        self.imports: dict[str, bytes] = {}
        self.remote_root = remote_root
        self.master_output = "+-----------+-----------------+\n| Host      | 127.0.0.1       |\n| Port      | 3306            |\n| User Name | root            |\n| Password  | master-secret   |\n+-----------+-----------------+\n"  # noqa: E501
        self.fail: dict[str, str] = {}
        self.cnf_contents: list[str] = []

    def which(self, program: str) -> str | None:
        return f"/usr/bin/{program}" if program in self.programs else None

    def commands(self, program: str) -> list[list[str]]:
        return [args for args, _ in self.calls if args[0] == program]

    def run(self, args: Sequence[str], options: RunOptions | None = None) -> CommandResult:
        args = list(args)
        opts = options or RunOptions()
        self.calls.append((args, opts))
        program = args[0]
        if program in self.fail:
            if opts.check:
                raise CommandError(args, 1, self.fail[program])
            return CommandResult(args, 1, "", self.fail[program])
        handler: Callable[[list[str], RunOptions], str] = getattr(
            self, "_" + program.replace("-", "_"), lambda a, o: ""
        )
        return CommandResult(args, 0, handler(args, opts) or "", "")

    # -- programs ------------------------------------------------------------------------------------------

    def _read_cnf(self, args: list[str]) -> None:
        for arg in args:
            if arg.startswith("--defaults-extra-file="):
                self.cnf_contents.append(Path(arg.split("=", 1)[1]).read_text())

    def _mariadb_dump(self, args: list[str], opts: RunOptions) -> str:
        self._read_cnf(args)
        db = args[-1]
        sql = f"-- dump of {db}\nCREATE TABLE t (id int);\nINSERT INTO t VALUES (1);\n".encode()
        assert opts.stdout_path is not None
        if opts.gzip_stdout:
            with gzip.open(opts.stdout_path, "wb") as fh:
                fh.write(sql)
        else:
            opts.stdout_path.write_bytes(sql)
        return ""

    _mysqldump = _mariadb_dump

    def _mariadb(self, args: list[str], opts: RunOptions) -> str:
        self._read_cnf(args)
        assert opts.stdin_path is not None
        raw = opts.stdin_path.read_bytes()
        data = gzip.decompress(raw) if opts.gunzip_stdin else raw
        self.imports[args[-1]] = data
        return ""

    def _clpctl(self, args: list[str], opts: RunOptions) -> str:
        sub = args[1]
        flags = dict(a[2:].split("=", 1) for a in args[2:] if a.startswith("--") and "=" in a)
        if sub == "db:show:master-credentials":
            return self.master_output
        if sub == "db:export":
            with gzip.open(flags["file"], "wb") as fh:
                fh.write(f"-- clpctl export of {flags['databaseName']}\n".encode())
        elif sub == "db:import":
            path = (opts.cwd or Path.cwd()) / flags["file"]
            self.imports[flags["databaseName"]] = gzip.decompress(Path(path).read_bytes())
        return ""

    def _age(self, args: list[str], opts: RunOptions) -> str:
        out = Path(args[args.index("--output") + 1])
        src = Path(args[-1])
        if "--encrypt" in args:
            out.write_bytes(b"FAKEAGE\n" + src.read_bytes()[::-1])
        else:
            data = src.read_bytes()
            assert data.startswith(b"FAKEAGE\n")
            out.write_bytes(data[len(b"FAKEAGE\n") :][::-1])
        return ""

    def _gpg(self, args: list[str], opts: RunOptions) -> str:
        out = Path(args[args.index("--output") + 1])
        src = Path(args[-1])
        if "--encrypt" in args:
            out.write_bytes(b"FAKEGPG\n" + src.read_bytes())
        else:
            out.write_bytes(src.read_bytes()[len(b"FAKEGPG\n") :])
        return ""

    def _remote_path(self, spec: str) -> Path:
        assert self.remote_root is not None
        _, _, path = spec.partition(":")
        return self.remote_root / path.lstrip("/")

    def _rsync(self, args: list[str], opts: RunOptions) -> str:
        positional = [a for a in args[1:] if not a.startswith("-")]
        # drop the value of -e
        if "-e" in args:
            positional.remove(args[args.index("-e") + 1])
        if "--list-only" in args:
            root = self._remote_path(positional[-1])
            if not root.exists():
                raise CommandError(args, 23, "change_dir failed: No such file or directory")
            lines = ["drwx------          4,096 2026/11/14 02:30:00 ."]
            for path in sorted(root.rglob("*")):
                rel = path.relative_to(root).as_posix()
                if path.is_dir():
                    lines.append(f"drwx------          4,096 2026/11/14 02:30:00 {rel}")
                else:
                    lines.append(f"-rw-------  {path.stat().st_size:>13,} 2026/11/14 02:30:00 {rel}")
            return "\n".join(lines) + "\n"
        src, dst = positional[-2], positional[-1]
        if ":" in dst:
            target = self._remote_path(dst)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        else:
            source = self._remote_path(src)
            if not source.exists():
                raise CommandError(args, 23, "No such file")
            shutil.copyfile(source, dst)
        return ""

    def _ssh(self, args: list[str], opts: RunOptions) -> str:
        if "rm" in args:
            path = args[-1].strip("'")
            assert self.remote_root is not None
            (self.remote_root / path.lstrip("/")).unlink(missing_ok=True)
        return ""


class FakeS3:
    """An in-memory S3 endpoint speaking enough of the API for the storage backend. It checks every signature."""

    def __init__(self, page_size: int = 2) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.requests: list[tuple[str, str]] = []
        self.page_size = page_size
        self.fail_next: list[int] = []

    def _check_signature(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> None:
        import hashlib

        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        payload_hash = headers["x-amz-content-sha256"]
        assert payload_hash == hashlib.sha256(body).hexdigest(), "payload hash does not match the body"
        moment = datetime.strptime(headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        base = {k: v for k, v in headers.items() if k not in ("Authorization", "x-amz-date", "x-amz-content-sha256")}
        region = headers["Authorization"].split("/")[2]
        expected = sign(
            method,
            f"{parts.scheme}://{parts.netloc}{parts.path}",
            query,
            base,
            payload_hash,
            ACCESS_KEY,
            SECRET_KEY,
            region,
            now=moment,
        )
        assert headers["Authorization"] == expected["Authorization"], "bad signature"

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | BinaryIO | None = None,
        sink: Path | None = None,
    ) -> HttpResponse:
        data = body.read() if hasattr(body, "read") else (body or b"")  # type: ignore[union-attr]
        if "content-length" in headers:
            assert int(headers["content-length"]) == len(data)
        self._check_signature(method, url, headers, data)
        self.requests.append((method, url))
        if self.fail_next:
            return HttpResponse(self.fail_next.pop(0), {}, b"<Error><Code>InternalError</Code></Error>")
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        path = unquote(parts.path).lstrip("/")
        bucket, _, key = path.partition("/")
        assert bucket == "backups"
        if method == "GET" and query.get("list-type") == "2":
            return self._list(query)
        if method == "PUT" and "uploadId" in query:
            self.uploads[query["uploadId"]][int(query["partNumber"])] = data
            return HttpResponse(200, {"etag": f'"etag-{query["partNumber"]}"'})
        if method == "PUT":
            self.objects[key] = data
            return HttpResponse(200, {"etag": '"x"'})
        if method == "POST" and "uploads" in query:
            upload_id = f"up{len(self.uploads) + 1}"
            self.uploads[upload_id] = {}
            return HttpResponse(
                200,
                {},
                f"<InitiateMultipartUploadResult><UploadId>{upload_id}</UploadId>"
                f"</InitiateMultipartUploadResult>".encode(),
            )
        if method == "POST" and "uploadId" in query:
            parts_ = self.uploads.pop(query["uploadId"])
            self.objects[key] = b"".join(parts_[n] for n in sorted(parts_))
            return HttpResponse(200, {}, b"<CompleteMultipartUploadResult/>")
        if method == "DELETE" and "uploadId" in query:
            self.uploads.pop(query["uploadId"], None)
            return HttpResponse(204)
        if method == "DELETE":
            self.objects.pop(key, None)
            return HttpResponse(204)
        if key not in self.objects:
            return HttpResponse(404, {}, b"<Error><Code>NoSuchKey</Code><Message>gone</Message></Error>")
        if method == "HEAD":
            return HttpResponse(200, {"content-length": str(len(self.objects[key]))})
        if method == "GET":
            if sink is not None:
                sink.write_bytes(self.objects[key])
                return HttpResponse(200)
            return HttpResponse(200, {}, self.objects[key])
        return HttpResponse(405)

    def _list(self, query: dict[str, str]) -> HttpResponse:
        keys = sorted(k for k in self.objects if k.startswith(query.get("prefix", "")))
        start = int(query.get("continuation-token", "0") or 0)
        page = keys[start : start + self.page_size]
        truncated = start + self.page_size < len(keys)
        items = "".join(
            f"<Contents><Key>{sx.escape(k)}</Key><Size>{len(self.objects[k])}</Size></Contents>" for k in page
        )
        token = f"<NextContinuationToken>{start + self.page_size}</NextContinuationToken>" if truncated else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{items}{token}</ListBucketResult>"
        )
        return HttpResponse(200, {}, xml.encode())


def make_site(home: Path, user: str, domain: str, wordpress: bool = True) -> Path:
    site = home / user / "htdocs" / domain
    (site / "wp-content" / "uploads" / "2026").mkdir(parents=True)
    (site / "wp-content" / "cache" / "page").mkdir(parents=True)
    (site / "node_modules" / "left-pad").mkdir(parents=True)
    (site / "index.php").write_text("<?php echo 'hello';\n")
    (site / "wp-content" / "uploads" / "2026" / "photo.jpg").write_bytes(b"\xff\xd8" + b"x" * 2000)
    (site / "wp-content" / "cache" / "page" / "index.html").write_text("cached")
    (site / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1;")
    (site / "debug.log").write_text("noise")
    if wordpress:
        (site / "wp-config.php").write_text(WP_CONFIG)
    (home / user / "logs").mkdir(exist_ok=True)
    return site


@pytest.fixture
def server(tmp_path: Path) -> dict[str, Path]:
    """A CloudPanel-like server: /home with two sites and CloudPanel's own clp user, plus nginx vhosts."""
    home = tmp_path / "home"
    nginx = tmp_path / "nginx"
    nginx.mkdir()
    shop = make_site(home, "shop", "shop.example.com")
    blog = make_site(home, "blog", "www.blog.example.org", wordpress=False)
    (home / "clp" / "htdocs" / "app").mkdir(parents=True)
    (nginx / "shop.example.com.conf").write_text(
        VHOST_TEMPLATE.format(
            names="shop.example.com www.shop.example.com", root=shop, port=17001, first="shop.example.com"
        )
    )
    return {"home": home, "nginx": nginx, "shop": shop, "blog": blog, "root": tmp_path}


def make_config(server: dict[str, Path], storage: dict | None = None, **sections: dict) -> Config:
    data: dict = {
        "backup": {
            "staging_dir": str(server["root"] / "staging"),
            "home_root": str(server["home"]),
            "nginx_dir": str(server["nginx"]),
            "host": "vps1",
        },
        "storage": storage or {"type": "local", "path": str(server["root"] / "store")},
    }
    for name, values in sections.items():
        data.setdefault(name, {}).update(values)
    return parse_config(data)


@pytest.fixture
def s3_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPB_S3_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("CPB_S3_SECRET_ACCESS_KEY", SECRET_KEY)
