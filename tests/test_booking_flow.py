"""Exercise booking updates through the dispatcher without calling Telegram."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    Contact,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
    ReplyKeyboardMarkup,
    Update,
    User,
)

from app.storage import BookingStore
from app.handlers.booking import BookingStates


TEST_TOKEN = "123456:TEST_TOKEN_FOR_LOCAL_TESTS"
NOW = datetime(2026, 9, 24, 8, tzinfo=timezone(timedelta(hours=7)))

with patch.dict(os.environ, {"BOT_TOKEN": TEST_TOKEN}):
    import bot as bot_app


class BookingFlowTests(unittest.IsolatedAsyncioTestCase):
    _user_ids = count(82000, 10)

    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = BookingStore(Path(directory.name) / "bookings.sqlite3")
        self.store.initialize()
        self.user_id = next(self._user_ids)
        self.other_user_id = self.user_id + 1
        self.sequence = count(1)
        self.bot_messages = {}
        self.bot = Bot(token=TEST_TOKEN)
        self.addAsyncCleanup(self.bot.session.close)
        api_patch = patch.object(Bot, "__call__", new_callable=AsyncMock)
        self.api_call = api_patch.start()
        self.addCleanup(api_patch.stop)
        self.api_call.side_effect = self.fake_telegram_response
        for module in ("app.catalog", "app.handlers.booking", "app.handlers.menu", "app.handlers.appointments"):
            now_patch = patch(f"{module}.now_local", return_value=NOW, create=True)
            now_patch.start()
            self.addCleanup(now_patch.stop)
        for user_id in (self.user_id, self.other_user_id):
            await self.state(user_id).clear()
            self.addAsyncCleanup(self.state(user_id).clear)

    def state(self, user_id=None):
        user_id = self.user_id if user_id is None else user_id
        return bot_app.dp.fsm.get_context(
            bot=self.bot, chat_id=user_id, user_id=user_id
        )

    def fake_telegram_response(self, method, **kwargs):
        if isinstance(method, (SendMessage, EditMessageText)):
            chat_id = int(method.chat_id)
            message = Message(
                message_id=getattr(method, "message_id", None) or next(self.sequence),
                date=NOW,
                chat=Chat(id=chat_id, type="private"),
                from_user=User(id=123456, is_bot=True, first_name="Test"),
                text=method.text,
                reply_markup=(
                    method.reply_markup
                    if isinstance(method.reply_markup, InlineKeyboardMarkup)
                    else None
                ),
            )
            self.bot_messages[chat_id] = message
            return message
        return True

    async def deliver(
        self, text=None, *, user_id=None, contact=None,
        chat_id=None, chat_type="private",
    ):
        user_id = self.user_id if user_id is None else user_id
        chat_id = user_id if chat_id is None else chat_id
        update = Update(
            update_id=next(self.sequence),
            message=Message(
                message_id=next(self.sequence),
                date=NOW,
                chat=Chat(id=chat_id, type=chat_type),
                from_user=User(id=user_id, is_bot=False, first_name="User"),
                text=text,
                contact=contact,
                entities=(
                    [MessageEntity(type="bot_command", offset=0, length=len(text))]
                    if text and text.startswith("/") else []
                ),
            ),
        )
        await bot_app.dp.feed_update(self.bot, update, booking_store=self.store)

    async def click(self, data, *, user_id=None, message=None):
        user_id = self.user_id if user_id is None else user_id
        update = Update(
            update_id=next(self.sequence),
            callback_query=CallbackQuery(
                id=str(next(self.sequence)),
                from_user=User(id=user_id, is_bot=False, first_name="User"),
                chat_instance=str(user_id),
                message=message or self.bot_messages[user_id],
                data=data,
            ),
        )
        await bot_app.dp.feed_update(self.bot, update, booking_store=self.store)

    def buttons(self, *, user_id=None):
        user_id = self.user_id if user_id is None else user_id
        keyboard = self.bot_messages[user_id].reply_markup
        self.assertIsInstance(keyboard, InlineKeyboardMarkup)
        return [button for row in keyboard.inline_keyboard for button in row]

    def button_data(self, prefix, *, user_id=None):
        candidates = [
            button.callback_data for button in self.buttons(user_id=user_id)
            if button.callback_data and button.callback_data.startswith(prefix)
        ]
        self.assertTrue(candidates, f"No button with callback prefix {prefix!r}")
        return candidates[0]

    def action_data(self, action, *, user_id=None):
        candidates = [
            button.callback_data for button in self.buttons(user_id=user_id)
            if button.callback_data and f":{action}:" in button.callback_data
        ]
        self.assertTrue(candidates, f"No button for booking action {action!r}")
        return candidates[0]

    async def choose_time(self, *, user_id=None):
        await self.deliver("📝 Записаться", user_id=user_id)
        await self.click(self.button_data("service_manicure", user_id=user_id), user_id=user_id)
        await self.click(self.action_data("master", user_id=user_id), user_id=user_id)
        await self.click(self.action_data("date", user_id=user_id), user_id=user_id)
        time_callback = self.action_data("time", user_id=user_id)
        await self.click(time_callback, user_id=user_id)
        self.assertEqual(await self.state(user_id).get_state(), BookingStates.name.state)
        return time_callback

    async def reach_confirmation(self, *, user_id=None, name="Анна Петрова"):
        await self.choose_time(user_id=user_id)
        await self.deliver(name, user_id=user_id)
        await self.deliver("8 (999) 123-45-67", user_id=user_id)
        self.assertEqual(await self.state(user_id).get_state(), BookingStates.confirm.state)
        return self.action_data("confirm", user_id=user_id)

    async def test_booking_is_saved_once_and_visible_only_to_its_owner(self):
        confirmation = await self.reach_confirmation()
        summary = self.bot_messages[self.user_id]
        data = await self.state().get_data()
        starts_at = datetime.fromisoformat(data["starts_at"])
        for expected in (
            "Маникюр", "Виолетта", "Анна Петрова", "+79991234567",
            starts_at.strftime("%d.%m.%Y"), starts_at.strftime("%H:%M"),
        ):
            self.assertIn(expected, summary.text)
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])

        await self.click(confirmation, message=summary)
        saved = self.store.list_user_bookings(self.user_id)
        self.assertEqual(len(saved), 1)
        booking = saved[0]
        self.assertEqual(booking.user_id, self.user_id)
        self.assertEqual(booking.service_id, "manicure")
        self.assertEqual(booking.master_id, "violetta")
        self.assertEqual(booking.master_name, "Виолетта")
        self.assertEqual(booking.client_name, "Анна Петрова")
        self.assertEqual(booking.phone, "+79991234567")
        self.assertEqual(booking.starts_at, starts_at)
        self.assertEqual(booking.ends_at - booking.starts_at, timedelta(hours=2))
        self.assertIsNone(await self.state().get_state())

        await self.click(confirmation, message=summary)
        self.assertEqual(self.store.list_user_bookings(self.user_id), saved)
        await self.deliver("📋 Мои записи")
        listing = self.bot_messages[self.user_id].text
        for expected in (
            "Маникюр", "Виолетта", "Анна Петрова", "+79991234567",
            starts_at.strftime("%d.%m.%Y"), starts_at.strftime("%H:%M"),
        ):
            self.assertIn(expected, listing)

        await self.deliver("📋 Мои записи", user_id=self.other_user_id)
        self.assertIn("нет записей", self.bot_messages[self.other_user_id].text)
        self.assertNotIn("Анна Петрова", self.bot_messages[self.other_user_id].text)
        self.assertNotIn("+79991234567", self.bot_messages[self.other_user_id].text)

        group_id = -self.user_id
        await self.deliver("📋 Мои записи", chat_id=group_id, chat_type="group")
        group_reply = self.bot_messages[group_id].text
        self.assertIn("в личном чате", group_reply)
        self.assertNotIn("Анна Петрова", group_reply)
        self.assertNotIn("+79991234567", group_reply)

    async def test_slot_taken_during_confirmation_can_be_replaced_without_losing_details(self):
        first_confirmation = await self.reach_confirmation()
        first_summary = self.bot_messages[self.user_id]
        second_confirmation = await self.reach_confirmation(
            user_id=self.other_user_id, name="Мария"
        )
        first_data = await self.state().get_data()
        second_data = await self.state(self.other_user_id).get_data()
        self.assertEqual(first_data["starts_at"], second_data["starts_at"])
        second_summary = self.bot_messages[self.other_user_id]

        await self.click(first_confirmation, message=first_summary)
        await self.click(
            second_confirmation, user_id=self.other_user_id, message=second_summary
        )
        self.assertEqual(self.store.list_user_bookings(self.other_user_id), [])
        self.assertEqual(
            await self.state(self.other_user_id).get_state(), BookingStates.time.state
        )
        first_start = datetime.fromisoformat(first_data["starts_at"])
        blocked_times = {
            first_start.strftime("%H%M"),
            (first_start + timedelta(hours=1)).strftime("%H%M"),
        }
        offered_times = {
            button.callback_data.rsplit(":", 1)[-1][-4:]
            for button in self.buttons(user_id=self.other_user_id)
            if ":time:" in (button.callback_data or "")
        }
        self.assertFalse(offered_times & blocked_times)
        await self.click(
            self.action_data("time", user_id=self.other_user_id),
            user_id=self.other_user_id,
        )
        self.assertEqual(
            await self.state(self.other_user_id).get_state(), BookingStates.confirm.state
        )
        self.assertIn("Мария", self.bot_messages[self.other_user_id].text)
        await self.click(
            self.action_data("confirm", user_id=self.other_user_id),
            user_id=self.other_user_id,
        )
        replacement = self.store.list_user_bookings(self.other_user_id)
        self.assertEqual(len(replacement), 1)
        self.assertEqual(replacement[0].client_name, "Мария")
        self.assertEqual(replacement[0].phone, "+79991234567")
        first = self.store.list_user_bookings(self.user_id)[0]
        self.assertGreaterEqual(replacement[0].starts_at, first.ends_at)

    async def test_invalid_name_phone_and_someone_elses_contact_do_not_advance(self):
        await self.choose_time()
        for invalid_name in (" ", "12345", "А" * 81):
            with self.subTest(name=invalid_name):
                await self.deliver(invalid_name)
                self.assertEqual(await self.state().get_state(), BookingStates.name.state)
                self.assertNotIn("client_name", await self.state().get_data())
        await self.deliver("  Анна   Петрова  ")
        self.assertEqual((await self.state().get_data())["client_name"], "Анна Петрова")
        for invalid_phone in ("привет", "12345", "+7000000"):
            with self.subTest(phone=invalid_phone):
                await self.deliver(invalid_phone)
                self.assertEqual(await self.state().get_state(), BookingStates.phone.state)
                self.assertNotIn("phone", await self.state().get_data())
        await self.deliver(contact=Contact(
            phone_number="79991234567", first_name="Другой",
            user_id=self.other_user_id,
        ))
        self.assertEqual(await self.state().get_state(), BookingStates.phone.state)
        self.assertNotIn("phone", await self.state().get_data())
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])

        await self.deliver(contact=Contact(
            phone_number="79991234567", first_name="Анна", user_id=self.user_id
        ))
        self.assertEqual(await self.state().get_state(), BookingStates.confirm.state)
        self.assertEqual((await self.state().get_data())["phone"], "+79991234567")
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])

    async def test_cancel_and_callbacks_from_an_old_form_cannot_save_booking(self):
        confirmation = await self.reach_confirmation()
        old_summary = self.bot_messages[self.user_id]
        cancel = self.action_data("cancel")
        await self.click(cancel)
        self.assertIsNone(await self.state().get_state())
        await self.click(confirmation, message=old_summary)
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])

        await self.deliver("📝 Записаться")
        await self.click(confirmation, message=old_summary)
        self.assertEqual(await self.state().get_state(), BookingStates.service.state)
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])
        await self.deliver("/cancel")
        self.assertIsNone(await self.state().get_state())

    async def test_closed_dates_and_a_slot_extending_after_work_hours_are_rejected(self):
        await self.deliver("📝 Записаться")
        await self.click(self.button_data("service_manicure"))
        await self.click(self.action_data("master"))
        data = await self.state().get_data()
        flow = data["flow_id"]
        for invalid_day in ("2026-09-25", "2026-09-26", "2026-10-01", "not-a-date"):
            with self.subTest(day=invalid_day):
                await self.click(f"book:{flow}:date:{invalid_day}")
                self.assertEqual(await self.state().get_state(), BookingStates.date.state)
                self.assertNotIn("date", await self.state().get_data())
        await self.click(self.action_data("date"))
        offered_times = [button.text for button in self.buttons()]
        self.assertNotIn("17:00", offered_times)
        chosen_day = (await self.state().get_data())["date"].replace("-", "")
        await self.click(f"book:{flow}:time:{chosen_day}1700")
        self.assertEqual(await self.state().get_state(), BookingStates.time.state)
        self.assertNotIn("starts_at", await self.state().get_data())
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])

    async def test_time_that_passes_before_confirmation_is_not_booked(self):
        confirmation = await self.reach_confirmation()
        data = await self.state().get_data()
        later = datetime.fromisoformat(data["starts_at"]) + timedelta(minutes=1)
        with (
            patch("app.catalog.now_local", return_value=later),
            patch("app.handlers.booking.now_local", return_value=later),
        ):
            await self.click(confirmation)
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])
        self.assertEqual(await self.state().get_state(), BookingStates.date.state)
        self.assertEqual((await self.state().get_data())["client_name"], "Анна Петрова")

    async def test_changing_date_invalidates_the_old_time_and_confirmation_buttons(self):
        old_time = await self.choose_time()
        await self.deliver("Анна")
        await self.deliver("89991234567")
        old_confirmation = self.action_data("confirm")
        old_summary = self.bot_messages[self.user_id]
        old_data = await self.state().get_data()
        await self.click(self.action_data("dates"))
        other_dates = [
            button.callback_data for button in self.buttons()
            if ":date:" in (button.callback_data or "")
            and not button.callback_data.endswith(old_data["date"])
        ]
        self.assertTrue(other_dates)
        await self.click(other_dates[0])
        current_time = self.action_data("time")
        await self.click(old_time)
        self.assertEqual(await self.state().get_state(), BookingStates.time.state)
        self.assertEqual((await self.state().get_data())["starts_at"], old_data["starts_at"])
        await self.click(current_time)
        current_confirmation = self.action_data("confirm")
        current_summary = self.bot_messages[self.user_id]
        current_data = await self.state().get_data()
        self.assertNotEqual(current_data["starts_at"], old_data["starts_at"])
        self.assertNotEqual(current_confirmation, old_confirmation)

        await self.click(old_confirmation, message=old_summary)
        self.assertEqual(await self.state().get_state(), BookingStates.confirm.state)
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])
        await self.click(current_confirmation, message=current_summary)
        saved = self.store.list_user_bookings(self.user_id)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].starts_at, datetime.fromisoformat(current_data["starts_at"]))

    async def test_lost_draft_returns_to_menu_and_idle_cancel_does_not_claim_cancellation(self):
        await self.choose_time()
        await self.deliver("Анна")
        self.assertEqual(await self.state().get_state(), BookingStates.phone.state)
        await self.state().clear()  # Simulate losing an unfinished form on restart.

        for payload in (
            {"contact": Contact(
                phone_number="79991234567", first_name="Анна", user_id=self.user_id
            )},
            {"text": "Анна"},
        ):
            with self.subTest(payload=payload):
                await self.deliver(**payload)
                response = self.api_call.await_args.args[0]
                self.assertIsInstance(response, SendMessage)
                self.assertIn("начните заново", response.text)
                self.assertIsInstance(response.reply_markup, ReplyKeyboardMarkup)
                self.assertIn(
                    "📝 Записаться",
                    [button.text for row in response.reply_markup.keyboard for button in row],
                )
                self.assertIsNone(await self.state().get_state())
                self.assertEqual(self.store.list_user_bookings(self.user_id), [])

        await self.deliver("/cancel")
        self.assertIn("нет незавершённой записи", self.bot_messages[self.user_id].text)
        self.assertNotIn("отменена", self.bot_messages[self.user_id].text)
        self.assertEqual(self.store.list_user_bookings(self.user_id), [])


if __name__ == "__main__":
    unittest.main()
