from __future__ import annotations

from local_dev_proxy.services import _admin_api_address


def test_admin_api_address_parses_ipv4_url() -> None:
    assert _admin_api_address("http://127.0.0.1:2020") == "127.0.0.1:2020"


def test_admin_api_address_adds_default_https_port() -> None:
    assert _admin_api_address("https://example.test") == "example.test:443"


def test_admin_api_address_formats_ipv6() -> None:
    assert _admin_api_address("http://[::1]:2020") == "[::1]:2020"
