"""Proxy parsing — security-sensitive, so it gets explicit coverage."""

import pytest

from doomscroller.config import parse_proxy

SECRET = "ee" + "ab" * 31


def test_empty_and_off_mean_direct():
    for raw in ("", "   ", "off", "OFF", "none", "direct"):
        assert parse_proxy(raw).kind == "none"


def test_parses_tme_mtproxy_link():
    p = parse_proxy(f"https://t.me/proxy?server=proxy.example.net&port=443&secret={SECRET}")
    assert p.kind == "mtproxy"
    assert p.host == "proxy.example.net"
    assert p.port == 443
    assert p.secret == SECRET


def test_parses_socks5_url():
    p = parse_proxy("socks5://user:pw@127.0.0.1:1080")
    assert p.kind == "socks5"
    assert (p.host, p.port, p.username, p.password) == ("127.0.0.1", 1080, "user", "pw")


def test_secret_never_appears_in_describe():
    p = parse_proxy(f"https://t.me/proxy?server=h.net&port=443&secret={SECRET}")
    assert SECRET not in p.describe()
    assert "secret" not in p.describe().lower()


def test_malformed_mtproxy_link_raises():
    with pytest.raises(RuntimeError):
        parse_proxy("https://t.me/proxy?server=h.net")  # no port/secret


def test_unknown_proxy_scheme_raises():
    with pytest.raises(RuntimeError):
        parse_proxy("http://not-a-proxy")
