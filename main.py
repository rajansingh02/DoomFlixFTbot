import asyncio

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from pymongo.errors import PyMongoError

from config import BOT_TOKEN, MAX_SEARCH_QUEUE, ensure_indexes, mongo_client, telethon_client
from db import load_configured_channel, load_enabled_groups
from handlers_owner import (
    ban_command,
    banlist_command,
    channel_command,
    disable_group,
    enable_group,
    groups_command,
    reset_limit_command,
    set_channel_command,
    status_command,
    unban_command,
)
from handlers_user import group_text_search, movie_command, start
from movie_search import perform_movie_search
from search_queue import movie_search_worker

search_worker_task = None


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if error:
        print(f"Unhandled Telegram error: {type(error).__name__}: {error}")


async def main():
    global search_worker_task

    # -----------------------------------------------------
    # MongoDB connection test + indexes.
    # -----------------------------------------------------
    try:
        await asyncio.to_thread(mongo_client.admin.command, "ping")
        print("MongoDB connected.")
        ensure_indexes()

    except PyMongoError as e:
        mongo_client.close()
        raise RuntimeError(f"MongoDB connection failed: {e}") from e

    # -----------------------------------------------------
    # Load enabled groups and the configured source channel.
    # -----------------------------------------------------
    await load_enabled_groups()
    await load_configured_channel()

    # -----------------------------------------------------
    # Connect Telethon.
    # -----------------------------------------------------
    try:
        await telethon_client.connect()

        authorized = await telethon_client.is_user_authorized()

        if not authorized:
            await telethon_client.disconnect()
            mongo_client.close()
            raise RuntimeError("SESSION_STRING is not authorized.")

        me = await telethon_client.get_me()
        print(f"Telethon connected as {getattr(me, 'username', None) or me.id}")

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
    application = Application.builder().token(BOT_TOKEN).build()

    # Normal commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("movie", movie_command))
    application.add_handler(CommandHandler("setchannel", set_channel_command))
    application.add_handler(CommandHandler("channel", channel_command))

    # Owner: group controls
    application.add_handler(CommandHandler("enable", enable_group))
    application.add_handler(CommandHandler("disable", disable_group))
    application.add_handler(CommandHandler("groups", groups_command))
    application.add_handler(CommandHandler("status", status_command))

    # Owner: moderation
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("banlist", banlist_command))
    application.add_handler(CommandHandler("resetlimit", reset_limit_command))

    # Group text searches
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, group_text_search)
    )

    application.add_error_handler(global_error_handler)

    # -----------------------------------------------------
    # Start bot.
    # -----------------------------------------------------
    try:
        await application.initialize()
        await application.start()

        await application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )

        # Start EXACTLY ONE movie search worker, after polling starts.
        search_worker_task = asyncio.create_task(
            movie_search_worker(perform_movie_search),
            name="movie-search-worker",
        )

        print("DoomFlix bot is running.")
        print(f"Movie search queue: 1 active + {MAX_SEARCH_QUEUE} waiting.")

        await telethon_client.run_until_disconnected()

    except KeyboardInterrupt:
        print("Shutdown requested.")

    finally:
        try:
            if application.updater.running:
                await application.updater.stop()
        except Exception as e:
            print(f"Updater shutdown error: {e}")

        try:
            if application.running:
                await application.stop()
        except Exception as e:
            print(f"Application stop error: {e}")

        # Searches waiting in memory are intentionally discarded on
        # shutdown/restart — they cannot safely survive a process
        # restart without persisting jobs to MongoDB/Redis.
        if search_worker_task:
            try:
                search_worker_task.cancel()
                await search_worker_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Search worker shutdown error: {e}")
            finally:
                search_worker_task = None

        try:
            await application.shutdown()
        except Exception as e:
            print(f"Application shutdown error: {e}")

        try:
            if telethon_client.is_connected():
                await telethon_client.disconnect()
        except Exception as e:
            print(f"Telethon shutdown error: {e}")

        try:
            mongo_client.close()
        except Exception as e:
            print(f"MongoDB close error: {e}")

        print("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
