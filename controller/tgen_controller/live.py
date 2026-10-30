# SPDX-License-Identifier: MIT
"""Live feed hub (docs/08 §4.4): the engine publishes, every WebSocket client gets a copy.

A slow client never slows the engine: each client has a small queue, and when it is
full the oldest message is dropped (the next tick carries the current state anyway).
"""
from __future__ import annotations

import asyncio
import json

from .health import Health
from .store import now_ms

QUEUE = 50


class Hub:
    def __init__(self, health: Health):
        self.health = health
        self.clients: set[asyncio.Queue] = set()
        self.last_tick: dict | None = None

    def publish(self, msg: dict, data_t_ms: int | None = None) -> None:
        if msg.get("type") == "tick":
            self.last_tick = msg
        text = json.dumps(msg)
        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait((text, data_t_ms))

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(QUEUE)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    async def serve(self, ws) -> None:
        """Pump one WebSocket (starlette) until it closes."""
        q = self.subscribe()
        try:
            if self.last_tick:
                await ws.send_text(json.dumps(self.last_tick))
            while True:
                text, t = await q.get()
                await ws.send_text(text)
                if t:
                    self.health.ws_sent(now_ms() - t)
        finally:
            self.unsubscribe(q)
