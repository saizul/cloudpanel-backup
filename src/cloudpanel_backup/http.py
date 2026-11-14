"""A minimal HTTP client interface on top of urllib, injectable for tests."""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Protocol

from cloudpanel_backup._version import __version__

USER_AGENT = f"cloudpanel-backup/{__version__}"


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | BinaryIO | None = None,
        sink: Path | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Sends requests with urllib. With ``sink`` the body of a 2xx answer is streamed to that file."""

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | BinaryIO | None = None,
        sink: Path | None = None,
    ) -> HttpResponse:
        all_headers = {"User-Agent": USER_AGENT, **headers}
        req = urllib.request.Request(url, data=body, method=method, headers=all_headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                if sink is not None and 200 <= resp.status < 300:
                    with sink.open("wb") as out:
                        shutil.copyfileobj(resp, out, 1024 * 1024)
                    return HttpResponse(resp.status, resp_headers)
                return HttpResponse(resp.status, resp_headers, resp.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())
