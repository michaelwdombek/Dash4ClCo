"""In-process pub/sub used to push live updates to connected browsers (SSE)."""

from __future__ import annotations

import asyncio


class Hub:
    def __init__(self, maxsize: int = 500):
        self._subscribers: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, kind: str, data) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((kind, data))
            except asyncio.QueueFull:
                # Slow client: drop its backlog and ask it to reload the board.
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(("resync", None))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
