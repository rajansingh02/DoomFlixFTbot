import asyncio
import hashlib
import hmac
import html
import os
import re
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from telethon import TelegramClient
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================================================
# Configuration
# =========================================================

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
MONGODB_URI = os.environ["MONGODB_URI"]
BOT_OWNER_ID = int(os.environ["BOT_OWNER_ID"])

CHANNEL = "@doomflixmovies"
BOT_USERNAME = "DoomflixFTbot"


# =========================================================
# Limits
# =========================================================

GROUP_DAILY_LIMIT = 10
PRIVATE_DAILY_LIMIT = 20

MAX_RESULTS = 10


# =========================================================
# Deep-link security
# =========================================================

DEEP_LINK_SECRET = hashlib.sha256(
    BOT_TOKEN.encode("utf-8")
).digest()

MAX_MOVIE_PAYLOAD_LENGTH = 80


# =========================================================
# MongoDB
# =========================================================

mongo_client = MongoClient(
    MONGODB_URI,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=5000,
    maxPoolSize=10,
    minPoolSize=1,
    retryWrites=True,
)

db = mongo_client["movie_search_bot"]

rate_limits = db["rate_limits"]
group_config = db["group_config"]
banned_users = db["banned_users"]


# =========================================================
# MongoDB indexes
# =========================================================

try:
    rate_limits.create_index(
        [
            ("user_id", 1),
            ("chat_id", 1),
            ("date", 1),
        ],
        unique=True,
        background=True,
    )

    group_config.create_index(
        [("chat_id", 1)],
        unique=True,
        background=True,
    )

    banned_users.create_index(
        [("user_id", 1)],
        unique=True,
        background=True,
    )

except PyMongoError as e:
    print(
        f"MongoDB index warning: {e}"
    )


# =========================================================
# Telethon
# =========================================================

telethon_client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
)


# =========================================================
# In-memory enabled-group cache
# =========================================================

enabled_groups = set()

group_cache_lock = asyncio.Lock()


# =========================================================
# General helpers
# =========================================================

def today_string() -> str:
    """
    UTC date used for daily limits.
    """

    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")


def is_bot_owner(
    update: Update,
) -> bool:
    user = update.effective_user

    if not user:
        return False

    return user.id == BOT_OWNER_ID


def is_private_chat(
    update: Update,
) -> bool:
    chat = update.effective_chat

    return bool(
        chat
        and chat.type == ChatType.PRIVATE
    )


def is_group_chat(
    update: Update,
) -> bool:
    chat = update.effective_chat

    return bool(
        chat
        and chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        )
    )


# =========================================================
# Group configuration
# =========================================================

async def mongo_enable_group(
    chat_id: int,
):
    def operation():
        return group_config.update_one(
            {
                "chat_id": chat_id,
            },
            {
                "$set": {
                    "chat_id": chat_id,
                    "enabled": True,
                    "updated_at": datetime.now(
                        timezone.utc
                    ),
                }
            },
            upsert=True,
        )

    return await asyncio.to_thread(
        operation
    )


async def mongo_disable_group(
    chat_id: int,
):
    def operation():
        return group_config.update_one(
            {
                "chat_id": chat_id,
            },
            {
                "$set": {
                    "chat_id": chat_id,
                    "enabled": False,
                    "updated_at": datetime.now(
                        timezone.utc
                    ),
                }
            },
            upsert=True,
        )

    return await asyncio.to_thread(
        operation
    )


async def mongo_get_enabled_groups():
    def operation():
        return list(
            group_config.find(
                {
                    "enabled": True,
                },
                {
                    "_id": 0,
                    "chat_id": 1,
                },
            )
        )

    return await asyncio.to_thread(
        operation
    )


async def load_enabled_groups():
    try:
        documents = (
            await mongo_get_enabled_groups()
        )

        groups = {
            int(doc["chat_id"])
            for doc in documents
            if doc.get("chat_id") is not None
        }

        async with group_cache_lock:
            enabled_groups.clear()
            enabled_groups.update(groups)

        print(
            f"Loaded {len(groups)} enabled group(s)."
        )

    except PyMongoError as e:
        print(
            f"Could not load enabled groups: {e}"
        )


async def enable_group_cache(
    chat_id: int,
):
    await mongo_enable_group(
        chat_id
    )

    async with group_cache_lock:
        enabled_groups.add(chat_id)


async def disable_group_cache(
    chat_id: int,
):
    await mongo_disable_group(
        chat_id
    )

    async with group_cache_lock:
        enabled_groups.discard(chat_id)


async def is_group_enabled(
    chat_id: int,
) -> bool:
    async with group_cache_lock:
        return chat_id in enabled_groups


# =========================================================
# Ban system
# =========================================================

async def is_user_banned(
    user_id: int,
) -> bool:
    def operation():
        return banned_users.find_one(
            {
                "user_id": user_id,
            },
            {
                "_id": 1,
            },
        )

    try:
        result = await asyncio.to_thread(
            operation
        )

        return result is not None

    except PyMongoError as e:
        print(
            f"Ban lookup error for "
            f"user {user_id}: {e}"
        )

        # Fail closed.
        return True


async def ban_user_db(
    user_id: int,
):
    def operation():
        return banned_users.update_one(
            {
                "user_id": user_id,
            },
            {
                "$set": {
                    "user_id": user_id,
                    "banned_at": datetime.now(
                        timezone.utc
                    ),
                    "banned_by": BOT_OWNER_ID,
                }
            },
            upsert=True,
        )

    return await asyncio.to_thread(
        operation
    )


async def unban_user_db(
    user_id: int,
):
    def operation():
        return banned_users.delete_one(
            {
                "user_id": user_id,
            }
        )

    return await asyncio.to_thread(
        operation
    )


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

    Key:

        user_id + chat_id + date

    Therefore:

        Group A -> separate limit
        Group B -> separate limit
        Private -> separate limit
    """

    today = today_string()

    now = datetime.now(
        timezone.utc
    )

    try:

        # -------------------------------------------------
        # Existing record for today and still under limit.
        # -------------------------------------------------

        result = rate_limits.find_one_and_update(
            {
                "user_id": user_id,
                "chat_id": chat_id,
                "date": today,
                "count": {
                    "$lt": limit,
                },
            },
            {
                "$inc": {
                    "count": 1,
                },
                "$set": {
                    "updated_at": now,
                },
            },
            return_document=ReturnDocument.AFTER,
        )

        if result is not None:
            return True

        # -------------------------------------------------
        # Check whether a record exists for this user/chat.
        #
        # If it exists with an old date, reset it.
        # -------------------------------------------------

        existing = rate_limits.find_one(
            {
                "user_id": user_id,
                "chat_id": chat_id,
            },
            {
                "_id": 1,
                "date": 1,
                "count": 1,
            },
        )

        if existing is not None:

            # -------------------------------------------------
            # Old day -> reset to 1.
            # -------------------------------------------------

            if existing.get("date") != today:

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

                # Another request changed it concurrently.
                # Try normal atomic update once more.
                result = rate_limits.find_one_and_update(
                    {
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "date": today,
                        "count": {
                            "$lt": limit,
                        },
                    },
                    {
                        "$inc": {
                            "count": 1,
                        },
                        "$set": {
                            "updated_at": now,
                        },
                    },
                    return_document=ReturnDocument.AFTER,
                )

                return result is not None

            # -------------------------------------------------
            # Same day and limit reached.
            # -------------------------------------------------

            return False

        # -------------------------------------------------
        # No record at all -> first request.
        # -------------------------------------------------

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
                    "count": {
                        "$lt": limit,
                    },
                },
                {
                    "$inc": {
                        "count": 1,
                    },
                    "$set": {
                        "updated_at": now,
                    },
                },
                return_document=ReturnDocument.AFTER,
            )

            return result is not None

    except PyMongoError as e:
        print(
            f"Rate-limit MongoDB error: {e}"
        )

        # Fail closed.
        return False


async def consume_daily_limit(
    user_id: int,
    chat_id: int,
    limit: int,
) -> bool:
    return await asyncio.to_thread(
        consume_daily_limit_sync,
        user_id,
        chat_id,
        limit,
    )


# =========================================================
# RESET DAILY LIMIT
# =========================================================

async def reset_user_limits_db(
    user_id: int,
):
    """
    Remove all current rate-limit records for this user.

    This resets:

        - all group limits
        - private limit

    Old historical records, if any, are also removed.
    """

    def operation():
        return rate_limits.delete_many(
            {
                "user_id": user_id,
            }
        )

    return await asyncio.to_thread(
        operation
    )


# =========================================================
# Resolve target user
# =========================================================

def get_target_user_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Supports:

        /command 123456789

    and:

        reply to user's message
        /command
    """

    message = update.message

    if not message:
        return None

    # -----------------------------------------------------
    # First preference: replied-to user.
    # -----------------------------------------------------

    reply = message.reply_to_message

    if reply and reply.from_user:

        target = reply.from_user

        return target.id

    # -----------------------------------------------------
    # Otherwise use numeric argument.
    # -----------------------------------------------------

    if context.args:

        raw_id = context.args[0].strip()

        try:
            user_id = int(raw_id)

        except ValueError:
            return None

        if user_id <= 0:
            return None

        return user_id

    return None


# =========================================================
# Deep-link security
# =========================================================

def make_deep_link_signature(
    message_id: int,
    group_id: int,
) -> str:
    payload = (
        f"{message_id}:{group_id}"
    ).encode("utf-8")

    return hmac.new(
        DEEP_LINK_SECRET,
        payload,
        hashlib.sha256,
    ).hexdigest()[:12]


def make_movie_deep_link(
    message_id: int,
    group_id: int,
) -> str:
    signature = make_deep_link_signature(
        message_id,
        group_id,
    )

    payload = (
        f"movie_{message_id}_"
        f"{group_id}_"
        f"{signature}"
    )

    return (
        f"https://t.me/{BOT_USERNAME}"
        f"?start={payload}"
    )


def parse_movie_payload(
    payload: str,
):
    """
    Format:

        movie_<message_id>_<group_id>_<signature>
    """

    if not payload:
        return None

    payload = payload.strip()

    if (
        len(payload)
        > MAX_MOVIE_PAYLOAD_LENGTH
    ):
        return None

    match = re.fullmatch(
        r"movie_(\d+)_(-?\d+)_([0-9a-f]{12})",
        payload,
        re.IGNORECASE,
    )

    if not match:
        return None

    message_id = int(
        match.group(1)
    )

    group_id = int(
        match.group(2)
    )

    supplied_signature = (
        match.group(3).lower()
    )

    expected_signature = (
        make_deep_link_signature(
            message_id,
            group_id,
        ).lower()
    )

    if not hmac.compare_digest(
        supplied_signature,
        expected_signature,
    ):
        return None

    return {
        "message_id": message_id,
        "group_id": group_id,
    }


# =========================================================
# Group admin check
# =========================================================

async def is_group_admin(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> bool:

    try:

        member = await context.bot.get_chat_member(
            chat_id=chat_id,
            user_id=user_id,
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except TelegramError as e:

        print(
            f"Admin check failed "
            f"user={user_id} "
            f"group={chat_id}: {e}"
        )

        # Fail closed.
        return False


# =========================================================
# Access control
# =========================================================

async def check_private_access(
    update: Update,
) -> bool:
    """
    Private movie delivery:

        owner  -> unlimited
        banned -> blocked
        user   -> 20/day
    """

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    user_id = user.id

    # -----------------------------------------------------
    # Owner bypass.
    # -----------------------------------------------------

    if user_id == BOT_OWNER_ID:
        return True

    # -----------------------------------------------------
    # Ban.
    # -----------------------------------------------------

    if await is_user_banned(
        user_id
    ):

        try:
            await update.effective_message.reply_text(
                "🚫 You are banned from using this bot."
            )

        except TelegramError:
            pass

        return False

    # -----------------------------------------------------
    # 20/day private requests.
    # -----------------------------------------------------

    allowed = await consume_daily_limit(
        user_id=user_id,
        chat_id=chat.id,
        limit=PRIVATE_DAILY_LIMIT,
    )

    if not allowed:

        try:
            await update.effective_message.reply_text(
                "⛔ You have reached your daily "
                f"private limit of "
                f"{PRIVATE_DAILY_LIMIT} requests.\n\n"
                "Try again tomorrow."
            )

        except TelegramError:
            pass

        return False

    return True


async def check_group_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    Group:

        disabled -> ignored
        banned   -> blocked
        owner    -> unlimited
        admin    -> unlimited
        user     -> 10/day
    """

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    # -----------------------------------------------------
    # Group must be enabled.
    # -----------------------------------------------------

    if not await is_group_enabled(
        chat.id
    ):
        return False

    # -----------------------------------------------------
    # Owner.
    # -----------------------------------------------------

    if user.id == BOT_OWNER_ID:
        return True

    # -----------------------------------------------------
    # Ban.
    # -----------------------------------------------------

    if await is_user_banned(
        user.id
    ):

        try:
            await update.effective_message.reply_text(
                "🚫 You are banned from using this bot."
            )

        except TelegramError:
            pass

        return False

    # -----------------------------------------------------
    # Admin / creator.
    # -----------------------------------------------------

    admin = await is_group_admin(
        context=context,
        chat_id=chat.id,
        user_id=user.id,
    )

    if admin:
        return True

    # -----------------------------------------------------
    # Normal user -> 10/day.
    # -----------------------------------------------------

    allowed = await consume_daily_limit(
        user_id=user.id,
        chat_id=chat.id,
        limit=GROUP_DAILY_LIMIT,
    )

    if not allowed:

        try:
            await update.effective_message.reply_text(
                "⛔ You have reached your daily "
                f"group limit of "
                f"{GROUP_DAILY_LIMIT} searches.\n\n"
                "Try again tomorrow."
            )

        except TelegramError:
            pass

        return False

    return True


# =========================================================
# Send exact movie post
# =========================================================

async def send_movie_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_id: int,
):
    """
    Forward the exact selected channel post.

    The movie is NOT searched again.
    """

    chat = update.effective_chat

    if not chat:
        return

    try:

        # -------------------------------------------------
        # Verify source message still exists.
        # -------------------------------------------------

        message = await telethon_client.get_messages(
            CHANNEL,
            ids=message_id,
        )

        if not message:

            await update.effective_message.reply_text(
                "❌ This movie post is no longer available."
            )

            return

        # -------------------------------------------------
        # Forward exact post.
        # -------------------------------------------------

        await context.bot.forward_message(
            chat_id=chat.id,
            from_chat_id=CHANNEL,
            message_id=message_id,
        )

    except FloodWaitError as e:

        print(
            f"Telegram flood wait: {e.seconds}s"
        )

        try:
            await update.effective_message.reply_text(
                "⏳ Telegram is temporarily "
                "rate-limiting requests.\n\n"
                f"Please try again in about "
                f"{e.seconds} seconds."
            )

        except TelegramError:
            pass

    except TelegramError as e:

        print(
            f"Movie forwarding error "
            f"message={message_id}: {e}"
        )

        try:
            await update.effective_message.reply_text(
                "⚠️ I couldn't send this movie right now.\n\n"
                "The original movie post may have been "
                "removed or is temporarily unavailable."
            )

        except TelegramError:
            pass

    except RPCError as e:

        print(
            f"Telethon movie lookup error: {e}"
        )

        try:
            await update.effective_message.reply_text(
                "⚠️ I couldn't retrieve this movie post."
            )

        except TelegramError:
            pass

    except Exception as e:

        print(
            f"Unexpected movie delivery error: {e}"
        )

        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong while "
                "sending the movie."
            )

        except TelegramError:
            pass


# =========================================================
# /start
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not update.effective_chat
        or not update.message
        or not update.effective_user
    ):
        return

    chat = update.effective_chat

    # =====================================================
    # /start WITHOUT payload
    # =====================================================

    if not context.args:

        # -------------------------------------------------
        # Private help.
        #
        # IMPORTANT:
        # This does NOT consume a daily request.
        # -------------------------------------------------

        if chat.type == ChatType.PRIVATE:

            if (
                not is_bot_owner(update)
                and await is_user_banned(
                    update.effective_user.id
                )
            ):
                await update.message.reply_text(
                    "🚫 You are banned from using this bot."
                )
                return

            await update.message.reply_text(
    "🎬 <b>DoomFlix Movie Search</b>\n\n"
    "This bot belongs to "
    "<a href=\"https://t.me/doomflix\"><b>DoomFlix</b></a>.\n\n"
    "🔎 Use <code>movie &lt;title&gt;</code> in a group to search for movies.\n\n"
    "✨ Discover what this bot can do in "
    "<a href=\"https://t.me/request_movies_series_anime\">"
    "<b>DoomFlix's Official Request Group</b></a>.",
    parse_mode="HTML",
)

            return

        # -------------------------------------------------
        # Group.
        # -------------------------------------------------

        if chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        ):

            if not await is_group_enabled(
                chat.id
            ):
                return

            if not await check_group_access(
                update,
                context,
            ):
                return

            await update.message.reply_text(
                "🎬 Send a movie request using:\n\n"
                "<code>movie Movie Name</code>",
                parse_mode="HTML",
            )

            return

        return

    # =====================================================
    # /start WITH movie payload
    # =====================================================

    payload = context.args[0].strip()

    movie_data = parse_movie_payload(
        payload
    )

    if not movie_data:

        await update.message.reply_text(
            "⚠️ Invalid movie link."
        )

        return

    message_id = movie_data[
        "message_id"
    ]

    # -----------------------------------------------------
    # Movie deep-links must be opened privately.
    # -----------------------------------------------------

    if chat.type != ChatType.PRIVATE:

        await update.message.reply_text(
            "⚠️ Please open the movie link in "
            "a private chat with the bot."
        )

        return

    # -----------------------------------------------------
    # Private delivery = one 20/day request.
    # -----------------------------------------------------

    if not await check_private_access(
        update
    ):
        return

    # -----------------------------------------------------
    # Forward exact selected post.
    # -----------------------------------------------------

    await send_movie_post(
        update=update,
        context=context,
        message_id=message_id,
    )


# =========================================================
# /movie
# =========================================================

async def movie_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not update.message
        or not update.effective_chat
        or not update.effective_user
    ):
        return

    chat = update.effective_chat

    # =====================================================
    # PRIVATE CHAT
    # =====================================================

    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "🎬 Movie search is available from enabled groups only."
        )
        return

    # =====================================================
    # GROUP
    # =====================================================

    if chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        if not await is_group_enabled(
            chat.id
        ):
            return

        if not context.args:
            await update.message.reply_text(
                "Usage:\n"
                "<code>/movie Movie Name</code>",
                parse_mode="HTML",
            )
            return

        if not await check_group_access(
            update,
            context,
        ):
            return

        query = " ".join(
            context.args
        ).strip()

        await perform_movie_search(
            update,
            context,
            query,
        )


# =========================================================
# Group text search
# =========================================================

async def group_text_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Group searches:

        movie <title>

    Case-insensitive.
    """

    if (
        not update.message
        or not update.effective_chat
        or not update.effective_user
    ):
        return

    chat = update.effective_chat

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    text = (
        update.message.text
        or ""
    ).strip()

    if not text:
        return

    # -----------------------------------------------------
    # Only "movie <query>"
    # -----------------------------------------------------

    match = re.match(
        r"^movie\s+(.+?)\s*$",
        text,
        re.IGNORECASE,
    )

    if not match:
        return

    # -----------------------------------------------------
    # Group must be enabled.
    # -----------------------------------------------------

    if not await is_group_enabled(
        chat.id
    ):
        return

    # -----------------------------------------------------
    # Access control.
    # -----------------------------------------------------

    if not await check_group_access(
        update,
        context,
    ):
        return

    query = match.group(1).strip()

    if not query:
        return

    if len(query) > 200:
        query = query[:200].strip()

    await perform_movie_search(
        update,
        context,
        query,
    )


# =========================================================
# Movie search
# =========================================================

async def perform_movie_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
):
    query = re.sub(
        r"\s+",
        " ",
        query.strip(),
    )

    if not query:
        return

    if len(query) > 200:
        query = query[:200].strip()

    # -----------------------------------------------------
    # Searching message.
    # -----------------------------------------------------

    status_message = None

    try:
        status_message = (
            await update.effective_message.reply_text(
                "🔎 Searching..."
            )
        )

    except TelegramError:
        pass

    # -----------------------------------------------------
    # Search source channel.
    # -----------------------------------------------------

    try:

        results = []

        async for message in (
            telethon_client.iter_messages(
                CHANNEL,
                search=query,
                limit=MAX_RESULTS,
            )
        ):

            if not message:
                continue

            if not message.id:
                continue

            # Ignore service messages.
            if message.action:
                continue

            text = (
                getattr(
                    message,
                    "message",
                    None,
                )
                or ""
            ).strip()

            if not text:
                continue

            results.append(
                message
            )

            if len(results) >= MAX_RESULTS:
                break

    except FloodWaitError as e:

        print(
            f"Search flood wait: {e.seconds}s"
        )

        if status_message:

            try:
                await status_message.edit_text(
                    "⏳ Telegram is temporarily "
                    "rate-limiting searches.\n\n"
                    f"Try again in about "
                    f"{e.seconds} seconds."
                )

            except TelegramError:
                pass

        return

    except RPCError as e:

        print(
            f"Telethon search error: {e}"
        )

        if status_message:

            try:
                await status_message.edit_text(
                    "⚠️ Movie search is temporarily "
                    "unavailable."
                )

            except TelegramError:
                pass

        return

    except Exception as e:

        print(
            f"Unexpected search error: {e}"
        )

        if status_message:

            try:
                await status_message.edit_text(
                    "⚠️ Something went wrong "
                    "while searching."
                )

            except TelegramError:
                pass

        return

    # -----------------------------------------------------
    # No results.
    # -----------------------------------------------------

    if not results:

        safe_query = html.escape(
            query
        )

        text = (
            "🎬 <b>Movie Search</b>\n\n"
            f"No titles found for "
            f"<b>“{safe_query}”</b>.\n\n"
            "⚠️ Make sure your movie title is correct.\n\n"
            "📩 Movie not showing? Send "
            "<code>#request [Movie Title] [YEAR]</code>\n"
            "It’ll be uploaded if available."
        )

        if status_message:

            try:
                await status_message.edit_text(
                    text,
                    parse_mode="HTML",
                )

            except TelegramError:
                pass

        return

    # -----------------------------------------------------
    # Build buttons.
    # -----------------------------------------------------

    buttons = []

    for message in results:

        title = (
            message.message
            or ""
        ).strip()

        if not title:
            continue

        # Telegram button text should remain short.
        if len(title) > 60:
            title = (
                title[:57]
                + "..."
            )

        deep_link = make_movie_deep_link(
            message_id=message.id,
            group_id=(
                update.effective_chat.id
            ),
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    title,
                    url=deep_link,
                )
            ]
        )

    if not buttons:

        if status_message:

            try:
                await status_message.edit_text(
                    "⚠️ Search results were unavailable."
                )

            except TelegramError:
                pass

        return

    # -----------------------------------------------------
    # Result text.
    # -----------------------------------------------------

    safe_query = html.escape(
        query
    )

    result_text = (
        "🎬 <b>Movie Search</b>\n\n"
        f"Found <b>{len(buttons)}</b> results for "
        f"<b>“{safe_query}”</b>\n"
        "⚠️ Incorrect result? Make sure your "
        "movie title is correct.\n\n"
        "📩 Movie not showing? Send "
        "<code>#request [Movie Title] [YEAR]</code>\n"
        "It’ll be uploaded if available.\n\n"
        "👇 Select a movie below:"
    )

    # -----------------------------------------------------
    # Delete searching message.
    # -----------------------------------------------------

    if status_message:

        try:
            await status_message.delete()

        except TelegramError:
            pass

    # -----------------------------------------------------
    # Send results.
    # -----------------------------------------------------

    try:

        await update.effective_message.reply_text(
            result_text,
            reply_markup=InlineKeyboardMarkup(
                buttons
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_to_message_id=(
                update.effective_message.message_id
            ),
        )

    except TelegramError as e:

        print(
            f"Result message error: {e}"
        )


# =========================================================
# Owner: enable group
# =========================================================

async def enable_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_owner(update):
        return

    if (
        not update.message
        or not update.effective_chat
    ):
        return

    chat = update.effective_chat

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        await update.message.reply_text(
            "❌ Run /enable inside the group "
            "you want to enable."
        )

        return

    try:

        await enable_group_cache(
            chat.id
        )

        await update.message.reply_text(
            "✅ <b>DoomFlix has been enabled "
            "in this group.</b>\n\n"
            f"👤 Normal users: "
            f"{GROUP_DAILY_LIMIT} searches/day\n"
            "👮 Group admins: unlimited\n"
            "👑 Bot owner: unlimited",
            parse_mode="HTML",
        )

    except PyMongoError as e:

        print(
            f"Enable group error: {e}"
        )

        await update.message.reply_text(
            "❌ I couldn't enable the bot "
            "in this group."
        )


# =========================================================
# Owner: disable group
# =========================================================

async def disable_group(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_owner(update):
        return

    if (
        not update.message
        or not update.effective_chat
    ):
        return

    chat = update.effective_chat

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        await update.message.reply_text(
            "❌ Run /disable inside the group."
        )

        return

    try:

        await disable_group_cache(
            chat.id
        )

        await update.message.reply_text(
            "✅ <b>DoomFlix has been disabled "
            "in this group.</b>",
            parse_mode="HTML",
        )

    except PyMongoError as e:

        print(
            f"Disable group error: {e}"
        )

        await update.message.reply_text(
            "❌ I couldn't disable the bot "
            "in this group."
        )


# =========================================================
# Owner: ban user
# =========================================================

async def ban_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    target_user_id = get_target_user_id(
        update,
        context,
    )

    if target_user_id is None:

        await update.message.reply_text(
            "Usage:\n"
            "<code>/ban USER_ID</code>\n\n"
            "Or reply to a user's message with "
            "<code>/ban</code>.",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # Never ban owner.
    # -----------------------------------------------------

    if target_user_id == BOT_OWNER_ID:

        await update.message.reply_text(
            "❌ You cannot ban the bot owner."
        )

        return

    try:

        await ban_user_db(
            target_user_id
        )

        await update.message.reply_text(
            "🚫 <b>User banned successfully.</b>\n\n"
            f"User ID: "
            f"<code>{target_user_id}</code>",
            parse_mode="HTML",
        )

    except PyMongoError as e:

        print(
            f"Ban command error: {e}"
        )

        await update.message.reply_text(
            "❌ Failed to ban the user."
        )


# =========================================================
# Owner: unban user
# =========================================================

async def unban_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    target_user_id = get_target_user_id(
        update,
        context,
    )

    if target_user_id is None:

        await update.message.reply_text(
            "Usage:\n"
            "<code>/unban USER_ID</code>\n\n"
            "Or reply to a user's message with "
            "<code>/unban</code>.",
            parse_mode="HTML",
        )

        return

    try:

        result = await unban_user_db(
            target_user_id
        )

        if result.deleted_count:

            await update.message.reply_text(
                "✅ <b>User unbanned.</b>\n\n"
                f"User ID: "
                f"<code>{target_user_id}</code>",
                parse_mode="HTML",
            )

        else:

            await update.message.reply_text(
                "ℹ️ That user is not banned."
            )

    except PyMongoError as e:

        print(
            f"Unban command error: {e}"
        )

        await update.message.reply_text(
            "❌ Failed to unban the user."
        )

# =========================================================
# Owner: ban list
# =========================================================

async def banlist_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    def operation():
        return list(
            banned_users.find(
                {},
                {
                    "_id": 0,
                    "user_id": 1,
                    "banned_at": 1,
                },
            ).sort(
                "banned_at",
                -1,
            )
        )

    try:
        users = await asyncio.to_thread(
            operation
        )

    except PyMongoError as e:
        print(
            f"Ban list error: {e}"
        )

        await update.message.reply_text(
            "❌ Failed to load the ban list."
        )

        return

    if not users:
        await update.message.reply_text(
            "📋 <b>Ban List</b>\n\n"
            "No users are currently banned.",
            parse_mode="HTML",
        )
        return

    lines = [
        "📋 <b>Banned Users</b>",
        "",
        f"Total: <b>{len(users)}</b>",
        "",
    ]

    for index, user in enumerate(
        users,
        start=1,
    ):
        user_id = user.get("user_id")

        if user_id is None:
            continue

        lines.append(
            f"{index}. "
            f"<code>{user_id}</code>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
    )


# =========================================================
# Owner: reset user limit
# =========================================================

async def reset_limit_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Owner-only.

    Usage:

        /resetlimit USER_ID

    Or reply to a user's message:

        /resetlimit

    This clears the user's rate-limit records,
    allowing a fresh daily allowance.
    """

    if not is_bot_owner(update):
        return

    if not update.message:
        return

    target_user_id = get_target_user_id(
        update,
        context,
    )

    if target_user_id is None:

        await update.message.reply_text(
            "Usage:\n"
            "<code>/resetlimit USER_ID</code>\n\n"
            "Or reply to a user's message with "
            "<code>/resetlimit</code>.",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------------------
    # Owner already has unlimited access, so there is
    # normally no reason to reset their records.
    # -----------------------------------------------------

    if target_user_id == BOT_OWNER_ID:

        await update.message.reply_text(
            "ℹ️ The bot owner already has unlimited access."
        )

        return

    try:

        result = await reset_user_limits_db(
            target_user_id
        )

        await update.message.reply_text(
            "✅ <b>Daily limits reset.</b>\n\n"
            f"User ID: "
            f"<code>{target_user_id}</code>\n"
            f"Removed rate-limit records: "
            f"<b>{result.deleted_count}</b>\n\n"
            f"Fresh allowance:\n"
            f"👥 Groups: <b>{GROUP_DAILY_LIMIT}/day</b> "
            f"per group\n"
            f"💬 Private: <b>{PRIVATE_DAILY_LIMIT}/day</b>",
            parse_mode="HTML",
        )

    except PyMongoError as e:

        print(
            f"Reset limit command error: {e}"
        )

        await update.message.reply_text(
            "❌ Failed to reset the user's limits."
        )


# =========================================================
# Owner: status
# =========================================================

async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    async with group_cache_lock:
        group_count = len(
            enabled_groups
        )

    await update.message.reply_text(
        "📊 <b>DoomFlix Bot Status</b>\n\n"
        f"Enabled groups: <b>{group_count}</b>\n"
        f"Group user limit: <b>{GROUP_DAILY_LIMIT}</b>/day\n"
        "Group admin limit: <b>Unlimited</b>\n"
        f"Private limit: <b>{PRIVATE_DAILY_LIMIT}</b>/day\n"
        f"Maximum results: <b>{MAX_RESULTS}</b>\n"
        "Force Subscribe: <b>Removed</b>",
        parse_mode="HTML",
    )

# =========================================================
# Get clickable group link
# =========================================================

async def get_group_link(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
):
    """
    Returns the best available clickable Telegram link.

    Priority:

        1. Public username
        2. Existing invite link
        3. Create an invite link if possible
        4. None if Telegram does not allow it
    """

    try:
        chat = await context.bot.get_chat(
            chat_id
        )

        # -------------------------------------------------
        # Public group/supergroup.
        # -------------------------------------------------

        username = getattr(
            chat,
            "username",
            None,
        )

        if username:
            return (
                f"https://t.me/{username}"
            )

        # -------------------------------------------------
        # Existing invite link.
        # -------------------------------------------------

        invite_link = getattr(
            chat,
            "invite_link",
            None,
        )

        if invite_link:
            return invite_link

        # -------------------------------------------------
        # Private group.
        #
        # Try creating an invite link.
        #
        # This requires the bot to have permission to
        # invite users.
        # -------------------------------------------------

        try:
            invite = (
                await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    name="DoomFlix Owner Link",
                )
            )

            return invite.invite_link

        except TelegramError as e:
            print(
                f"Could not create invite link "
                f"for group {chat_id}: {e}"
            )

    except TelegramError as e:
        print(
            f"Could not get group {chat_id}: {e}"
        )

    return None


# =========================================================
# Owner: list enabled groups
# =========================================================

async def groups_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    async with group_cache_lock:
        groups = list(
            enabled_groups
        )

    if not groups:

        await update.message.reply_text(
            "📋 No groups are currently enabled."
        )

        return

    groups.sort()

    lines = [
        "📋 <b>Enabled Groups</b>",
        "",
    ]

    for index, chat_id in enumerate(
        groups,
        start=1,
    ):

        # -------------------------------------------------
        # Get group information/link.
        # -------------------------------------------------

        try:
            chat = await context.bot.get_chat(
                chat_id
            )

            title = (
                getattr(
                    chat,
                    "title",
                    None,
                )
                or "Unknown Group"
            )

            title = html.escape(
                title
            )

        except TelegramError as e:

            print(
                f"Could not fetch group "
                f"{chat_id}: {e}"
            )

            title = "Unknown Group"

        link = await get_group_link(
            context,
            chat_id,
        )

        # -------------------------------------------------
        # Group entry.
        # -------------------------------------------------

        lines.append(
            f"{index}. <b>{title}</b>"
        )

        lines.append(
            f"   ID: <code>{chat_id}</code>"
        )

        if link:
            lines.append(
                f'   🔗 <a href="{html.escape(link, quote=True)}">'
                "Open Group</a>"
            )
        else:
            lines.append(
                "   🔗 Link unavailable"
            )

        lines.append("")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# =========================================================
# Error handler
# =========================================================

async def global_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error

    if error:

        print(
            "Unhandled Telegram error: "
            f"{type(error).__name__}: {error}"
        )


# =========================================================
# Main
# =========================================================

async def main():

    # -----------------------------------------------------
    # MongoDB connection test.
    # -----------------------------------------------------

    try:

        await asyncio.to_thread(
            mongo_client.admin.command,
            "ping",
        )

        print(
            "MongoDB connected."
        )

    except PyMongoError as e:

        mongo_client.close()

        raise RuntimeError(
            f"MongoDB connection failed: {e}"
        ) from e

    # -----------------------------------------------------
    # Load enabled groups.
    # -----------------------------------------------------

    await load_enabled_groups()

    # -----------------------------------------------------
    # Connect Telethon.
    # -----------------------------------------------------

    try:

        await telethon_client.connect()

        authorized = await (
            telethon_client.is_user_authorized()
        )

        if not authorized:

            await telethon_client.disconnect()
            mongo_client.close()

            raise RuntimeError(
                "SESSION_STRING is not authorized."
            )

        me = await telethon_client.get_me()

        print(
            "Telethon connected as "
            f"{getattr(me, 'username', None) or me.id}"
        )

    except Exception:

        try:

            if telethon_client.is_connected():
                await telethon_client.disconnect()

        except Exception:
            pass

        mongo_client.close()

        raise

    # -----------------------------------------------------
    # Telegram application.
    # -----------------------------------------------------

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # =====================================================
    # Normal commands
    # =====================================================

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "movie",
            movie_command,
        )
    )

    # =====================================================
    # Owner: group controls
    # =====================================================

    application.add_handler(
        CommandHandler(
            "enable",
            enable_group,
        )
    )

    application.add_handler(
        CommandHandler(
            "disable",
            disable_group,
        )
    )

    application.add_handler(
        CommandHandler(
            "groups",
            groups_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "status",
            status_command,
        )
    )

    # =====================================================
    # Owner: moderation
    # =====================================================

    application.add_handler(
        CommandHandler(
            "ban",
            ban_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "unban",
            unban_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "banlist",
            banlist_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "resetlimit",
            reset_limit_command,
        )
    )

    # =====================================================
    # Group text searches
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            group_text_search,
        )
    )

    # =====================================================
    # Global errors
    # =====================================================

    application.add_error_handler(
        global_error_handler
    )

    # =====================================================
    # Start bot
    # =====================================================

    try:

        await application.initialize()

        await application.start()

        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )

        print(
            "DoomFlix bot is running."
        )

        await telethon_client.run_until_disconnected()

    except KeyboardInterrupt:

        print(
            "Shutdown requested."
        )

    finally:

        # -------------------------------------------------
        # Stop polling.
        # -------------------------------------------------

        try:

            if application.updater.running:
                await application.updater.stop()

        except Exception as e:

            print(
                f"Updater shutdown error: {e}"
            )

        # -------------------------------------------------
        # Stop application.
        # -------------------------------------------------

        try:

            if application.running:
                await application.stop()

        except Exception as e:

            print(
                f"Application stop error: {e}"
            )

        # -------------------------------------------------
        # Shutdown application.
        # -------------------------------------------------

        try:

            await application.shutdown()

        except Exception as e:

            print(
                f"Application shutdown error: {e}"
            )

        # -------------------------------------------------
        # Disconnect Telethon.
        # -------------------------------------------------

        try:

            if telethon_client.is_connected():
                await telethon_client.disconnect()

        except Exception as e:

            print(
                f"Telethon shutdown error: {e}"
            )

        # -------------------------------------------------
        # Close MongoDB.
        # -------------------------------------------------

        try:

            mongo_client.close()

        except Exception as e:

            print(
                f"MongoDB close error: {e}"
            )

        print(
            "Shutdown complete."
        )


# =========================================================
# Entry point
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
