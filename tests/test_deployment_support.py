import socket
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Chat, Message, Update, User
from aiohttp import ClientSession

from app.health import start_health_server
from app.settings import Settings
from app.storage import BookingStore
from app.store_provider import BookingStoreMiddleware, StoreProvider
from scripts.backup import backup


class DeploymentSupportTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_changes_from_starting_to_ready(self):
        with closing(socket.socket()) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        status = {"ready": False}
        runner = await start_health_server(port, status)
        self.addAsyncCleanup(runner.cleanup)
        async with ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{port}/health") as response:
                self.assertEqual(response.status, 503)
            status["ready"] = True
            async with client.get(f"http://127.0.0.1:{port}/health") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.json(), {"status": "ok", "service": "by-vio-booking"})

    async def test_real_dispatcher_middleware_injects_private_demo_store(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = StoreProvider(Path(directory) / "bookings.sqlite3", Settings())
            dispatcher = Dispatcher()
            dispatcher.update.outer_middleware(BookingStoreMiddleware(provider))
            router = Router()
            received = []

            @router.message()
            async def capture(message: Message, booking_store: BookingStore, settings: Settings):
                received.append((message.from_user.id, booking_store.path, settings.is_admin(message.from_user.id)))

            dispatcher.include_router(router)
            bot = Bot("123456:TEST_TOKEN_FOR_LOCAL_TESTS")
            try:
                for user_id in (51, 52, 51):
                    await dispatcher.feed_update(bot, Update(
                        update_id=user_id,
                        message=Message(message_id=user_id, date=datetime.now(timezone.utc),
                                        chat=Chat(id=user_id, type="private"),
                                        from_user=User(id=user_id, is_bot=False, first_name="Test"), text="/start"),
                    ))
            finally:
                await bot.session.close()
                await dispatcher.storage.close()
            self.assertEqual(len(received), 3)
            self.assertNotEqual(received[0][1], received[1][1])
            self.assertEqual(received[0][1], received[2][1])
            self.assertTrue(all(row[2] for row in received))
            self.assertFalse(provider.path.exists())


class BackupTests(unittest.TestCase):
    def test_snapshot_preserves_live_and_demo_databases_and_closes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = BookingStore(root / "data" / "bookings.sqlite3")
            demo = BookingStore(root / "data" / "demo" / "user_51.sqlite3")
            for store in (live, demo):
                store.initialize()
                store.create_booking(request_id="backup-test", user_id=51, service_id="manicure",
                                     service_name="Маникюр", master_id="violetta", master_name="Виолетта",
                                     starts_at=datetime(2026, 10, 1, 9, tzinfo=timezone.utc), duration_minutes=120,
                                     client_name="Тест", phone="+70000000000")
            snapshot = backup(root / "data", root / "backups")
            for relative in ("bookings.sqlite3", "demo/user_51.sqlite3"):
                copied = BookingStore(snapshot / relative)
                self.assertEqual(len(copied.list_user_bookings(51)), 1)
            self.assertEqual(len(live.list_user_bookings(51)), 1)


if __name__ == "__main__":
    unittest.main()
