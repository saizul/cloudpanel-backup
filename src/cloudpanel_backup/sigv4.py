"""AWS Signature Version 4 for S3-compatible APIs (AWS S3, Cloudflare R2, Backblaze B2, MinIO, Wasabi, ...).

Implements the header-based signing described in the AWS "Signature Version 4 signing process" documentation:
canonical request -> string to sign -> HMAC-SHA256 key derivation (date, region, service, "aws4_request").
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
ALGORITHM = "AWS4-HMAC-SHA256"


def uri_encode(value: str, *, keep_slash: bool = False) -> str:
    return quote(value, safe="-_.~/" if keep_slash else "-_.~")


def canonical_query(query: Mapping[str, str]) -> str:
    pairs = sorted((uri_encode(k), uri_encode(v)) for k, v in query.items())
    return "&".join(f"{k}={v}" for k, v in pairs)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    k_date = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def canonical_request(
    method: str, path: str, query: Mapping[str, str], headers: Mapping[str, str], payload_hash: str
) -> tuple[str, str]:
    """Return (canonical request, signed header list). ``path`` must already be URI-encoded."""
    normalized = {k.lower().strip(): " ".join(str(v).strip().split()) for k, v in headers.items()}
    names = sorted(normalized)
    canonical_headers = "".join(f"{name}:{normalized[name]}\n" for name in names)
    signed = ";".join(names)
    request = "\n".join([method.upper(), path or "/", canonical_query(query), canonical_headers, signed, payload_hash])
    return request, signed


def sign(
    method: str,
    url: str,
    query: Mapping[str, str],
    headers: Mapping[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    now: datetime | None = None,
) -> dict[str, str]:
    """Return the headers to send: the input headers plus host, x-amz-date, x-amz-content-sha256, Authorization.

    ``url`` is the request URL without the query string; its path must already be URI-encoded.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    parts = urlsplit(url)
    out = dict(headers)
    lower = {k.lower() for k in out}
    if "host" not in lower:
        out["host"] = parts.netloc
    out["x-amz-date"] = amz_date
    out["x-amz-content-sha256"] = payload_hash
    request, signed = canonical_request(method, parts.path, query, out, payload_hash)
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(request.encode("utf-8")).hexdigest()])
    signature = hmac.new(
        signing_key(secret_key, date, region, service), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    out["Authorization"] = f"{ALGORITHM} Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    return out
