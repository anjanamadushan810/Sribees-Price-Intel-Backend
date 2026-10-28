"""Web Bot Auth signatures verify with the published key, over the exact RFC 9421 base."""

import base64
import os
import re

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from app.adapters import botauth
from app.core.config import get_settings

NL = "\n"


@pytest.fixture
def signing(monkeypatch):
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    monkeypatch.setenv("WEB_BOT_AUTH_KEY", base64.b64encode(raw).decode())
    monkeypatch.setenv("SIGNATURE_AGENT", "https://devprice.sribees.com")
    get_settings.cache_clear()
    botauth._key.cache_clear()
    yield key
    monkeypatch.delenv("WEB_BOT_AUTH_KEY")
    monkeypatch.delenv("SIGNATURE_AGENT")
    get_settings.cache_clear()
    botauth._key.cache_clear()


def _public(jwk: dict) -> Ed25519PublicKey:
    x = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    return Ed25519PublicKey.from_public_bytes(x)


def _signature(header: str) -> bytes:
    return base64.b64decode(re.fullmatch(r"sig1=:(.+):", header).group(1))


def test_request_signature_verifies(signing):
    h = botauth.request_headers("spar2u.lk", now=1_800_000_000)
    assert h["Signature-Agent"] == '"https://devprice.sribees.com"'
    params = h["Signature-Input"].removeprefix("sig1=")
    assert 'tag="web-bot-auth"' in params and "created=1800000000" in params
    assert "expires=1800000060" in params
    jwk = botauth.public_jwk(signing)
    assert f'keyid="{botauth.thumbprint(jwk)}"' in params
    base = NL.join(['"@authority": spar2u.lk',
                    '"signature-agent": "https://devprice.sribees.com"',
                    f'"@signature-params": {params}'])
    _public(jwk).verify(_signature(h["Signature"]), base.encode())  # raises if wrong


def test_directory_is_signed_and_has_no_private_part(signing):
    body, h = botauth.directory("devprice.sribees.com")
    (jwk,) = body["keys"]
    assert set(jwk) == {"kty", "crv", "x"}
    params = h["Signature-Input"].removeprefix("sig1=")
    assert 'tag="http-message-signatures-directory"' in params
    base = NL.join(['"@authority";req: devprice.sribees.com', f'"@signature-params": {params}'])
    _public(jwk).verify(_signature(h["Signature"]), base.encode())


def test_rfc7638_thumbprint_example():
    # RFC 8037 appendix A.3: the Ed25519 example key's thumbprint.
    jwk = {"kty": "OKP", "crv": "Ed25519", "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"}
    assert botauth.thumbprint(jwk) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"


def test_unsigned_when_not_configured():
    assert os.environ.get("WEB_BOT_AUTH_KEY") is None
    assert botauth.request_headers("spar2u.lk") == {}
