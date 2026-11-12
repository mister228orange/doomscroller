"""Run locally in a private terminal; login prompts must stay out of chat."""

import asyncio
import argparse

from doomscroller.config import load_settings
from doomscroller.telegram_client import build_client


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mcp', action='store_true', help='Authorize the separate MCP session')
    args = parser.parse_args()
    settings = load_settings()
    client = build_client(settings, settings.mcp_session_path if args.mcp else None)
    try:
        await client.start(phone=settings.phone or None)
        print("Telegram login ready. Session saved locally.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
