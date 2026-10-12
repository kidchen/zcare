"""WeCom callback message crypto (企业微信 回调 加解密方案).

Docs: https://developer.work.weixin.qq.com/document/path/90968

- Signature: sha1 of the lexicographically sorted [token, timestamp, nonce, encrypted].
- Cipher:    AES-256-CBC, key = base64(EncodingAESKey + "="), IV = key[:16],
             PKCS#7 padding with a 32-byte block size.
- Plaintext: random(16) | msg_len (4 bytes, big-endian) | msg | receive_id (CorpID).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_SIZE = 32


class WeComCryptoError(ValueError):
    """Bad signature, malformed ciphertext, or receive_id mismatch."""


def _pkcs7_pad(data: bytes) -> bytes:
    pad = BLOCK_SIZE - len(data) % BLOCK_SIZE
    return data + bytes([pad]) * pad


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise WeComCryptoError("empty plaintext")
    pad = data[-1]
    if not 1 <= pad <= BLOCK_SIZE or data[-pad:] != bytes([pad]) * pad:
        raise WeComCryptoError("bad padding")
    return data[:-pad]


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        if len(encoding_aes_key) != 43:
            raise ValueError("EncodingAESKey must be 43 characters")
        if not token:
            raise ValueError("callback Token is required")
        try:
            self._key = base64.b64decode(encoding_aes_key + "=")
        except binascii.Error as exc:
            raise ValueError("EncodingAESKey is not valid base64") from exc
        if len(self._key) != 32:
            raise ValueError("EncodingAESKey must decode to 32 bytes")
        self._token = token
        self._receive_id = receive_id

    def signature(self, timestamp: str, nonce: str, encrypted: str) -> str:
        parts = sorted([self._token, timestamp, nonce, encrypted])
        return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()

    def verify_signature(self, msg_signature: str, timestamp: str, nonce: str, encrypted: str) -> None:
        expected = self.signature(timestamp, nonce, encrypted)
        if not hmac.compare_digest(expected, msg_signature.lower()):
            raise WeComCryptoError("signature mismatch")

    def decrypt(self, encrypted: str) -> str:
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
        except binascii.Error as exc:
            raise WeComCryptoError("ciphertext is not base64") from exc
        if not ciphertext or len(ciphertext) % 16:
            raise WeComCryptoError("ciphertext length invalid")
        decryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).decryptor()
        plain = _pkcs7_unpad(decryptor.update(ciphertext) + decryptor.finalize())
        if len(plain) < 20:
            raise WeComCryptoError("plaintext too short")
        (msg_len,) = struct.unpack(">I", plain[16:20])
        msg, receive_id = plain[20:20 + msg_len], plain[20 + msg_len:]
        if len(msg) != msg_len:
            raise WeComCryptoError("declared length exceeds plaintext")
        if self._receive_id and receive_id.decode("utf-8", "replace") != self._receive_id:
            raise WeComCryptoError("receive_id (CorpID) mismatch")
        return msg.decode("utf-8")

    def encrypt(self, msg: str, random_prefix: bytes | None = None) -> str:
        """Inverse of decrypt — used for encrypted passive replies and in tests."""
        body = msg.encode("utf-8")
        plain = (random_prefix or os.urandom(16)) + struct.pack(">I", len(body)) + body \
            + self._receive_id.encode("utf-8")
        encryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).encryptor()
        return base64.b64encode(encryptor.update(_pkcs7_pad(plain)) + encryptor.finalize()).decode()

    def verify_url(self, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> str:
        """Handle the GET URL-verification handshake; returns the plaintext to echo back."""
        self.verify_signature(msg_signature, timestamp, nonce, echostr)
        return self.decrypt(echostr)
