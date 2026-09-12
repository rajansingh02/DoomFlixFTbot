"""
All persistent state lives here: enabled groups, bans, rate limits,
and the configured movie source channel — each backed by MongoDB with
an in-memory cache where it matters for hot-path performance.

This is the single source of truth for these helpers. Do not redefine
them elsewhere (the original bot.py accidentally duplicated several of
these from config.py, which meant edits to one copy silently had no
effect at runtime).
"""

import asyncio
from datetime import datetime, timezone

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from config import (
    banned_users,
    bot_settings,
    group_config,
    rate_limits,
    CHANNEL_DEFAULT,
)


def today_string() -> str:
    """UTC date used for daily limits."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# =========================================================
# Enabled-group cache
# =========================================================

enabled_groups = set()
group_cache_lock = asyncio.Lock()


async def mongo_enable_group(chat_id: int):
    def operation():
        return group_config.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_id": chat_id,
                    "enabled": True,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    return await asyncio.to_thread(operation)


async def mongo_disable_group(chat_id: int):
    def operation():
        return group_config.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_id": chat_id,
                    "enabled": False,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    return await asyncio.to_thread(operation)


async def mongo_get_enabled_groups():
    def operation():
        return list(
            group_config.find(
                {"enabled": True},
                {"_id": 0, "chat_id": 1},
            )
        )

    return await asyncio.to_thread(operation)


async def load_enabled_groups():
    try:
        documents = await mongo_get_enabled_groups()

        groups = set()

        for document in documents:
            chat_id = document.get("chat_id")

            if chat_id is None:
                continue

            try:
                groups.add(int(chat_id))
            except (TypeError, ValueError):
                print(
                    "Ignoring invalid group chat_id in MongoDB: "
                    f"{chat_id!r}"
                )

        async with group_cache_lock:
            enabled_groups.clear()
            enabled_groups.update(groups)

        print(f"Loaded {len(groups)} enabled group(s).")

    except PyMongoError as e:
        print(f"Could not load enabled groups: {e}")


async def enable_group_cache(chat_id: int):
    await mongo_enable_group(chat_id)

    async with group_cache_lock:
        enabled_groups.add(chat_id)


async def disable_group_cache(chat_id: int):
    await mongo_disable_group(chat_id)

    async with group_cache_lock:
        enabled_groups.discard(chat_id)


async def is_group_enabled(chat_id: int) -> bool:
    async with group_cache_lock:
        return chat_id in enabled_groups


# =========================================================
# Cached invite links for /groups (avoid regenerating one
# every time the command runs)
# =========================================================

async def get_cached_group_link(chat_id: int):
    def operation():
        return group_config.find_one(
            {"chat_id": chat_id},
            {"_id": 0, "invite_link": 1},
        )

    try:
        doc = await asyncio.to_thread(operation)
    except PyMongoError as e:
        print(f"Could not read cached invite link for {chat_id}: {e}")
        return None

    return doc.get("invite_link") if doc else None


async def set_cached_group_link(chat_id: int, invite_link: str):
    def operation():
        return group_config.update_one(
            {"chat_id": chat_id},
            {"$set": {"invite_link": invite_link}},
            upsert=True,
        )

    try:
        await asyncio.to_thread(operation)
    except PyMongoError as e:
        print(f"Could not cache invite link for {chat_id}: {e}")


# =========================================================
# Ban system
# =========================================================

async def is_user_banned(user_id: int) -> bool:
    def operation():
        return banned_users.find_one(
            {"user_id": user_id},
            {"_id": 1},
        )

    try:
        result = await asyncio.to_thread(operation)
        return result is not None

    except PyMongoError as e:
        print(f"Ban lookup error for user {user_id}: {e}")
        # Fail closed.
        return True


async def ban_user_db(user_id: int, banned_by: int):
    def operation():
        return banned_users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "banned_at": datetime.now(timezone.utc),
                    "banned_by": banned_by,
                }
            },
            upsert=True,
        )

    return await asyncio.to_thread(operation)


async def unban_user_db(user_id: int):
    def operation():
        return banned_users.delete_one({"user_id": user_id})

    return await asyncio.to_thread(operation)


async def list_banned_users():
    def operation():
        return list(
            banned_users.find(
                {},
                {"_id": 0, "user_id": 1, "banned_at": 1},
            ).sort("banned_at", -1)
        )

    return await asyncio.to_thread(operation)


# =========================================================
# Daily rate limiting
# =========================================================

def consume_daily_limit_sync(
    user_id: int,
    chat_id: int,
    limit: int,
) -> bool:
    """
    Atomically consume one request.

    Key: user_id + chat_id + date, so each chat (each group, and
    private chat) tracks its own independent limit per user.
    """

    today = today_string()
    now = datetime.now(timezone.utc)

    try:
        # Fast path: existing record for today, still under limit.
        result = rate_limits.find_one_and_update(
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "date": today,
                "count": {"$lt": limit},
            },
            {
                "$inc": {"count": 1},
                "$set": {"updated_at": now},
            },
            return_document=ReturnDocument.AFTER,
        )

        if result is not None:
            return True

        existing = rate_limits.find_one(
            {"user_id": user_id, "chat_id": chat_id},
            {"_id": 1, "date": 1, "count": 1},
        )

        if existing is not None:
            if existing.get("date") != today:
                # Old day -> reset to 1.
                result = rate_limits.find_one_and_update(
                    {
                        "_id": existing["_id"],
                        "date": existing.get("date"),
                    },
                    {
                        "$set": {
                            "date": today,
                            "count": 1,
                            "updated_at": now,
                        },
                    },
                    return_document=ReturnDocument.AFTER,
                )

                if result is not None:
                    return True

                # Another request changed it concurrently; try the
                # normal atomic update once more.
                result = rate_limits.find_one_and_update(
                    {
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "date": today,
                        "count": {"$lt": limit},
                    },
                    {
                        "$inc": {"count": 1},
                        "$set": {"updated_at": now},
                    },
                    return_document=ReturnDocument.AFTER,
                )

                return result is not None

            # Same day, limit reached.
            return False

        # No record at all -> first request.
        try:
            rate_limits.insert_one(
                {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "date": today,
                    "count": 1,
                    "updated_at": now,
                }
            )
            return True

        except DuplicateKeyError:
            # Another request created it concurrently.
            result = rate_limits.find_one_and_update(
                {
                    "user_id": user_id,
                    "chat_id": chat_id,
                    "date": today,
                    "count": {"$lt": limit},
                },
                {
                    "$inc": {"count": 1},
                    "$set": {"updated_at": now},
                },
                return_document=ReturnDocument.AFTER,
            )
            return result is not None

    except PyMongoError as e:
        print(f"Rate-limit MongoDB error: {e}")
        # Fail closed.
        return False


async def consume_daily_limit(user_id: int, chat_id: int, limit: int) -> bool:
    return await asyncio.to_thread(
        consume_daily_limit_sync, user_id, chat_id, limit
    )


async def reset_user_limits_db(user_id: int):
    """Remove all current rate-limit records for this user (all chats)."""

    def operation():
        return rate_limits.delete_many({"user_id": user_id})

    return await asyncio.to_thread(operation)


# =========================================================
# Single movie source channel
# =========================================================

_configured_channel = CHANNEL_DEFAULT
_channel_lock = asyncio.Lock()


def get_configured_channel() -> str:
    """
    Return the currently configured movie source channel, as a
    canonical string: "@username" or a bot-API style numeric ID
    string like "-1001234567890".
    """
    return _configured_channel


async def load_configured_channel():
    global _configured_channel

    def operation():
        return bot_settings.find_one({"_id": "movie_channel"})

    try:
        document = await asyncio.to_thread(operation)

        if document and document.get("channel"):
            channel = str(document["channel"]).strip()
        else:
            channel = CHANNEL_DEFAULT

        if not channel:
            channel = CHANNEL_DEFAULT

        async with _channel_lock:
            _configured_channel = channel

        print(f"Configured movie channel: {channel}")
        return channel

    except PyMongoError as e:
        print(f"Could not load configured channel: {e}")

        async with _channel_lock:
            _configured_channel = CHANNEL_DEFAULT

        return CHANNEL_DEFAULT


async def set_configured_channel(channel: str):
    global _configured_channel

    channel = str(channel).strip()

    if not channel:
        raise ValueError("Channel cannot be empty")

    now = datetime.now(timezone.utc)

    def operation():
        return bot_settings.update_one(
            {"_id": "movie_channel"},
            {"$set": {"channel": channel, "updated_at": now}},
            upsert=True,
        )

    result = await asyncio.to_thread(operation)

    async with _channel_lock:
        _configured_channel = channel

    return result
