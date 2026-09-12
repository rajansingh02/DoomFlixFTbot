"""
Environment configuration, constants, and raw client objects.

This module intentionally contains NO business logic (no rate limiting,
no group cache, no channel resolution). Everything else imports the
clients/constants it needs from here.
"""

import hashlib
import os

from dotenv import load_dotenv
from pymongo import MongoClient

from telethon import TelegramClient
from telethon.sessions import StringSession

# =========================================================
# Environment configuration
# =========================================================

load_dotenv()

print(
    "RAILWAY BOT_TOKEN CHECK:",
    bool(os.getenv("BOT_TOKEN")),
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
MONGODB_URI = os.environ["MONGODB_URI"]
BOT_OWNER_ID = int(os.environ["BOT_OWNER_ID"])

BOT_USERNAME = "DoomflixFTbot"


# =========================================================
# Limits / defaults
# =========================================================

GROUP_DAILY_LIMIT = 10
PRIVATE_DAILY_LIMIT = 20
MAX_RESULTS = 10

# One active search + this many waiting.
MAX_SEARCH_QUEUE = 10

# Used only when no channel has ever been configured.
# NOTE: this must be a channel the Telethon account can already see.
CHANNEL_DEFAULT = "@doomflixmovies"

MAX_MOVIE_PAYLOAD_LENGTH = 80

# Telegram's hard cap on a single message is 4096 characters.
# Leave headroom for HTML tags/entities that expand on render.
TELEGRAM_MESSAGE_SAFE_LIMIT = 3500


# =========================================================
# Deep-link security
# =========================================================

DEEP_LINK_SECRET = hashlib.sha256(
    BOT_TOKEN.encode("utf-8")
).digest()


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
bot_settings = db["bot_settings"]


def ensure_indexes():
    """
    Create required MongoDB indexes. Call once at startup.
    """

    from pymongo.errors import PyMongoError

    try:
        rate_limits.create_index(
            [
                ("user_id", 1),
                ("chat_id", 1),
                ("date", 1),
            ],
            unique=True,
            background=True,
            name="user_chat_date_unique",
        )

        group_config.create_index(
            [("chat_id", 1)],
            unique=True,
            background=True,
            name="chat_id_unique",
        )

        banned_users.create_index(
            [("user_id", 1)],
            unique=True,
            background=True,
            name="user_id_unique",
        )

        # DO NOT create an index on _id — MongoDB creates a unique
        # _id index automatically. Doing so manually raises
        # InvalidIndexSpecificationOption.

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
