import json

from ziniao_automation.ziniao import credentials
from ziniao_automation.ziniao.credentials import (
    _decode_blob,
    _serialise_secret,
    credential_matches,
)


def test_decode_utf8_credential_blob() -> None:
    assert _decode_blob(b"secret") == "secret"


def test_decode_utf16_credential_blob() -> None:
    assert _decode_blob("密钥".encode("utf-16-le")) == "密钥"


def test_credential_matches_checks_every_expected_field(monkeypatch) -> None:
    monkeypatch.setattr(
        credentials,
        "read_generic_credential",
        lambda _: {"company": "某公司", "username": "u", "password": "old"},
    )

    assert credential_matches(
        "ref", {"company": "某公司", "username": "u", "password": "old"}
    )
    assert not credential_matches(
        "ref", {"company": "某公司", "username": "u", "password": "new"}
    )


def test_serialise_mapping_round_trip() -> None:
    encoded = _serialise_secret({"password": "密钥", "username": "user"})
    decoded = json.loads(_decode_blob(encoded))
    assert decoded == {"password": "密钥", "username": "user"}
