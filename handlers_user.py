"""
/start, /movie, and the "movie <title>" plain-text group trigger.
"""

import re

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from access_control import check_private_access, get_group_access_type, is_bot_owner
from db import is_group_enabled, is_user_banned
from movie_search import send_movie_post
from search_queue import enqueue_movie_search
from security import parse_movie_payload

_MOVIE_TEXT_RE = re.compile(r"^movie\s+(.+?)\s*$", re.IGNORECASE)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.message or not update.effective_user:
        return

    chat = update.effective_chat

    # =====================================================
    # /start WITHOUT payload
    # =====================================================
    if not context.args:
        if chat.type == ChatType.PRIVATE:
            if not is_bot_owner(update) and await is_user_banned(
                update.effective_user.id
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

        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            # /start help is not a search — must not consume the daily limit.
            if not await is_group_enabled(chat.id):
                return

            access_type = await get_group_access_type(update, context)
            if access_type is None:
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
    movie_data = parse_movie_payload(payload)

    if not movie_data:
        await update.message.reply_text("⚠️ Invalid movie link.")
        return

    message_id = movie_data["message_id"]

    if chat.type != ChatType.PRIVATE:
        await update.message.reply_text(
            "⚠️ Please open the movie link in a private chat with the bot."
        )
        return

    if not await check_private_access(update):
        return

    await send_movie_post(update=update, context=context, message_id=message_id)


async def movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_chat or not update.effective_user:
        return

    chat = update.effective_chat

    if chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "🎬 Movie search is available from enabled groups only."
        )
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not await is_group_enabled(chat.id):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage:\n<code>/movie Movie Name</code>",
            parse_mode="HTML",
        )
        return

    query = " ".join(context.args).strip()
    if not query:
        return

    if len(query) > 200:
        query = query[:200].strip()

    access_type = await get_group_access_type(update, context)
    if access_type is None:
        return

    await enqueue_movie_search(
        update=update,
        context=context,
        query=query,
        access_type=access_type,
    )


async def group_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group searches: "movie <title>" (case-insensitive)."""

    if not update.message or not update.effective_chat or not update.effective_user:
        return

    chat = update.effective_chat

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    match = _MOVIE_TEXT_RE.match(text)
    if not match:
        return

    if not await is_group_enabled(chat.id):
        return

    query = match.group(1).strip()
    if not query:
        return

    if len(query) > 200:
        query = query[:200].strip()

    access_type = await get_group_access_type(update, context)
    if access_type is None:
        return

    await enqueue_movie_search(
        update=update,
        context=context,
        query=query,
        access_type=access_type,
    )
