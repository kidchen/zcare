import base64
import os

import pytest

from clients.wecom_crypto import WeComCrypto, WeComCryptoError

TOKEN = "QDG6eK"
AES_KEY = base64.b64encode(os.urandom(32)).decode().rstrip("=")  # 43 chars, like the console's
CORP_ID = "wwcorp123"


@pytest.fixture
def crypto():
    return WeComCrypto(TOKEN, AES_KEY, CORP_ID)


def test_roundtrip_and_verify_url(crypto):
    echostr = crypto.encrypt("1616140317555161061")
    sig = crypto.signature("1409659589", "263014780", echostr)
    assert crypto.verify_url(sig, "1409659589", "263014780", echostr) == "1616140317555161061"


def test_known_signature_vector():
    # sha1("".join(sorted([token, timestamp, nonce, encrypt]))) computed independently
    import hashlib
    c = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    parts = sorted([TOKEN, "1", "2", "abc"])
    assert c.signature("1", "2", "abc") == hashlib.sha1("".join(parts).encode()).hexdigest()


def test_bad_signature_rejected(crypto):
    echostr = crypto.encrypt("hello")
    with pytest.raises(WeComCryptoError, match="signature"):
        crypto.verify_url("0" * 40, "1", "2", echostr)


def test_wrong_corp_id_rejected(crypto):
    other = WeComCrypto(TOKEN, AES_KEY, "someone-else")
    echostr = other.encrypt("hello")
    with pytest.raises(WeComCryptoError, match="receive_id"):
        crypto.decrypt(echostr)


def test_invalid_key_length():
    with pytest.raises(ValueError):
        WeComCrypto(TOKEN, "short", CORP_ID)


def test_webhook_route_echoes_plaintext(monkeypatch):
    from fastapi.testclient import TestClient

    import config
    import main

    monkeypatch.setenv("WECOM_CALLBACK_TOKEN", TOKEN)
    monkeypatch.setenv("WECOM_ENCODING_AES_KEY", AES_KEY)
    monkeypatch.setenv("WECOM_CORP_ID", CORP_ID)
    for fn in (config.get_settings, main.get_services, main.get_wecom_crypto):
        fn.cache_clear()
    try:
        crypto = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
        echostr = crypto.encrypt("verify-me-123")
        sig = crypto.signature("1700000000", "nonce1", echostr)
        with TestClient(main.app) as client:
            ok = client.get("/wecom/webhook", params={
                "msg_signature": sig, "timestamp": "1700000000", "nonce": "nonce1", "echostr": echostr})
            assert ok.status_code == 200
            assert ok.text == "verify-me-123"  # bare text, not JSON-quoted
            assert ok.headers["content-type"].startswith("text/plain")
            # '+' delivered unencoded arrives as a space — still accepted
            if "+" in echostr:
                raw = client.get(f"/wecom/webhook?msg_signature={sig}&timestamp=1700000000"
                                 f"&nonce=nonce1&echostr={echostr}")
                assert raw.text == "verify-me-123"
            bad = client.get("/wecom/webhook", params={
                "msg_signature": "f" * 40, "timestamp": "1700000000", "nonce": "nonce1", "echostr": echostr})
            assert bad.status_code == 403
    finally:
        for fn in (config.get_settings, main.get_services, main.get_wecom_crypto):
            fn.cache_clear()
