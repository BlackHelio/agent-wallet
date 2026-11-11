"""HTTP Digest auth for the signer's RPC.

`monero-wallet-rpc` protects its RPC with HTTP Digest (epee's implementation), not Basic auth, and
two details of epee's implementation matter:

* Python's `HTTPDigestAuthHandler` does not interoperate with it — the retry ends in
  "digest auth failed".
* The nonce is stored in epee's **per-connection** context, so the challenge request and the
  authenticated retry must travel over the *same* TCP connection. A retry on a fresh connection is
  rejected even with a correct response hash.

So this does the handshake by hand: one connection, unauthenticated probe, read the challenge,
compute MD5 digest, resend on the same socket. Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import os
import re
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

_CHALLENGE = re.compile(r'(\w+)\s*=\s*"?([^",]*)"?')


def _parse_challenge(header: str) -> Dict[str, str]:
    body = header.split(" ", 1)[1] if " " in header else header
    return {key.lower(): value for key, value in _CHALLENGE.findall(body)}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def post(url: str, body: bytes, user: str = "", password: str = "",
         timeout: float = 30.0, headers: Optional[Dict[str, str]] = None) -> Tuple[int, bytes]:
    """POST `body` to `url`, answering a Digest challenge when one comes back.

    Returns (status, body). Raises OSError/HTTPException only when the socket itself fails.
    """
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    base_headers = {"Content-Type": "application/json"}
    if headers:
        base_headers.update(headers)

    connection: http.client.HTTPConnection
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        connection = http.client.HTTPConnection(host, port, timeout=timeout)

    try:
        connection.request("POST", path, body=body, headers=base_headers)
        response = connection.getresponse()
        payload = response.read()
        if response.status != 401 or not user:
            return response.status, payload

        challenge = response.getheader("WWW-Authenticate") or ""
        if "digest" not in challenge.lower():
            return response.status, payload
        fields = _parse_challenge(challenge)
        if not fields.get("nonce"):
            return response.status, payload

        realm = fields.get("realm", "")
        nonce = fields["nonce"]
        qop = fields.get("qop", "")
        opaque = fields.get("opaque", "")
        algorithm = fields.get("algorithm", "MD5")
        nc = "00000001"
        cnonce = base64.b16encode(os.urandom(8)).decode()

        ha1 = _md5(f"{user}:{realm}:{password}")
        if algorithm.lower() == "md5-sess":
            ha1 = _md5(f"{ha1}:{nonce}:{cnonce}")
        ha2 = _md5(f"POST:{path}")
        if "auth" in qop:
            digest = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        else:
            digest = _md5(f"{ha1}:{nonce}:{ha2}")

        pieces = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"',
                  f'uri="{path}"', f'cnonce="{cnonce}"', f"nc={nc}"]
        if qop:
            pieces.append("qop=auth")
        pieces += [f'response="{digest}"', f"algorithm={algorithm}"]
        if opaque:
            pieces.append(f'opaque="{opaque}"')

        authorised = dict(base_headers)
        authorised["Authorization"] = "Digest " + ", ".join(pieces)
        # Same connection: epee keeps the nonce in its per-connection context.
        connection.request("POST", path, body=body, headers=authorised)
        response = connection.getresponse()
        payload = response.read()
        return response.status, payload
    finally:
        connection.close()
