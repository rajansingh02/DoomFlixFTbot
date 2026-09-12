"""
Owner-only administration commands.
"""

import html

from telegram import Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from pymongo.errors import PyMongoError
from telethon.errors import RPCError

from access_control import get_target_user_id, is_bot_owner
from channel_utils import (
    ChannelResolutionError,
    canonical_channel_string,
    channel_for_bot_api,
    resolve_channel_reference,
)
from config import (
    BOT_OWNER_ID,
    GROUP_DAILY_LIMIT,
    MAX_RESULTS,
    PRIVATE_DAILY_LIMIT,
)
from config import telethon_client
from db import (
    ban_user_db,
    disable_group_cache,
    enable_group_cache,
    enabled_groups,
    get_cached_group_link,
    get_configured_channel,
    group_cache_lock,
    list_banned_users,
    reset_user_limits_db,
    set_cached_group_link,
    set_configured_channel,
    unban_user_db,
)
from messaging_utils import send_chunked_html
from search_queue import get_waiting_search_count
from config import MAX_SEARCH_QUEUE


# =========================================================
# Owner: enable / disable group
# =========================================================

async def enable_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message or not update.effective_chat:
        return

    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text(
            "❌ Run /enable inside the group you want to enable."
        )
        return

    try:
        await enable_group_cache(chat.id)

        await update.message.reply_text(
            "✅ <b>DoomFlix has been enabled in this group.</b>\n\n"
            f"👤 Normal users: {GROUP_DAILY_LIMIT} searches/day\n"
            "👮 Group admins: unlimited\n"
            "👑 Bot owner: unlimited",
            parse_mode="HTML",
        )

    except PyMongoError as e:
        print(f"Enable group error: {e}")
        await update.message.reply_text(
            "❌ I couldn't enable the bot in this group."
        )


async def disable_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message or not update.effective_chat:
        return

    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await update.message.reply_text("❌ Run /disable inside the group.")
        return

    try:
        await disable_group_cache(chat.id)

        await update.message.reply_text(
            "✅ <b>DoomFlix has been disabled in this group.</b>",
            parse_mode="HTML",
        )

    except PyMongoError as e:
        print(f"Disable group error: {e}")
        await update.message.reply_text(
            "❌ I couldn't disable the bot in this group."
        )


# =========================================================
# Owner: moderation
# =========================================================

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    target_user_id = get_target_user_id(update, context)

    if target_user_id is None:
        await update.message.reply_text(
            "Usage:\n<code>/ban USER_ID</code>\n\n"
            "Or reply to a user's message with <code>/ban</code>.",
            parse_mode="HTML",
        )
        return

    if target_user_id == BOT_OWNER_ID:
        await update.message.reply_text("❌ You cannot ban the bot owner.")
        return

    try:
        await ban_user_db(target_user_id, banned_by=BOT_OWNER_ID)

        await update.message.reply_text(
            "🚫 <b>User banned successfully.</b>\n\n"
            f"User ID: <code>{target_user_id}</code>",
            parse_mode="HTML",
        )

    except PyMongoError as e:
        print(f"Ban command error: {e}")
        await update.message.reply_text("❌ Failed to ban the user.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    target_user_id = get_target_user_id(update, context)

    if target_user_id is None:
        await update.message.reply_text(
            "Usage:\n<code>/unban USER_ID</code>\n\n"
            "Or reply to a user's message with <code>/unban</code>.",
            parse_mode="HTML",
        )
        return

    try:
        result = await unban_user_db(target_user_id)

        if result.deleted_count:
            await update.message.reply_text(
                "✅ <b>User unbanned.</b>\n\n"
                f"User ID: <code>{target_user_id}</code>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text("ℹ️ That user is not banned.")

    except PyMongoError as e:
        print(f"Unban command error: {e}")
        await update.message.reply_text("❌ Failed to unban the user.")


async def banlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    try:
        users = await list_banned_users()
    except PyMongoError as e:
        print(f"Ban list error: {e}")
        await update.message.reply_text("❌ Failed to load the ban list.")
        return

    if not users:
        await update.message.reply_text(
            "📋 <b>Ban List</b>\n\nNo users are currently banned.",
            parse_mode="HTML",
        )
        return

    lines = [
        "📋 <b>Banned Users</b>",
        "",
        f"Total: <b>{len(users)}</b>",
        "",
    ]

    for index, user in enumerate(users, start=1):
        user_id = user.get("user_id")
        if user_id is None:
            continue
        lines.append(f"{index}. <code>{user_id}</code>")

    await send_chunked_html(update.message, lines)


async def reset_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage: /resetlimit USER_ID (or reply to a user's message).
    Clears the user's rate-limit records across all chats.
    """

    if not is_bot_owner(update):
        return

    if not update.message:
        return

    target_user_id = get_target_user_id(update, context)

    if target_user_id is None:
        await update.message.reply_text(
            "Usage:\n<code>/resetlimit USER_ID</code>\n\n"
            "Or reply to a user's message with <code>/resetlimit</code>.",
            parse_mode="HTML",
        )
        return

    if target_user_id == BOT_OWNER_ID:
        await update.message.reply_text(
            "ℹ️ The bot owner already has unlimited access."
        )
        return

    try:
        result = await reset_user_limits_db(target_user_id)

        await update.message.reply_text(
            "✅ <b>Daily limits reset.</b>\n\n"
            f"User ID: <code>{target_user_id}</code>\n"
            f"Removed rate-limit records: <b>{result.deleted_count}</b>\n\n"
            f"Fresh allowance:\n"
            f"👥 Groups: <b>{GROUP_DAILY_LIMIT}/day</b> per group\n"
            f"💬 Private: <b>{PRIVATE_DAILY_LIMIT}/day</b>",
            parse_mode="HTML",
        )

    except PyMongoError as e:
        print(f"Reset limit command error: {e}")
        await update.message.reply_text("❌ Failed to reset the user's limits.")


# =========================================================
# Owner: single movie source channel (public OR private)
# =========================================================

async def set_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n<code>/setchannel @channelusername</code>\n"
            "or:\n<code>/setchannel -1001234567890</code>\n"
            "or a private invite link:\n"
            "<code>/setchannel https://t.me/+AbCdEfGhIjK</code>\n\n"
            "Only one movie source channel can be configured at a time. "
            "Private channels are supported as long as the search "
            "account is already a member.",
            parse_mode="HTML",
        )
        return

    raw_channel = context.args[0].strip()

    try:
        entity = await resolve_channel_reference(telethon_client, raw_channel)
    except ChannelResolutionError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    except RPCError as e:
        print(f"Unexpected channel resolution error: {e}")
        await update.message.reply_text(
            "❌ I couldn't validate that channel right now. Please try again."
        )
        return

    if not getattr(entity, "broadcast", False):
        await update.message.reply_text(
            "❌ That is not a Telegram channel. Please provide a channel."
        )
        return

    try:
        canonical = canonical_channel_string(entity)
    except ChannelResolutionError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    is_private = not canonical.startswith("@")

    # -------------------------------------------------
    # Verify the bot API can access the channel too (needed for
    # forwarding selected movie posts). For private channels this
    # means the BOT must already be an admin member — it cannot
    # join a private channel on its own.
    # -------------------------------------------------
    try:
        await context.bot.get_chat(channel_for_bot_api(canonical))
    except TelegramError as e:
        print(f"Bot API channel access error for {canonical}: {e}")
        extra = (
            " Since this is a private channel, add the bot to it "
            "directly (invite links won't work for bots) as an "
            "administrator."
            if is_private
            else ""
        )
        await update.message.reply_text(
            "❌ The bot cannot access that channel for forwarding.\n\n"
            "Add the bot to the channel (normally as an administrator) "
            f"and try again.{extra}"
        )
        return

    try:
        await set_configured_channel(canonical)
    except PyMongoError as e:
        print(f"Set channel database error: {e}")
        await update.message.reply_text(
            "❌ The channel was validated but could not be saved."
        )
        return
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    kind = "🔒 private" if is_private else "🌐 public"

    await update.message.reply_text(
        "✅ <b>Movie source channel updated.</b>\n\n"
        f"📺 Channel ({kind}): <code>{html.escape(canonical)}</code>\n\n"
        "There is only one active movie source channel.\n"
        "New searches and movie links will use this channel.",
        parse_mode="HTML",
    )


async def channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    channel = get_configured_channel()
    kind = "🔒 private" if not channel.startswith("@") else "🌐 public"

    await update.message.reply_text(
        "📺 <b>Movie Source Channel</b>\n\n"
        f"Type: {kind}\n"
        f"<code>{html.escape(str(channel))}</code>\n\n"
        "Only one channel is configured at a time.",
        parse_mode="HTML",
    )


# =========================================================
# Owner: status
# =========================================================

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    async with group_cache_lock:
        group_count = len(enabled_groups)

    waiting = await get_waiting_search_count()

    await update.message.reply_text(
        "📊 <b>DoomFlix Bot Status</b>\n\n"
        f"Enabled groups: <b>{group_count}</b>\n"
        f"Group user limit: <b>{GROUP_DAILY_LIMIT}</b>/day\n"
        "Group admin limit: <b>Unlimited</b>\n"
        f"Private limit: <b>{PRIVATE_DAILY_LIMIT}</b>/day\n"
        f"Maximum results: <b>{MAX_RESULTS}</b>\n"
        f"Movie channel: <code>{html.escape(str(get_configured_channel()))}</code>\n"
        f"Searches waiting: <b>{waiting}</b>/{MAX_SEARCH_QUEUE}\n"
        "Searches running simultaneously: <b>1</b>\n"
        "Force Subscribe: <b>Removed</b>",
        parse_mode="HTML",
    )


# =========================================================
# Owner: list enabled groups (with cached invite links)
# =========================================================

async def get_group_link(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """
    Best available clickable link, in priority order:
        1. Public username
        2. Cached invite link (from a previous /groups run)
        3. Telegram-reported invite_link on the Chat object
        4. Newly created invite link (cached for next time)
        5. None
    """

    try:
        chat = await context.bot.get_chat(chat_id)

        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}"

        cached = await get_cached_group_link(chat_id)
        if cached:
            return cached

        invite_link = getattr(chat, "invite_link", None)
        if invite_link:
            await set_cached_group_link(chat_id, invite_link)
            return invite_link

        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                name="DoomFlix Owner Link",
            )
            await set_cached_group_link(chat_id, invite.invite_link)
            return invite.invite_link

        except TelegramError as e:
            print(f"Could not create invite link for group {chat_id}: {e}")

    except TelegramError as e:
        print(f"Could not get group {chat_id}: {e}")

    return None


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_bot_owner(update):
        return

    if not update.message:
        return

    async with group_cache_lock:
        groups = sorted(enabled_groups)

    if not groups:
        await update.message.reply_text("📋 No groups are currently enabled.")
        return

    lines = ["📋 <b>Enabled Groups</b>", ""]

    for index, chat_id in enumerate(groups, start=1):
        try:
            chat = await context.bot.get_chat(chat_id)
            title = html.escape(getattr(chat, "title", None) or "Unknown Group")
        except TelegramError as e:
            print(f"Could not fetch group {chat_id}: {e}")
            title = "Unknown Group"

        link = await get_group_link(context, chat_id)

        lines.append(f"{index}. <b>{title}</b>")
        lines.append(f"   ID: <code>{chat_id}</code>")

        if link:
            lines.append(
                f'   🔗 <a href="{html.escape(link, quote=True)}">Open Group</a>'
            )
        else:
            lines.append("   🔗 Link unavailable")

        lines.append("")

    await send_chunked_html(update.message, lines, disable_web_page_preview=True)
