"""WeCom callback XML: parse incoming envelopes/messages, build encrypted passive replies.

Incoming POST body (outer envelope, signed + encrypted):
    <xml><ToUserName/><AgentID/><Encrypt>BASE64</Encrypt></xml>
Decrypted inner message (text example):
    <xml><ToUserName>corpid</ToUserName><FromUserName>userid</FromUserName>
         <CreateTime>..</CreateTime><MsgType>text</MsgType><Content>..</Content>
         <MsgId>..</MsgId><AgentID>..</AgentID></xml>

The body is untrusted until the signature is checked, so it is parsed with
defusedxml (blocks entity-expansion / external-entity attacks).
"""
from __future__ import annotations

from dataclasses import dataclass
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from clients.wecom_crypto import WeComCrypto

MAX_BODY_BYTES = 64 * 1024


class CallbackFormatError(ValueError):
    """Body is not a well-formed WeCom callback."""


def _parse(xml: str | bytes) -> Element:
    try:
        return fromstring(xml)
    except (ParseError, DefusedXmlException) as exc:
        raise CallbackFormatError(f"invalid XML: {exc}") from exc


def _text(root: Element, tag: str) -> str:
    el = root.find(tag)
    return (el.text or "").strip() if el is not None else ""


def extract_encrypt(body: bytes) -> str:
    if len(body) > MAX_BODY_BYTES:
        raise CallbackFormatError("body too large")
    encrypt = _text(_parse(body), "Encrypt")
    if not encrypt:
        raise CallbackFormatError("missing <Encrypt>")
    return encrypt


@dataclass(frozen=True)
class IncomingMessage:
    to_user: str  # CorpID
    from_user: str  # sender's WeCom UserID
    create_time: int
    msg_type: str  # text | image | voice | event | ...
    content: str  # text messages only
    msg_id: str  # absent for events
    agent_id: str
    event: str = ""  # e.g. enter_agent, for msg_type=event


def parse_message(xml: str) -> IncomingMessage:
    root = _parse(xml)
    try:
        create_time = int(_text(root, "CreateTime") or 0)
    except ValueError:
        create_time = 0
    return IncomingMessage(
        to_user=_text(root, "ToUserName"),
        from_user=_text(root, "FromUserName"),
        create_time=create_time,
        msg_type=_text(root, "MsgType"),
        content=_text(root, "Content"),
        msg_id=_text(root, "MsgId"),
        agent_id=_text(root, "AgentID"),
        event=_text(root, "Event"),
    )


def _cdata(value: str) -> str:
    # A literal "]]>" would terminate the CDATA section; split it across two sections.
    return "<![CDATA[" + value.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_passive_reply(
    crypto: WeComCrypto, *, to_user: str, corp_id: str, content: str, timestamp: str, nonce: str
) -> str:
    """Encrypted, signed text reply returned directly in the HTTP response body."""
    inner = (
        f"<xml><ToUserName>{_cdata(to_user)}</ToUserName>"
        f"<FromUserName>{_cdata(corp_id)}</FromUserName>"
        f"<CreateTime>{timestamp}</CreateTime>"
        f"<MsgType><![CDATA[text]]></MsgType>"
        f"<Content>{_cdata(content)}</Content></xml>"
    )
    encrypted = crypto.encrypt(inner)
    signature = crypto.signature(timestamp, nonce, encrypted)
    return (
        f"<xml><Encrypt>{_cdata(encrypted)}</Encrypt>"
        f"<MsgSignature>{_cdata(signature)}</MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce>{_cdata(nonce)}</Nonce></xml>"
    )
