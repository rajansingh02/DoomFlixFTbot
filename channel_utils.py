"""
Resolving a user-supplied "channel" (from the owner running
/setchannel) into something both Telethon and python-telegram-bot
can use — including PRIVATE channels, not just public @usernames.

Supported inputs to /setchannel:

    @channelusername
    https://t.me/channelusername
    -1001234567890                (bot-API style numeric ID)
    2001234567890                 (bare channel ID, e.g. copied
                                    from a forwarded message)
    https://t.me/+AbCdEfGhIjK     (private channel invite link)
    https://t.me/joinchat/AbCdEf  (legacy private invite link)

Private channels have no username, so the ONLY way Telethon can look
them up is if the searching account (SESSION_STRING) has already seen
them — either by being a member, or by resolving the invite. We do not
auto-join on the account's behalf (joining a channel is a visible,
consequential action), so if the invite hash resolves to "not yet a
member", we tell the owner to join with that account first.

The resolved channel is stored as a canonical string:

    "@username"              for public channels
    "-100<id>"                for private (or username-less) channels

`channel_for_telethon` / `channel_for_bot_api` turn that stored string
back into whatever each library needs at call time.
"""

import re

from telethon import utils
from telethon.errors import RPCError
from telethon.tl.functions.messages import CheckChatInviteRequest
from telethon.tl.types import ChatInviteAlready, PeerChannel

_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")
_NUMERIC_RE = re.compile(r"-?\d+")


class ChannelResolutionError(ValueError):
    """Raised with a user-facing message when a channel can't be resolved."""


def _strip_url_prefix(raw: str) -> str:
    raw = raw.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    return raw.split("?", 1)[0].strip().strip("/")


async def resolve_channel_reference(telethon_client, raw_channel: str):
    """
    Resolve a raw owner-supplied string into a Telethon entity.
    Raises ChannelResolutionError with a user-facing message on failure.
    """

    raw_channel = _strip_url_prefix(raw_channel)

    if not raw_channel:
        raise ChannelResolutionError("Invalid channel.")

    # -----------------------------------------------------
    # Private invite link: "+hash" or legacy "joinchat/hash".
    # -----------------------------------------------------
    invite_hash = None

    if raw_channel.startswith("joinchat/"):
        invite_hash = raw_channel[len("joinchat/"):]
    elif raw_channel.startswith("+"):
        invite_hash = raw_channel[1:]

    if invite_hash:
        try:
            result = await telethon_client(
                CheckChatInviteRequest(invite_hash)
            )
        except RPCError as e:
            raise ChannelResolutionError(
                "Could not resolve that invite link "
                f"({type(e).__name__})."
            ) from e

        if isinstance(result, ChatInviteAlready):
            return result.chat

        raise ChannelResolutionError(
            "The search account isn't a member of that private channel "
            "yet. Join it with the account behind SESSION_STRING first, "
            "then run /setchannel again."
        )

    # -----------------------------------------------------
    # Numeric ID: bot-API style (-100xxxxxxxxxx) or a bare
    # channel ID (as seen in some forwarded-message payloads).
    # -----------------------------------------------------
    if _NUMERIC_RE.fullmatch(raw_channel):
        raw_id = int(raw_channel)

        try:
            if str(raw_id).startswith("-100"):
                real_id, peer_class = utils.resolve_id(raw_id)
                peer = peer_class(real_id)
            else:
                peer = PeerChannel(abs(raw_id))
        except Exception as e:
            raise ChannelResolutionError(f"Invalid channel ID ({e}).") from e

        try:
            return await telethon_client.get_entity(peer)
        except (RPCError, ValueError) as e:
            raise ChannelResolutionError(
                "I cannot access that channel with the Telethon account. "
                "Make sure the account is a member/admin of it."
            ) from e

    # -----------------------------------------------------
    # Public username.
    # -----------------------------------------------------
    username = raw_channel[1:] if raw_channel.startswith("@") else raw_channel

    if not _USERNAME_RE.fullmatch(username):
        raise ChannelResolutionError(
            "Invalid channel. Use a public @username, a numeric channel "
            "ID, or a private invite link."
        )

    try:
        return await telethon_client.get_entity("@" + username)
    except (RPCError, ValueError) as e:
        raise ChannelResolutionError(
            "I cannot access that channel with the Telethon account."
        ) from e


def canonical_channel_string(entity) -> str:
    """Turn a resolved Telethon entity into the string we persist."""

    username = getattr(entity, "username", None)
    if username:
        return "@" + username

    entity_id = getattr(entity, "id", None)
    if entity_id is None:
        raise ChannelResolutionError("Resolved entity has no usable ID.")

    # Bot-API style: -100 followed by the channel's internal ID.
    return f"-100{entity_id}"


def channel_for_bot_api(channel: str):
    """Convert a stored channel string into what python-telegram-bot expects."""
    if channel.startswith("@"):
        return channel
    try:
        return int(channel)
    except ValueError:
        # Not numeric and not a username — pass through, PTB will
        # raise a clear error if it's genuinely invalid.
        return channel


def channel_for_telethon(channel: str):
    """Convert a stored channel string into something Telethon can resolve."""
    if channel.startswith("@"):
        return channel

    numeric = int(channel)

    if str(numeric).startswith("-100"):
        real_id, peer_class = utils.resolve_id(numeric)
        return peer_class(real_id)

    return PeerChannel(abs(numeric))
