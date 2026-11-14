"""A small parser for the nginx vhost files CloudPanel writes to /etc/nginx/sites-enabled/.

It is not a full nginx parser: it tokenizes the file (quotes, comments, braces), walks the ``server`` blocks and
pulls out the directives a backup needs: ``server_name``, ``root``, ``proxy_pass`` and the PHP-FPM upstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServerBlock:
    server_names: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)
    listens: list[str] = field(default_factory=list)
    proxy_passes: list[str] = field(default_factory=list)
    fastcgi_passes: list[str] = field(default_factory=list)


@dataclass
class Vhost:
    path: Path
    servers: list[ServerBlock]

    @property
    def server_names(self) -> list[str]:
        names: list[str] = []
        for server in self.servers:
            for name in server.server_names:
                if name not in names and name not in ("_", "localhost"):
                    names.append(name)
        return names

    @property
    def root(self) -> str | None:
        for server in self.servers:
            for root in server.roots:
                return root
        return None

    @property
    def proxy_pass(self) -> str | None:
        for server in self.servers:
            for target in server.proxy_passes:
                # CloudPanel's Varnish setups proxy to 127.0.0.1:8080 internally; prefer other targets.
                return target
        return None

    @property
    def uses_php(self) -> bool:
        return any(server.fastcgi_passes for server in self.servers)


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif ch in "{};":
            tokens.append(ch)
            i += 1
        elif ch in "\"'":
            quote = ch
            i += 1
            buf = []
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1
            tokens.append("".join(buf))
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in "{};":
                # ${var} inside a token
                if text[i] == "$" and i + 1 < n and text[i + 1] == "{":
                    close = text.find("}", i)
                    i = close + 1 if close != -1 else n
                    continue
                i += 1
            tokens.append(text[start:i])
    return tokens


def parse_vhost_text(text: str, path: Path | None = None) -> Vhost:
    tokens = tokenize(text)
    servers: list[ServerBlock] = []
    stack: list[str] = []  # block names
    current: list[ServerBlock] = []
    statement: list[str] = []
    for token in tokens:
        if token == "{":
            name = statement[0] if statement else ""
            stack.append(name)
            if name == "server" and "server" not in stack[:-1]:
                block = ServerBlock()
                servers.append(block)
                current.append(block)
            statement = []
        elif token == "}":
            if stack:
                name = stack.pop()
                if name == "server" and "server" not in stack and current:
                    current.pop()
            statement = []
        elif token == ";":
            if statement and current:
                block = current[-1]
                directive, args = statement[0], statement[1:]
                if directive == "server_name":
                    block.server_names.extend(a.lower() for a in args)
                elif directive == "root" and args:
                    block.roots.append(args[0])
                elif directive == "listen" and args:
                    block.listens.append(" ".join(args))
                elif directive == "proxy_pass" and args:
                    block.proxy_passes.append(args[0])
                elif directive == "fastcgi_pass" and args:
                    block.fastcgi_passes.append(args[0])
            statement = []
        else:
            statement.append(token)
    return Vhost(path=path or Path("<string>"), servers=servers)


def parse_vhost_file(path: Path) -> Vhost:
    return parse_vhost_text(path.read_text(encoding="utf-8", errors="replace"), path)


def load_vhosts(directory: Path) -> list[Vhost]:
    if not directory.is_dir():
        return []
    vhosts = []
    for path in sorted(directory.iterdir()):
        if path.is_file() or path.is_symlink():
            try:
                vhosts.append(parse_vhost_file(path))
            except OSError:
                continue
    return vhosts
