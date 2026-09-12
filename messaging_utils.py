"""
EDGE CASE FIX: banlist_command and groups_command originally built one
big `reply_text` from an unbounded loop over all banned users / all
enabled groups. Once that text passes Telegram's ~4096 character hard
limit, `reply_text` raises, the call sites didn't catch it, and the
owner silently got no response (swallowed by the global error
handler). `send_chunked_html` below splits on line boundaries so a
single long list becomes several messages instead of failing outright.
"""

from telegram.error import TelegramError

from config import TELEGRAM_MESSAGE_SAFE_LIMIT


async def send_chunked_html(message, lines, *, disable_web_page_preview=False):
    """
    Send a list of pre-formatted HTML lines as one or more messages,
    each kept under TELEGRAM_MESSAGE_SAFE_LIMIT characters. Splits are
    only made between lines, never mid-line, so tags never get cut.
    """

    chunks = []
    current = []
    current_len = 0

    for line in lines:
        # +1 accounts for the joining newline.
        added_len = len(line) + 1

        if current and current_len + added_len > TELEGRAM_MESSAGE_SAFE_LIMIT:
            chunks.append("\n".join(current))
            current = []
            current_len = 0

        current.append(line)
        current_len += added_len

    if current:
        chunks.append("\n".join(current))

    if not chunks:
        chunks = [""]

    for chunk in chunks:
        try:
            await message.reply_text(
                chunk,
                parse_mode="HTML",
                disable_web_page_preview=disable_web_page_preview,
            )
        except TelegramError as e:
            print(f"Chunked message send error: {e}")
