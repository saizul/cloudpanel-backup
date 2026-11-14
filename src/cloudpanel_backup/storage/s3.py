"""S3-compatible object storage with a built-in SigV4 client (no dependencies).

Tested against the S3 API as implemented by AWS, Cloudflare R2 (region ``auto``) and Backblaze B2's S3 endpoint.
Large files use multipart upload; every request carries the SHA-256 of its payload.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote

from cloudpanel_backup.config import StorageSettings, require_env
from cloudpanel_backup.http import HttpResponse, Transport, UrllibTransport
from cloudpanel_backup.sigv4 import EMPTY_SHA256, canonical_query, sign
from cloudpanel_backup.storage.base import ObjectInfo, StorageError
from cloudpanel_backup.util import sha256_file

log = logging.getLogger(__name__)
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
MIN_PART_SIZE = 5 * 1024 * 1024


def _find(elem: ET.Element, name: str) -> ET.Element | None:
    found = elem.find(NS + name)
    return found if found is not None else elem.find(name)


def _findall(elem: ET.Element, name: str) -> list[ET.Element]:
    return elem.findall(NS + name) or elem.findall(name)


def _text(elem: ET.Element | None) -> str:
    return elem.text or "" if elem is not None else ""


class S3Storage:
    def __init__(
        self,
        bucket: str,
        access_key: str,
        secret_key: str,
        endpoint: str | None = None,
        region: str = "auto",
        path_style: bool = True,
        transport: Transport | None = None,
        multipart_threshold: int = 64 * 1024 * 1024,
        storage_class: str | None = None,
        retries: int = 3,
        retry_delay: float = 0.5,
    ) -> None:
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.transport = transport or UrllibTransport()
        self.multipart_threshold = max(multipart_threshold, MIN_PART_SIZE)
        self.storage_class = storage_class
        self.retries = retries
        self.retry_delay = retry_delay
        if endpoint:
            self.endpoint = endpoint.rstrip("/")
            self.path_style = path_style
        else:  # AWS: virtual-hosted style on the regional endpoint
            region_name = "us-east-1" if region in ("", "auto") else region
            self.region = region_name
            self.endpoint = f"https://s3.{region_name}.amazonaws.com"
            self.path_style = False
        if not self.endpoint.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise StorageError("refusing a plain-HTTP S3 endpoint; use https://")

    @classmethod
    def from_settings(cls, s: StorageSettings, transport: Transport | None = None) -> S3Storage:
        assert s.bucket is not None
        return cls(
            bucket=s.bucket,
            access_key=require_env(s.access_key_env, "S3 access key id"),
            secret_key=require_env(s.secret_key_env, "S3 secret access key"),
            endpoint=s.endpoint,
            region=s.region,
            path_style=s.path_style,
            transport=transport,
            multipart_threshold=s.multipart_threshold_mb * 1024 * 1024,
            storage_class=s.storage_class,
        )

    def describe(self) -> str:
        return f"s3://{self.bucket} ({self.endpoint})"

    # -- low level -------------------------------------------------------------------------------------------

    def _url(self, key: str) -> str:
        path = quote(key, safe="-_.~/")
        if self.path_style:
            return f"{self.endpoint}/{self.bucket}/{path}" if key else f"{self.endpoint}/{self.bucket}"
        scheme, _, host = self.endpoint.partition("://")
        return f"{scheme}://{self.bucket}.{host}/{path}"

    def _request(
        self,
        method: str,
        key: str,
        query: Mapping[str, str] | None = None,
        body: bytes | None = None,
        body_file: Path | None = None,
        payload_hash: str | None = None,
        headers: Mapping[str, str] | None = None,
        sink: Path | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> HttpResponse:
        query = dict(query or {})
        url = self._url(key)
        if payload_hash is None:
            payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        last: HttpResponse | None = None
        for attempt in range(self.retries + 1):
            hdrs = dict(headers or {})
            if body is not None:
                hdrs["content-length"] = str(len(body))
            elif body_file is not None:
                hdrs["content-length"] = str(body_file.stat().st_size)
            signed = sign(method, url, query, hdrs, payload_hash, self.access_key, self.secret_key, self.region)
            full_url = url + ("?" + canonical_query(query) if query else "")
            if body_file is not None:
                with body_file.open("rb") as handle:
                    resp = self.transport.request(method, full_url, signed, handle, sink)
            else:
                resp = self.transport.request(method, full_url, signed, body, sink)
            if resp.status in ok:
                return resp
            last = resp
            if resp.status < 500 and resp.status != 429:
                break
            if attempt < self.retries:
                time.sleep(min(2**attempt, 10) * self.retry_delay)
        assert last is not None
        raise StorageError(self._error_message(method, key, last))

    @staticmethod
    def _error_message(method: str, key: str, resp: HttpResponse) -> str:
        code = message = ""
        try:
            root = ET.fromstring(resp.body)
            code, message = _text(_find(root, "Code")), _text(_find(root, "Message"))
        except ET.ParseError:
            pass
        detail = f"{code}: {message}" if code else resp.text()[:200]
        return f"S3 {method} {key or '/'} failed with HTTP {resp.status} {detail}".strip()

    # -- Storage interface -----------------------------------------------------------------------------------

    def put_bytes(self, data: bytes, key: str) -> None:
        headers = {"content-type": "application/json"} if key.endswith(".json") else {}
        if self.storage_class:
            headers["x-amz-storage-class"] = self.storage_class
        self._request("PUT", key, body=data, headers=headers)

    def put_file(self, local: Path, key: str, sha256: str | None = None) -> None:
        size = local.stat().st_size
        if size > self.multipart_threshold:
            self._multipart_upload(local, key, size)
            return
        headers = {"content-type": "application/octet-stream"}
        if self.storage_class:
            headers["x-amz-storage-class"] = self.storage_class
        self._request("PUT", key, body_file=local, payload_hash=sha256 or sha256_file(local), headers=headers)

    def _multipart_upload(self, local: Path, key: str, size: int) -> None:
        headers = {"content-type": "application/octet-stream"}
        if self.storage_class:
            headers["x-amz-storage-class"] = self.storage_class
        resp = self._request("POST", key, query={"uploads": ""}, headers=headers)
        upload_id = _text(_find(ET.fromstring(resp.body), "UploadId"))
        if not upload_id:
            raise StorageError("CreateMultipartUpload returned no UploadId")
        part_size = self.multipart_threshold
        parts: list[tuple[int, str]] = []
        try:
            with local.open("rb") as handle:
                number = 1
                while chunk := handle.read(part_size):
                    part = self._request(
                        "PUT", key, query={"partNumber": str(number), "uploadId": upload_id}, body=chunk
                    )
                    etag = part.headers.get("etag", "")
                    if not etag:
                        raise StorageError(f"UploadPart {number} returned no ETag")
                    parts.append((number, etag))
                    number += 1
            xml = io.StringIO()
            xml.write("<CompleteMultipartUpload>")
            for number, etag in parts:
                xml.write(f"<Part><PartNumber>{number}</PartNumber><ETag>{etag}</ETag></Part>")
            xml.write("</CompleteMultipartUpload>")
            done = self._request("POST", key, query={"uploadId": upload_id}, body=xml.getvalue().encode())
            # S3 can answer 200 with an <Error> body for CompleteMultipartUpload
            if b"<Error>" in done.body:
                raise StorageError(self._error_message("POST", key, HttpResponse(500, {}, done.body)))
            log.debug("uploaded %s in %d parts (%d bytes)", key, len(parts), size)
        except BaseException:
            try:
                self._request("DELETE", key, query={"uploadId": upload_id}, ok=(200, 204))
            except StorageError as exc:  # pragma: no cover - best effort
                log.warning("could not abort multipart upload %s: %s", upload_id, exc)
            raise

    def get_file(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._request("GET", key, sink=local)

    def get_bytes(self, key: str) -> bytes:
        return self._request("GET", key).body

    def list(self, prefix: str) -> list[ObjectInfo]:
        out: list[ObjectInfo] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix.strip("/") + "/" if prefix.strip("/") else ""}
            if token:
                query["continuation-token"] = token
            root = ET.fromstring(self._request("GET", "", query=query).body)
            for item in _findall(root, "Contents"):
                out.append(ObjectInfo(_text(_find(item, "Key")), int(_text(_find(item, "Size")) or 0)))
            if _text(_find(root, "IsTruncated")).lower() != "true":
                break
            token = _text(_find(root, "NextContinuationToken"))
            if not token:
                break
        return sorted(out, key=lambda o: o.key)

    def delete(self, key: str) -> None:
        self._request("DELETE", key, ok=(200, 204))

    def stat(self, key: str) -> ObjectInfo | None:
        try:
            resp = self._request("HEAD", key, ok=(200,))
        except StorageError as exc:
            if "HTTP 404" in str(exc) or "HTTP 403" in str(exc):
                return None
            raise
        return ObjectInfo(key, int(resp.headers.get("content-length", "0")))
