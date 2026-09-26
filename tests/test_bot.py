import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import (
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from app.storage import BookingStore


TEST_TOKEN = "123456:TEST_TOKEN_FOR_LOCAL_TESTS"

with patch.dict(os.environ, {"BOT_TOKEN": TEST_TOKEN}):
    import bot as bot_app


class ProxySelectionTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        bypass = patch.object(bot_app, "proxy_bypass", return_value=False)
        self.bypass = bypass.start()
        self.addCleanup(bypass.stop)
        proxies = patch.object(bot_app, "getproxies", return_value={})
        self.proxies = proxies.start()
        self.addCleanup(proxies.stop)

    def test_system_https_proxy_is_used(self):
        self.proxies.return_value = {
            "https": "http://127.0.0.1:10809",
            "http": "http://127.0.0.1:8080",
        }
        self.assertEqual(bot_app.get_proxy_url(), "http://127.0.0.1:10809")

    def test_explicit_proxy_takes_priority_over_system_settings(self):
        os.environ["BOT_PROXY_URL"] = "http://127.0.0.1:9090"
        self.proxies.return_value = {"https": "http://127.0.0.1:10809"}
        self.bypass.return_value = True
        self.assertEqual(bot_app.get_proxy_url(), "http://127.0.0.1:9090")

    def test_system_bypass_uses_direct_connection(self):
        self.proxies.return_value = {"https": "http://127.0.0.1:10809"}
        self.bypass.return_value = True
        self.assertIsNone(bot_app.get_proxy_url())
        self.bypass.assert_called_once_with("api.telegram.org")

    def test_no_proxy_uses_direct_connection(self):
        self.assertIsNone(bot_app.get_proxy_url())


class MessageHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BookingStore(Path(directory.name) / "bookings.sqlite3")
        self.store.initialize()
        self.bot = Bot(token=TEST_TOKEN)
        self.addAsyncCleanup(self.bot.session.close)
        state = bot_app.dp.fsm.get_context(bot=self.bot, chat_id=42, user_id=42)
        await state.clear()
        # Message.answer awaits Bot.__call__ directly, bypassing Bot.send_message.
        # Mock that boundary so dispatcher tests cannot send network requests.
        api_call = patch.object(Bot, "__call__", new_callable=AsyncMock)
        self.api_call = api_call.start()
        self.addCleanup(api_call.stop)
        identity = patch.object(
            self.bot,
            "me",
            new_callable=AsyncMock,
            return_value=User(
                id=123456, is_bot=True, first_name="Test", username="test_bot"
            ),
        )
        identity.start()
        self.addCleanup(identity.stop)

    async def deliver(self, text):
        update = Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime(2026, 1, 1, tzinfo=timezone.utc),
                chat=Chat(id=42, type="private"),
                from_user=User(id=42, is_bot=False, first_name="User"),
                text=text,
                entities=[
                    MessageEntity(type="bot_command", offset=0, length=len(text))
                ] if text.startswith("/") else [],
            ),
        )
        await bot_app.dp.feed_update(self.bot, update, booking_store=self.store)

    def assert_greeting_sent(self):
        self.api_call.assert_awaited_once()
        request = self.api_call.await_args.args[0]
        self.assertIsInstance(request, SendMessage)
        self.assertEqual(request.chat_id, 42)
        for expected in ("BY VIO", "Виолетте", "Ювента", "📝 Записаться", "ДЕМО"):
            self.assertIn(expected, request.text)
        self.assertIsInstance(request.reply_markup, ReplyKeyboardMarkup)
        labels = [button.text for row in request.reply_markup.keyboard for button in row]
        for label in ("📝 Записаться", "📋 Мои записи", "ℹ️ О нас", "🛠 Кабинет мастера"):
            self.assertIn(label, labels)
        self.assertTrue(request.reply_markup.resize_keyboard)

    async def test_start_command_receives_greeting(self):
        await self.deliver("/start")
        self.assert_greeting_sent()

    async def test_start_addressed_to_this_bot_receives_greeting(self):
        await self.deliver("/start@test_bot")
        self.assert_greeting_sent()

    async def test_start_addressed_to_other_bot_is_ignored(self):
        await self.deliver("/start@other_bot")
        self.api_call.assert_not_awaited()

    def assert_text_reply_sent(self, expected_text):
        self.api_call.assert_awaited_once()
        request = self.api_call.await_args.args[0]
        self.assertIsInstance(request, SendMessage)
        self.assertEqual(request.chat_id, 42)
        self.assertEqual(request.text, expected_text)

    async def test_booking_button_receives_reply(self):
        await self.deliver("📝 Записаться")
        self.api_call.assert_awaited_once()
        self.assertIn("1/6", self.api_call.await_args.args[0].text)
        keyboard = self.api_call.await_args.args[0].reply_markup
        self.assertIsInstance(keyboard, InlineKeyboardMarkup)
        self.assertEqual(
            [[(button.text, button.callback_data) for button in row]
             for row in keyboard.inline_keyboard],
            [
                [("💅 Маникюр", "service_manicure")],
                [("✨ Маникюр + Френч", "service_manicure_gel")],
                [("🦶 Педикюр", "service_pedicure")],
            ],
        )

    async def test_my_bookings_button_receives_reply(self):
        await self.deliver("📋 Мои записи")
        self.api_call.assert_awaited_once()
        self.assertIn("У вас пока нет записей.", self.api_call.await_args.args[0].text)

    async def test_about_button_receives_reply(self):
        await self.deliver("ℹ️ О нас")
        self.api_call.assert_awaited_once()
        for expected in ("BY VIO", "Бьюти Мастер Виолетта", "Салон «Ювента»", "88005553535"):
            self.assertIn(expected, self.api_call.await_args.args[0].text)


if __name__ == "__main__":
    unittest.main()
