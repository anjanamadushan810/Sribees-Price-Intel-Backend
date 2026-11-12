"""The client address in audit rows comes from the last X-Forwarded-For hop (Caddy's)."""

from types import SimpleNamespace

from app.api.deps import client_ip


def _req(xff=None, peer="172.18.0.5"):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=peer))


def test_forged_first_hop_is_ignored():
    assert client_ip(_req("1.2.3.4, 203.0.113.9")) == "203.0.113.9"


def test_single_hop_and_no_header():
    assert client_ip(_req("203.0.113.9")) == "203.0.113.9"
    assert client_ip(_req()) == "172.18.0.5"
