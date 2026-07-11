"""Minimal AWS Signature Version 4 signing (stdlib only, no boto3).

Anki bundles no AWS SDK, so requests against Bedrock are signed by hand.
Implements the canonical-request / string-to-sign flow from the AWS SigV4
specification for a single POST with a JSON body.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import urllib.parse


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "aws4_request")


def sign_request(
    *,
    method: str,
    url: str,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    session_token: str = "",
    body: bytes = b"",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the headers (incl. Authorization) for a SigV4-signed request."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    # Canonical URI: path segments must stay URI-encoded exactly as sent.
    canonical_uri = parsed.path or "/"

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    payload_hash = hashlib.sha256(body).hexdigest()

    headers: dict[str, str] = {
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    if extra_headers:
        for k, v in extra_headers.items():
            headers[k.lower()] = v

    signed_names = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in signed_names)
    signed_headers = ";".join(signed_names)

    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            parsed.query,  # canonical query string (already encoded)
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    # 'host' is set by the HTTP library itself; sending it twice is harmless,
    # but we drop it to keep requests' header handling clean.
    result = {k: v for k, v in headers.items() if k != "host"}
    return result
