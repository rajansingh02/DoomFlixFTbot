"""
Who is allowed to do what, and consuming daily limits where appropriate.

Note the split between `get_group_access_type` (checks everything
EXCEPT the daily search limit) and `check_group_access` /
`enqueue_movie_search` (which do consume it). Queue-full requests must
never burn a user's daily allowance — see search_queue.py.
"""

from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import BOT_OWNER_ID, GROUP_DAILY_LIMIT, PRIVATE_DAILY_LIMIT
from db import consume_daily_limit, is_group_enabled, is_user_banned


def is_bot_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == BOT_OWNER_ID)


def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == ChatType.PRIVATE)


def is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(
        chat and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    )


def get_target_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Supports:
        /command 123456789
    or:
        reply to user's message
        /command
    """

    message = update.message
    if not message:
        return None

    reply = message.reply_to_message
    if reply and reply.from_user:
        return reply.from_user.id

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
        return member.status in ("administrator", "creator")

    except TelegramError as e:
        print(f"Admin check failed user={user_id} group={chat_id}: {e}")
        # Fail closed.
        return False


async def check_private_access(update: Update) -> bool:
    """
    Private movie delivery:
        owner  -> unlimited
        banned -> blocked
        user   -> PRIVATE_DAILY_LIMIT/day
    """

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    user_id = user.id

    if user_id == BOT_OWNER_ID:
        return True

    if await is_user_banned(user_id):
        try:
            await update.effective_message.reply_text(
                "🚫 You are banned from using this bot."
            )
        except TelegramError:
            pass
        return False

    allowed = await consume_daily_limit(
        user_id=user_id,
        chat_id=chat.id,
        limit=PRIVATE_DAILY_LIMIT,
    )

    if not allowed:
        try:
            await update.effective_message.reply_text(
                "⛔ You have reached your daily private limit of "
                f"{PRIVATE_DAILY_LIMIT} requests.\n\nTry again tomorrow."
            )
        except TelegramError:
            pass
        return False

    return True


async def get_group_access_type(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    """
    Checks everything EXCEPT the daily search limit.

    Returns "owner" / "admin" / "user" / None.
    """

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return None

    if not await is_group_enabled(chat.id):
        return None

    if user.id == BOT_OWNER_ID:
        return "owner"

    if await is_user_banned(user.id):
        try:
            await update.effective_message.reply_text(
                "🚫 You are banned from using this bot."
            )
        except TelegramError:
            pass
        return None

    if await is_group_admin(context=context, chat_id=chat.id, user_id=user.id):
        return "admin"

    # Daily limit is deliberately NOT consumed here.
    return "user"


async def check_group_access(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    """
    Kept for any non-search command that needs a simple yes/no group
    gate. Search handlers use the queue-aware admission function in
    search_queue.py instead, since that one must not charge a daily
    request when the queue is full.
    """

    access_type = await get_group_access_type(update, context)

    if access_type is None:
        return False

    if access_type in ("owner", "admin"):
        return True

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    allowed = await consume_daily_limit(
        user_id=user.id,
        chat_id=chat.id,
        limit=GROUP_DAILY_LIMIT,
    )

    if not allowed:
        try:
            await update.effective_message.reply_text(
                "⛔ You have reached your daily group limit of "
                f"{GROUP_DAILY_LIMIT} searches.\n\nTry again tomorrow."
            )
        except TelegramError:
            pass
        return False

    return True
