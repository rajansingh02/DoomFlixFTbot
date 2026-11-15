"""
Actually searching the configured channel and delivering results.

Both entity lookups go through channel_utils so the configured source
channel can be a PRIVATE channel (numeric ID) just as well as a public
@username — Telethon needs a resolved Peer for private channels, and
python-telegram-bot needs an int chat_id rather than a numeric string.
"""

import html
import re
from urllib.parse import quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from telethon.errors import FloodWaitError, RPCError
from pymongo.errors import PyMongoError

from channel_utils import channel_for_bot_api, channel_for_telethon
from config import MAX_RESULTS, telethon_client
from db import get_configured_channel, get_ignored_terms
from ignore_filter import normalize_search_text, remove_ignored_terms
from security import make_movie_deep_link


MAX_SEARCH_CANDIDATES = MAX_RESULTS * 10


async def send_movie_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_id: int,
):
    """Forward the exact selected channel post. The movie is NOT re-searched."""

    chat = update.effective_chat
    if not chat:
        return

    channel = get_configured_channel()

    try:
        telethon_peer = channel_for_telethon(channel)

        message = await telethon_client.get_messages(
            telethon_peer,
            ids=message_id,
        )

        if not message:
            await update.effective_message.reply_text(
                "❌ This movie post is no longer available."
            )
            return

        bot_chat_id = channel_for_bot_api(channel)

        await context.bot.forward_message(
            chat_id=chat.id,
            from_chat_id=bot_chat_id,
            message_id=message_id,
        )

    except FloodWaitError as e:
        print(f"Telegram flood wait: {e.seconds}s")
        try:
            await update.effective_message.reply_text(
                "⏳ Telegram is temporarily rate-limiting requests.\n\n"
                f"Please try again in about {e.seconds} seconds."
            )
        except TelegramError:
            pass

    except TelegramError as e:
        print(f"Movie forwarding error message={message_id}: {e}")
        try:
            await update.effective_message.reply_text(
                "⚠️ I couldn't send this movie right now.\n\n"
                "The original movie post may have been removed or is "
                "temporarily unavailable. If the source channel is "
                "private, make sure the bot is still an admin member "
                "of it."
            )
        except TelegramError:
            pass

    except RPCError as e:
        print(f"Telethon movie lookup error: {e}")
        try:
            await update.effective_message.reply_text(
                "⚠️ I couldn't retrieve this movie post."
            )
        except TelegramError:
            pass

    except Exception as e:
        print(f"Unexpected movie delivery error: {e}")
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong while sending the movie."
            )
        except TelegramError:
            pass


async def perform_movie_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
):
    query = re.sub(r"\s+", " ", query.strip())

    if not query:
        return

    if len(query) > 200:
        query = query[:200].strip()

    status_message = None
    channel = get_configured_channel()

    try:
        ignored_terms = await get_ignored_terms()
        query = remove_ignored_terms(query, ignored_terms)

        # Silently stop if nothing remains after removing ignored terms.
        if not query:
            return

        try:
            status_message = await update.effective_message.reply_text(
                "🔎 Searching..."
            )
        except TelegramError:
            pass

        telethon_peer = channel_for_telethon(channel)

        search_queries = [query]
        normalized_query = normalize_search_text(query)

        if normalized_query and normalized_query.casefold() != query.casefold():
            # Telegram searches server-side; retry punctuation-normalized
            # text rather than scanning the channel locally.
            search_queries.append(normalized_query)

        candidates = {}

        for search_query in search_queries:
            async for message in telethon_client.iter_messages(
                telethon_peer,
                search=search_query,
                limit=MAX_RESULTS,
            ):
                if not message or not message.id:
                    continue

                # Ignore service messages.
                if message.action:
                    continue

                text = (getattr(message, "message", None) or "").strip()
                if not text:
                    continue

                candidates[message.id] = message

        # Merge newest-first server-side searches before applying the
        # existing result limit.
        results = sorted(
            candidates.values(),
            key=lambda message: message.id,
            reverse=True,
        )[:MAX_RESULTS]

    except FloodWaitError as e:
        print(f"Search flood wait: {e.seconds}s")
        if status_message:
            try:
                await status_message.edit_text(
                    "⏳ Telegram is temporarily rate-limiting searches.\n\n"
                    f"Try again in about {e.seconds} seconds."
                )
            except TelegramError:
                pass
        return

    except PyMongoError as e:
        print(f"Search ignore-list database error: {e}")
        if status_message:
            try:
                await status_message.edit_text(
                    "⚠️ Movie search is temporarily unavailable."
                )
            except TelegramError:
                pass
        return

    except RPCError as e:
        print(f"Telethon search error: {e}")
        if status_message:
            try:
                await status_message.edit_text(
                    "⚠️ Movie search is temporarily unavailable."
                )
            except TelegramError:
                pass
        return

    except Exception as e:
        print(f"Unexpected search error: {e}")
        if status_message:
            try:
                await status_message.edit_text(
                    "⚠️ Something went wrong while searching."
                )
            except TelegramError:
                pass
        return

    if not results:
        safe_query = html.escape(query)
        text = (
            "🎬 <b>Movie Search</b>\n\n"
            f"No matching movie found for: <b>“{safe_query}”</b>.\n\n"
            "⚠️ Make sure your movie title is correct.\n\n"
            "📩 Movie not showing? Send "
            "<code>#request [Movie Title] [YEAR]</code>\n"
            "It’ll be uploaded if available."
        )

        google_url = (
            "https://www.google.com/search?q=" + quote_plus(query)
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Search on Google ↗", url=google_url)
        ]])

        if status_message:
            try:
                await status_message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except TelegramError:
                pass
        return

    buttons = []
    requester = update.effective_user
    group = update.effective_chat

    if not requester or not group:
        if status_message:
            try:
                await status_message.edit_text(
                    "⚠️ Search results were unavailable."
                )
            except TelegramError:
                pass
        return

    # Normal searches authorize the person who issued the command.
    authorized_user_id = requester.id

    # When replying to another human user's message, authorize that
    # person instead. If the reply is to a bot, channel post, anonymous
    # admin, or a message without a normal user sender, keep the default.
    source_message = update.effective_message
    replied_message = (
        source_message.reply_to_message if source_message else None
    )

    if (
        replied_message
        and replied_message.from_user
        and not replied_message.from_user.is_bot
    ):
        authorized_user_id = replied_message.from_user.id

    for message in results:
        title = (message.message or "").strip()
        if not title:
            continue

        if len(title) > 60:
            title = title[:57] + "..."

        deep_link = make_movie_deep_link(
            message_id=message.id,
            group_id=group.id,
            requester_user_id=authorized_user_id,
        )

        buttons.append([
            InlineKeyboardButton(title, url=deep_link)
        ])

    if not buttons:
        if status_message:
            try:
                await status_message.edit_text(
                    "⚠️ Search results were unavailable."
                )
            except TelegramError:
                pass
        return

    safe_query = html.escape(query)

    result_text = (
        "🎬 <b>Movie Search</b>\n\n"
        f"Found <b>{len(buttons)}</b> results for <b>“{safe_query}”</b>\n"
        "⚠️ Incorrect result? Make sure your movie title is correct.\n\n"
        "📩 Movie not showing? Send "
        "<code>#request [Movie Title] [YEAR]</code>\n"
        "It’ll be uploaded if available.\n\n"
        "👇 Select a movie below:"
    )

    if status_message:
        try:
            await status_message.delete()
        except TelegramError:
            pass

    try:
        await update.effective_message.reply_text(
            result_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_to_message_id=update.effective_message.message_id,
        )
    except TelegramError as e:
        print(f"Result message error: {e}")
