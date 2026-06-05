# doomscroller_ollama.py
import json, asyncio
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from ollama import AsyncClient

class DoomScroller:
    """
    Monitors Telegram chats, maintains a persistent state per chat,
    uses a local Ollama model to update the state and detect events.
    Notifies a target chat when a defined event occurs.
    """

    def __init__(
        self,
        config: dict,
        client: TelegramClient,
        ollama_model: str = "llama3",
        ollama_url: str = "http://localhost:11434",
        poll_interval: int = 10,
    ):
        self.config = config
        self.client = client
        self.ollama_model = ollama_model
        self.ollama_url = ollama_url
        self.poll_interval = poll_interval
        self.states: dict = {}
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._ollama: Optional[AsyncClient] = None

        # Load persisted states
        state_dir = Path("doom_states")
        state_dir.mkdir(exist_ok=True)
        for chat in config.get("monitored_chats", []):
            path = state_dir / f"{chat}.json"
            if path.exists():
                self.states[chat] = json.loads(path.read_text())
            else:
                self.states[chat] = {"previous": "", "current": "", "last_msg_id": 0}

    def _save_state(self, chat: str):
        (Path("doom_states") / f"{chat}.json").write_text(
            json.dumps(self.states[chat], indent=2)
        )

    async def start(self):
        if self._running:
            return
        self._running = True
        # Create Ollama AsyncClient (no need to close, stateless)
        self._ollama = AsyncClient(host=self.ollama_url)
        await self.client.start()
        for chat in self.config["monitored_chats"]:
            self._tasks.append(asyncio.create_task(self._monitor_chat(chat)))

    async def stop(self):
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._ollama = None

    async def _monitor_chat(self, chat_name: str):
        while self._running:
            try:
                entity = await self.client.get_entity(chat_name)
                state = self.states[chat_name]
                last_id = state.get("last_msg_id", 0)

                messages = await self.client.get_messages(
                    entity, limit=100, offset_id=last_id, min_id=last_id
                )
                if not messages:
                    await asyncio.sleep(self.poll_interval)
                    continue

                messages.sort(key=lambda m: m.id)
                self.states[chat_name]["last_msg_id"] = messages[-1].id
                self._save_state(chat_name)

                new_text = "\n".join(
                    f"{m.sender_id}: {m.text}" for m in messages if m.text
                )
                if not new_text:
                    await asyncio.sleep(self.poll_interval)
                    continue

                # 1. Build prompt
                prompt = self._build_prompt(chat_name, new_text)
                # 2. Call Ollama via the Python library
                response_text = await self._call_ollama(prompt)

                # 3. Parse the JSON response
                try:
                    result = json.loads(response_text)
                except json.JSONDecodeError:
                    result = {"new_state": state["current"], "event": None, "message": ""}

                # 4. Update state
                old_current = state["current"]
                state["previous"] = old_current
                state["current"] = result.get("new_state", old_current)
                self._save_state(chat_name)

                # 5. Notify if event detected
                if result.get("event"):
                    msg = result.get("message", f"Event: {result['event']}")
                    await self._send_notification(msg)

            except Exception as e:
                print(f"Error in {chat_name}: {e}")
            finally:
                await asyncio.sleep(self.poll_interval)

    def _build_prompt(self, chat_name: str, new_messages: str) -> str:
        state = self.states[chat_name]
        events = "\n".join(f"- {e}" for e in self.config["event_descriptions"])
        return f"""
You are a stateful monitoring assistant.
Purpose: {self.config['purpose']}

Current task state:
- Previous: {state['previous']}
- Current: {state['current']}

New messages from chat "{chat_name}":
{new_messages}

Events we are waiting for (choose one if applicable, or null if none):
{events}

Based on the new messages, update the task state. The new state should be a concise description of the most recent task being discussed.
Determine if any of the listed events occurred in these messages.
Return ONLY a JSON object:
{{
  "new_state": "<updated description>",
  "event": "<event name or null>",
  "message": "<detailed notification message>"
}}
"""

    async def _call_ollama(self, prompt: str) -> str:
        """Use ollama.AsyncClient to generate a JSON response."""
        response = await self._ollama.generate(
            model=self.ollama_model,
            prompt=prompt,
            format="json",
            stream=False,
        )
        return response["response"]

    async def _send_notification(self, message: str):
        target = self.config["target_chat"]
        try:
            entity = await self.client.get_entity(target)
            await self.client.send_message(entity, message) # type: ignore
            print(f"Notification sent to {target}: {message}")
        except Exception as e:
            print(f"Failed to send notification: {e}")
