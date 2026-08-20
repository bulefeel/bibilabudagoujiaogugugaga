"""Hierarchical asyncio locks used by browser and financial workflows."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import re

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.-]+")


class ExecutionLocks:
    """Process-local lock hierarchy.

    Lock order is always ``funds -> store -> launch``.  The launch lock is
    deliberately held only while start/stop commands run; a store lock remains
    held for the full browser session.  Funds V1 sessions acquire a single
    global lock, making all disbursements serial.
    """

    def __init__(self) -> None:
        self.launch = asyncio.Lock()
        self.funds = asyncio.Lock()
        self._store_locks: dict[str, asyncio.Lock] = {}
        self._map_guard = asyncio.Lock()

    @staticmethod
    def normalise_store_key(store_key: str) -> str:
        key = _SAFE_KEY.sub("-", str(store_key).strip()).strip("-")
        if not key:
            raise ValueError("store_key must not be empty")
        return key.lower()

    async def store_lock(self, store_key: str) -> asyncio.Lock:
        key = self.normalise_store_key(store_key)
        async with self._map_guard:
            return self._store_locks.setdefault(key, asyncio.Lock())

    @asynccontextmanager
    async def store(self, store_key: str) -> AsyncIterator[None]:
        lock = await self.store_lock(store_key)
        async with lock:
            yield

    @asynccontextmanager
    async def financial_store(self, store_key: str) -> AsyncIterator[None]:
        """Acquire funds then one store lock in a fixed, deadlock-free order."""
        async with self.funds:
            async with self.store(store_key):
                yield

    def snapshot(self) -> dict[str, object]:
        return {
            "launch_locked": self.launch.locked(),
            "funds_locked": self.funds.locked(),
            "stores": {key: lock.locked() for key, lock in self._store_locks.items()},
        }
