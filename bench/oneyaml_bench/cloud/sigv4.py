"""Presigned S3 URLs, signed locally with the standard library.

The instances get no credentials of their own: they download their inputs and upload their
results and logs through URLs that this module signs for one object and one method, valid
only until the run's deadline. The caller supplies short-lived credentials, so the URLs stop
working when the run is over even if one leaks.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
from urllib.parse import quote


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _enc(s: str, keep: str = "") -> str:
    return quote(s, safe="-_.~" + keep)


def bucket_host(bucket: str, region: str) -> str:
    return f"{bucket}.s3.{region}.amazonaws.com"


def presign(method: str, host: str, key: str, region: str, access_key: str, secret_key: str,
            token: str | None, expires: int, now: dt.datetime | None = None) -> str:
    """A presigned URL (signature version 4, query string form) for one method on one object."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    scope = f"{day}/{region}/s3/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    }
    if token:
        query["X-Amz-Security-Token"] = token
    canon_query = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(query.items()))
    path = "/" + _enc(key, "/")
    canon = "\n".join([method, path, canon_query, f"host:{host}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = _hmac(("AWS4" + secret_key).encode(), day)
    for part in (region, "s3", "aws4_request"):
        k = _hmac(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}{path}?{canon_query}&X-Amz-Signature={sig}"
