"""Select a shared salon store or a private demo store for each visitor."""

import asyncio
from pathlib import Path

from aiogram import BaseMiddleware

from app.settings import Settings
from app.storage import BookingStore


class StoreProvider:
    def __init__(self, path: Path, settings: Settings):
        self.path = Path(path)
        self.settings = settings
        self.demo_directory = self.path.parent / "demo"
        self._stores: dict[Path, BookingStore] = {}
        self._lock = asyncio.Lock()

    async def get_store(self, user_id: int) -> BookingStore:
        if user_id <= 0:
            raise ValueError("A private user ID is required")
        path = (self.demo_directory / f"user_{user_id}.sqlite3"
                if self.settings.demo_mode else self.path)
        async with self._lock:
            if path not in self._stores:
                store = BookingStore(path)
                await asyncio.to_thread(store.initialize)
                self._stores[path] = store
            return self._stores[path]

    async def reminder_stores(self) -> list[BookingStore]:
        if not self.settings.demo_mode:
            return [await self.get_store(1)]
        # Include private demos created before a server restart.
        paths = await asyncio.to_thread(lambda: list(self.demo_directory.glob("user_*.sqlite3")))
        for path in paths:
            suffix = path.stem.removeprefix("user_")
            if suffix.isascii() and suffix.isdecimal() and int(suffix) > 0:
                await self.get_store(int(suffix))
        return list(self._stores.values())


class BookingStoreMiddleware(BaseMiddleware):
    def __init__(self, provider: StoreProvider):
        self.provider = provider

    async def __call__(self, handler, event, data):
        data.setdefault("settings", self.provider.settings)
        user = data.get("event_from_user")
        # Explicit injected stores are used by offline dispatcher tests.
        if "booking_store" not in data and user is not None:
            data["booking_store"] = await self.provider.get_store(user.id)
        return await handler(event, data)
