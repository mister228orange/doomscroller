import asyncio

from telethon import TelegramClient

from config import (
    OLLAMA_MODEL,
    OLLAMA_URL,
    POLL_INTERVAL,
    SCROLLER_CONFIG,
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_PHONE,
    TELEGRAM_SESSION,
)
from DoomScroller import DoomScroller

client = TelegramClient(TELEGRAM_SESSION, TELEGRAM_API_ID, TELEGRAM_API_HASH)
scroller = DoomScroller(
    SCROLLER_CONFIG,
    client,
    ollama_model=OLLAMA_MODEL,
    ollama_url=OLLAMA_URL,
    poll_interval=POLL_INTERVAL,
)

async def main():
    await client.start(phone=TELEGRAM_PHONE)
    await scroller.start()
    # Keep running forever
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
