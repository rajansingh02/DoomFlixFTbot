"""
Single-worker FIFO movie-search queue.

Exactly one search runs at a time; up to MAX_SEARCH_QUEUE requests may
wait. A normal user's daily search limit is only ever consumed AFTER
a waiting position is successfully reserved, so a full queue never
silently burns someone's daily allowance.

EDGE CASE FIX: the original implementation released the reserved
waiting-queue position inside `except` blocks only. When a normal
user's daily limit was exhausted, the function returned `False`
directly (no exception raised), which skipped both `except` clauses
and the reservation was never released — the slot leaked permanently.
Repeated over time this made `waiting_search_count` drift upward until
the bot reported "queue full" even with nothing actually queued. Fixed
below by releasing in a `finally` block whenever ownership of the slot
was not transferred to the queue.
"""

import asyncio

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import GROUP_DAILY_LIMIT, MAX_SEARCH_QUEUE
from db import consume_daily_limit

search_queue = asyncio.Queue()

waiting_search_lock = asyncio.Lock()
waiting_search_count = 0


async def release_waiting_search_slot() -> None:
    """Release exactly one reserved waiting-queue position."""
    global waiting_search_count
    async with waiting_search_lock:
        if waiting_search_count > 0:
            waiting_search_count -= 1
        else:
            waiting_search_count = 0


async def get_waiting_search_count() -> int:
    async with waiting_search_lock:
        return waiting_search_count


async def send_search_queue_full_message(update: Update):
    try:
        await update.effective_message.reply_text(
            "⏳ The movie search queue is currently full.\n\n"
            f"Only {MAX_SEARCH_QUEUE} searches can wait at once while "
            "one search is being processed.\n\nPlease try again shortly."
        )
    except TelegramError:
        pass


async def enqueue_movie_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    access_type: str,
) -> bool:
    """
    Admit a movie search into the FIFO queue.

        1. Reserve a waiting position (reject if the queue is full).
        2. Only admitted requests consume the daily limit — and only
           normal ("user") requests are limited at all.
        3. Put the request into the FIFO queue.
        4. The single worker processes jobs one at a time.

    Returns True if admitted, False otherwise. In every False case,
    the reserved waiting position (if any) is released.
    """

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return False

    global waiting_search_count

    async with waiting_search_lock:
        if waiting_search_count >= MAX_SEARCH_QUEUE:
            acquired = False
        else:
            waiting_search_count += 1
            acquired = True

    if not acquired:
        await send_search_queue_full_message(update)
        return False

    # From here, this request owns a queue position until either it's
    # handed off to the queue (`position_reserved = False`) or this
    # function returns/raises, at which point `finally` reclaims it.
    position_reserved = True

    try:
        if access_type == "user":
            allowed = await consume_daily_limit(
                user_id=user.id,
                chat_id=chat.id,
                limit=GROUP_DAILY_LIMIT,
            )

            if not allowed:
                try:
                    await update.effective_message.reply_text(
                        "⛔ You have reached your daily group limit of "
                        f"{GROUP_DAILY_LIMIT} searches.\n\n"
                        "Try again tomorrow."
                    )
                except TelegramError:
                    pass
                return False

        await search_queue.put(
            {
                "update": update,
                "context": context,
                "query": query,
            }
        )

        # Ownership of the waiting position transfers to the queued
        # job; the worker releases it when the job becomes active.
        position_reserved = False
        return True

    except asyncio.CancelledError:
        raise

    except Exception as e:
        print(f"Could not enqueue movie search: {e}")
        try:
            await update.effective_message.reply_text(
                "⚠️ I couldn't add your search to the queue. "
                "Please try again."
            )
        except TelegramError:
            pass
        return False

    finally:
        if position_reserved:
            await release_waiting_search_slot()


async def movie_search_worker(perform_movie_search):
    """
    Single FIFO search worker. Exactly ONE search executes at a time.

    `perform_movie_search` is injected (rather than imported directly)
    to avoid a circular import between this module and movie_search.py.
    """

    print("Movie search worker started.")

    while True:
        job = await search_queue.get()

        # The waiting slot is freed as soon as the job leaves the
        # queue — the worker is now processing it.
        await release_waiting_search_slot()

        try:
            await perform_movie_search(
                job["update"],
                job["context"],
                job["query"],
            )

        except asyncio.CancelledError:
            raise

        except Exception as e:
            print(f"Movie search worker error: {type(e).__name__}: {e}")

            try:
                update = job.get("update")
                if update and update.effective_message:
                    await update.effective_message.reply_text(
                        "⚠️ Something went wrong while processing "
                        "your movie search."
                    )
            except Exception:
                pass

        finally:
            search_queue.task_done()
