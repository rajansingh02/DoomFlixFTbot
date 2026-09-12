"""
Signed deep links: https://t.me/<bot>?start=movie_<id>_<group>_<sig>

The signature binds the message ID, the group it was found in, AND
the currently configured channel, so if the owner ever repoints
/setchannel at a different channel, old links stop resolving instead
of silently pointing at an unrelated message with the same ID.
"""

import hashlib
import hmac
import re

from config import BOT_USERNAME, DEEP_LINK_SECRET, MAX_MOVIE_PAYLOAD_LENGTH
from db import get_configured_channel

_PAYLOAD_RE = re.compile(
    r"movie_(\d+)_(-?\d+)_([0-9a-f]{12})",
    re.IGNORECASE,
)


def make_deep_link_signature(message_id: int, group_id: int) -> str:
    channel = get_configured_channel()
    payload = f"{message_id}:{group_id}:{channel}".encode("utf-8")

    return hmac.new(
        DEEP_LINK_SECRET,
        payload,
        hashlib.sha256,
    ).hexdigest()[:12]


def make_movie_deep_link(message_id: int, group_id: int) -> str:
    signature = make_deep_link_signature(message_id, group_id)
    payload = f"movie_{message_id}_{group_id}_{signature}"
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


def parse_movie_payload(payload: str):
    """
    Format: movie_<message_id>_<group_id>_<signature>
    Returns {"message_id": int, "group_id": int} or None if invalid.
    """

    if not payload:
        return None

    payload = payload.strip()

    if len(payload) > MAX_MOVIE_PAYLOAD_LENGTH:
        return None

    match = _PAYLOAD_RE.fullmatch(payload)

    if not match:
        return None

    message_id = int(match.group(1))
    group_id = int(match.group(2))
    supplied_signature = match.group(3).lower()

    expected_signature = make_deep_link_signature(message_id, group_id).lower()

    if not hmac.compare_digest(supplied_signature, expected_signature):
        return None

    return {"message_id": message_id, "group_id": group_id}
