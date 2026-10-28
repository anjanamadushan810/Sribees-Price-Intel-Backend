"""Web Bot Auth: sign every outgoing request so shops can tell it is us
(HTTP Message Signatures, RFC 9421, as profiled by
draft-meunier-web-bot-auth-architecture; SPAR's agents.md asks for it).

We sign ("@authority" "signature-agent") with an Ed25519 key. The public key is
published at {SIGNATURE_AGENT}/.well-known/http-message-signatures-directory
(app.main), itself signed, so a shop can fetch and check it.
"""

import base64
import hashlib
import json
import secrets
import time
from functools import lru_cache

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.config import get_settings

DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_TYPE = "application/http-message-signatures-directory+json"
LIFETIME_SECONDS = 60


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@lru_cache
def _key() -> Ed25519PrivateKey | None:
    """WEB_BOT_AUTH_KEY = the 32-byte raw private key, base64. Empty = signing off."""
    raw = get_settings().web_bot_auth_key
    if not raw:
        return None
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw))


def public_jwk(key: Ed25519PrivateKey) -> dict:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    x = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(x)}


def thumbprint(jwk: dict) -> str:
    """RFC 7638: SHA-256 over the required members in lexical order, no spaces."""
    canonical = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]},
                           separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def _sign(key: Ed25519PrivateKey, lines: list[str], params: str) -> str:
    base = "\n".join([*lines, f'"@signature-params": {params}'])
    return base64.b64encode(key.sign(base.encode())).decode()


def request_headers(authority: str, now: float | None = None) -> dict[str, str]:
    """Headers for one request to `authority` (host[:port]); {} when signing is off."""
    key, agent = _key(), get_settings().signature_agent
    if key is None or not agent:
        return {}
    created = int(now if now is not None else time.time())
    agent_header = f'"{agent}"'
    params = (f'("@authority" "signature-agent");created={created}'
              f';keyid="{thumbprint(public_jwk(key))}";alg="ed25519"'
              f';expires={created + LIFETIME_SECONDS};nonce="{_b64url(secrets.token_bytes(32))}"'
              f';tag="web-bot-auth"')
    signature = _sign(key, [f'"@authority": {authority.lower()}',
                            f'"signature-agent": {agent_header}'], params)
    return {"Signature-Agent": agent_header, "Signature-Input": f"sig1={params}",
            "Signature": f"sig1=:{signature}:"}


def directory(authority: str, now: float | None = None) -> tuple[dict, dict[str, str]] | None:
    """The key directory body and its signature headers; None when signing is off."""
    key = _key()
    if key is None:
        return None
    jwk = public_jwk(key)
    created = int(now if now is not None else time.time())
    params = (f'("@authority";req);alg="ed25519";keyid="{thumbprint(jwk)}"'
              f';nonce="{_b64url(secrets.token_bytes(32))}"'
              f';tag="http-message-signatures-directory"'
              f';created={created};expires={created + 300}')
    signature = _sign(key, [f'"@authority";req: {authority.lower()}'], params)
    return {"keys": [jwk]}, {"Signature-Input": f"sig1={params}",
                             "Signature": f"sig1=:{signature}:"}
