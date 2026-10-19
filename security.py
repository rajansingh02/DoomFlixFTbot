"""
Signed movie links. Requester-bound links prevent sharing a search result
with another Telegram user.
"""

import hashlib
import hmac
import re

from config import BOT_USERNAME, DEEP_LINK_SECRET, MAX_MOVIE_PAYLOAD_LENGTH
from db import get_configured_channel

_LEGACY_RE = re.compile(r"movie_(\d+)_(-?\d+)_([0-9a-f]{12})", re.I)
_BOUND_RE = re.compile(r"movie_(\d+)_(-?\d+)_(\d+)_([0-9a-f]{12})", re.I)


def make_deep_link_signature(message_id: int, group_id: int,
                             requester_user_id: int | None = None) -> str:
    channel = get_configured_channel()
    if requester_user_id is None:
        payload = f"{message_id}:{group_id}:{channel}"
    else:
        payload = f"{message_id}:{group_id}:{requester_user_id}:{channel}"
    return hmac.new(
        DEEP_LINK_SECRET, payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:12]


def make_movie_deep_link(message_id: int, group_id: int,
                         requester_user_id: int | None = None) -> str:
    signature = make_deep_link_signature(
        message_id, group_id, requester_user_id
    )
    if requester_user_id is None:
        payload = f"movie_{message_id}_{group_id}_{signature}"
    else:
        payload = (
            f"movie_{message_id}_{group_id}_{requester_user_id}_{signature}"
        )
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


def parse_movie_payload(payload: str):
    """Validate a legacy or requester-bound movie deep link."""

    if not payload:
        return None
    payload = payload.strip()
    if len(payload) > MAX_MOVIE_PAYLOAD_LENGTH:
        return None

    match = _BOUND_RE.fullmatch(payload)
    if match:
        message_id = int(match.group(1))
        group_id = int(match.group(2))
        requester_user_id = int(match.group(3))
        supplied_signature = match.group(4).lower()
        expected = make_deep_link_signature(
            message_id, group_id, requester_user_id
        ).lower()
        if not hmac.compare_digest(supplied_signature, expected):
            return None
        return {
            "message_id": message_id,
            "group_id": group_id,
            "requester_user_id": requester_user_id,
        }

    match = _LEGACY_RE.fullmatch(payload)
    if not match:
        return None
    message_id = int(match.group(1))
    group_id = int(match.group(2))
    supplied_signature = match.group(3).lower()
    expected = make_deep_link_signature(message_id, group_id).lower()
    if not hmac.compare_digest(supplied_signature, expected):
        return None
    return {
        "message_id": message_id,
        "group_id": group_id,
        "requester_user_id": None,
    }
