import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage import (
    BookingChangedError,
    BookingNotFoundError,
    BookingStore,
    InvalidBookingActionError,
    SlotUnavailableError,
)


class StorageFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bookings.sqlite3"
        self.store = BookingStore(self.path)
        self.tz = timezone(timedelta(hours=7))
        self.now = datetime(2026, 10, 1, 7, tzinfo=self.tz)
        self.start = self.now + timedelta(hours=4)

    def details(self, **overrides):
        values = dict(
            request_id="first-request", user_id=42,
            service_id="manicure", service_name="Маникюр",
            master_id="violetta", master_name="Виолетта",
            starts_at=self.start, duration_minutes=120,
            client_name="Анна", phone="+79991234567",
        )
        values.update(overrides)
        return values

    def book(self, **overrides):
        return self.store.create_booking(**self.details(**overrides))

    def move(self, booking, **overrides):
        options = dict(
            user_id=booking.user_id, expected_version=booking.version,
            now=self.now, starts_at=booking.starts_at + timedelta(days=1),
        )
        options.update(overrides)
        return self.store.reschedule_booking(booking.id, **options)

    def cancel(self, booking, **overrides):
        options = dict(user_id=booking.user_id, expected_version=booking.version, now=self.now)
        options.update(overrides)
        return self.store.cancel_booking(booking.id, **options)


class StorageMigrationTests(StorageFixture):
    def create_legacy_database(self):
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        starts_us = int((self.start - epoch).total_seconds()) * 1_000_000
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL, service_id TEXT NOT NULL,
                    service_name TEXT NOT NULL, master_id TEXT NOT NULL,
                    master_name TEXT NOT NULL, starts_at INTEGER NOT NULL,
                    duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
                    client_name TEXT NOT NULL, phone TEXT NOT NULL,
                    ends_at INTEGER NOT NULL CHECK(ends_at > starts_at)
                )
            """)
            connection.execute("""
                INSERT INTO bookings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                41, "legacy-request", 42, "manicure", "Маникюр", "violetta",
                "Виолетта", starts_us, 120, "О'Коннор", "+79991234567",
                starts_us + 120 * 60 * 1_000_000,
            ))

    def test_legacy_rows_are_preserved_and_upgrade_is_idempotent(self):
        self.create_legacy_database()
        self.store.initialize()
        booking = self.store.get_booking(41)
        self.assertEqual(booking.client_name, "О'Коннор")
        self.assertEqual(booking.starts_at, self.start)
        self.assertEqual(booking.ends_at, self.start + timedelta(hours=2))
        self.assertEqual(booking.request_id, "legacy-request")
        self.assertEqual(booking.status, "confirmed")
        self.assertEqual(booking.version, 1)
        self.assertIs(booking.reminders_enabled, False)
        self.assertEqual(booking.reminder_sent_version, 0)
        cancelled = self.cancel(booking)
        replacement = self.book(reminders_enabled=True)
        blocked_day = (self.start + timedelta(days=1)).date()
        self.store.block_day("violetta", blocked_day, tz=self.tz)
        reopened = BookingStore(self.path)
        reopened.initialize()
        reopened.initialize()
        self.assertEqual(reopened.get_booking(41), cancelled)
        self.assertEqual(reopened.get_booking(replacement.id), replacement)
        self.assertGreater(replacement.id, 41)
        self.assertTrue(reopened.is_day_blocked("violetta", blocked_day))

    def test_partial_upgrade_preserves_an_existing_status_column(self):
        self.create_legacy_database()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE bookings ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'")
            connection.execute("UPDATE bookings SET status = 'cancelled' WHERE id = 41")
        self.store.initialize()
        booking = self.store.get_booking(41)
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(booking.version, 1)
        self.assertIs(booking.reminders_enabled, False)
        self.assertEqual(self.store.busy_intervals("violetta", self.start.date(), tz=self.tz), [])

    def test_concurrent_legacy_upgrades_do_not_duplicate_or_lose_rows(self):
        self.create_legacy_database()
        barrier = threading.Barrier(3)

        def upgrade(_):
            barrier.wait(timeout=10)
            store = BookingStore(self.path)
            store.initialize()
            return store.list_user_bookings(42)

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(upgrade, range(3)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(results[0]), 1)
        self.assertEqual(results[0][0].id, 41)


class StorageManagementTests(StorageFixture):
    def setUp(self):
        super().setUp()
        self.store.initialize()

    def test_cancel_frees_slot_but_preserves_history_and_is_idempotent(self):
        original = self.book()
        cancelled = self.cancel(original)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.version, 2)
        self.assertEqual(cancelled.starts_at, original.starts_at)
        self.assertEqual(self.cancel(original), cancelled)
        self.assertEqual(self.store.busy_intervals("violetta", self.start.date(), tz=self.tz), [])
        replacement = self.book(request_id="replacement", user_id=99)
        self.assertEqual(self.store.list_user_bookings(42), [cancelled])
        self.assertEqual(self.store.list_user_bookings(99), [replacement])

    def test_lookup_and_management_do_not_disclose_other_users_bookings(self):
        original = self.book()
        self.assertEqual(self.store.get_booking(original.id, user_id=42), original)
        self.assertEqual(self.store.get_booking(original.id), original)
        self.assertIsNone(self.store.get_booking(original.id, user_id=99))
        self.assertIsNone(self.store.get_booking(999))
        with self.assertRaises(BookingNotFoundError):
            self.cancel(original, user_id=99)
        with self.assertRaises(BookingNotFoundError):
            self.move(original, user_id=99)
        with self.assertRaises(BookingNotFoundError):
            self.store.cancel_booking(999, user_id=None, expected_version=1, now=self.now)
        self.assertEqual(self.store.get_booking(original.id), original)
        cancelled = self.cancel(original, user_id=None)
        with self.assertRaises(BookingNotFoundError):
            self.cancel(cancelled, user_id=99)

    def test_started_bookings_cannot_be_cancelled_or_rescheduled(self):
        booking = self.book()
        for current in (self.start, self.start + timedelta(hours=3)):
            with self.subTest(current=current):
                with self.assertRaises(InvalidBookingActionError):
                    self.cancel(booking, now=current)
                with self.assertRaises(InvalidBookingActionError):
                    self.move(booking, now=current)
        self.assertEqual(self.store.get_booking(booking.id), booking)

    def test_reschedule_preserves_identity_and_releases_old_slot(self):
        booking = self.book(reminders_enabled=True)
        self.assertTrue(self.store.mark_reminder_sent(booking.id, booking.version))
        moved = self.move(booking)
        self.assertEqual(moved.id, booking.id)
        self.assertEqual(moved.user_id, booking.user_id)
        self.assertEqual(moved.request_id, booking.request_id)
        self.assertEqual(moved.client_name, booking.client_name)
        self.assertEqual(moved.phone, booking.phone)
        self.assertEqual(moved.starts_at, self.start + timedelta(days=1))
        self.assertEqual(moved.ends_at - moved.starts_at, timedelta(hours=2))
        self.assertEqual(moved.version, 2)
        self.assertEqual(moved.reminder_sent_version, 0)
        self.assertIs(moved.reminders_enabled, True)
        replacement = self.book(request_id="replacement", user_id=99)
        self.assertEqual(self.store.list_user_bookings(42), [moved])
        self.assertEqual(self.store.list_user_bookings(99), [replacement])

    def test_reschedule_may_overlap_its_own_previous_time(self):
        booking = self.book()
        moved = self.move(booking, starts_at=self.start + timedelta(minutes=30))
        self.assertEqual(moved.starts_at, self.start + timedelta(minutes=30))
        self.assertEqual(moved.version, 2)
        self.assertEqual(self.store.busy_intervals(
            "violetta", self.start.date(), tz=self.tz, exclude_booking_id=moved.id
        ), [])

    def test_busy_intervals_excludes_only_requested_booking(self):
        first = self.book()
        second = self.book(request_id="second", starts_at=self.start + timedelta(hours=3))
        self.assertEqual(self.store.busy_intervals(
            "violetta", self.start.date(), tz=self.tz, exclude_booking_id=first.id
        ), [(second.starts_at, second.ends_at)])
        tomorrow = (self.start + timedelta(days=1)).date()
        self.store.block_day("violetta", tomorrow, tz=self.tz)
        midnight = datetime.combine(tomorrow, datetime.min.time(), self.tz)
        self.assertEqual(self.store.busy_intervals(
            "violetta", tomorrow, tz=self.tz, exclude_booking_id=first.id
        ), [(midnight, midnight + timedelta(days=1))])

    def test_conflicting_reschedule_rolls_back_every_field(self):
        original = self.book(reminders_enabled=True)
        self.store.mark_reminder_sent(original.id, original.version)
        before = self.store.get_booking(original.id)
        target = self.book(request_id="target", starts_at=self.start + timedelta(hours=3))
        with self.assertRaises(SlotUnavailableError):
            self.move(original, starts_at=target.starts_at, reminders_enabled=False)
        self.assertEqual(self.store.get_booking(original.id), before)
        self.assertEqual(self.store.get_booking(target.id), target)

    def test_version_rejects_stale_actions_and_cancelled_reschedule(self):
        original = self.book()
        moved = self.move(original)
        with self.assertRaises(BookingChangedError):
            self.cancel(original)
        with self.assertRaises(BookingChangedError):
            self.move(original, starts_at=self.start + timedelta(days=2))
        cancelled = self.cancel(moved)
        with self.assertRaises(InvalidBookingActionError):
            self.move(cancelled)
        self.assertEqual(self.store.get_booking(original.id), cancelled)

    def test_reschedule_validates_future_timezone_owner_and_reminder_flag(self):
        original = self.book()
        for options, expected in (
            ({"starts_at": self.now}, InvalidBookingActionError),
            ({"starts_at": self.now - timedelta(minutes=1)}, InvalidBookingActionError),
            ({"starts_at": self.start.replace(tzinfo=None)}, ValueError),
            ({"now": self.now.replace(tzinfo=None)}, ValueError),
            ({"user_id": None}, ValueError),
            ({"reminders_enabled": "false"}, ValueError),
        ):
            with self.subTest(options=options), self.assertRaises(expected):
                self.move(original, **options)
        self.assertEqual(self.store.get_booking(original.id), original)

    def test_concurrent_cancel_and_reschedule_have_one_winner(self):
        original = self.book()
        barrier = threading.Barrier(2)

        def manage(action):
            barrier.wait(timeout=10)
            try:
                return ("ok", action(original))
            except BookingChangedError:
                return ("stale", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(manage, (self.cancel, self.move)))
        self.assertEqual(sorted(result[0] for result in results), ["ok", "stale"])
        latest = self.store.get_booking(original.id)
        self.assertEqual(latest.version, 2)
        self.assertEqual(len(self.store.list_user_bookings(42)), 1)

    def test_list_bookings_uses_half_open_range_and_status_master_filters(self):
        first = self.book()
        other = self.book(request_id="other", master_id="other", user_id=99)
        late = self.book(request_id="late", starts_at=self.start + timedelta(hours=3))
        cancelled = self.cancel(other)
        end = late.starts_at
        self.assertEqual(self.store.list_bookings(self.start, end), [first])
        self.assertEqual(self.store.list_bookings(self.start, end, include_cancelled=True), [first, cancelled])
        self.assertEqual(self.store.list_bookings(
            self.start, end, master_id="other", include_cancelled=True
        ), [cancelled])
        self.assertEqual(self.store.list_bookings(end, end + timedelta(seconds=1)), [late])
        self.assertEqual(self.store.list_bookings(end, end), [])
        with self.assertRaises(ValueError):
            self.store.list_bookings(end, self.start)

    def test_blocked_day_is_idempotent_persistent_and_specific_to_master(self):
        day = self.start.date()
        tomorrow = day + timedelta(days=1)
        for target in (day, tomorrow, day):
            self.store.block_day("violetta", target, tz=self.tz)
        self.store.block_day("other", day, tz=self.tz)
        self.assertTrue(BookingStore(self.path).is_day_blocked("violetta", day))
        self.assertFalse(self.store.is_day_blocked("unknown", day))
        self.assertEqual(self.store.list_blocked_days("violetta", day, tomorrow), [day, tomorrow])
        self.assertEqual(self.store.list_blocked_days("violetta", tomorrow, tomorrow), [tomorrow])
        with self.assertRaises(SlotUnavailableError):
            self.book()
        self.book(master_id="available")
        self.store.unblock_day("violetta", day)
        self.store.unblock_day("violetta", day)
        self.assertFalse(self.store.is_day_blocked("violetta", day))
        self.assertTrue(self.store.is_day_blocked("other", day))
        self.book(request_id="after-unblock")

    def test_block_refuses_booked_day_but_allows_cancelled_booking(self):
        original = self.book()
        with self.assertRaises(SlotUnavailableError):
            self.store.block_day("violetta", self.start.date(), tz=self.tz)
        self.assertFalse(self.store.is_day_blocked("violetta", self.start.date()))
        self.cancel(original)
        self.store.block_day("violetta", self.start.date(), tz=self.tz)
        self.assertTrue(self.store.is_day_blocked("violetta", self.start.date()))

    def test_blocking_checks_bookings_crossing_midnight_and_adjacent_boundaries(self):
        midnight = self.start.replace(hour=0)
        original = self.book(starts_at=midnight - timedelta(minutes=30))
        with self.assertRaises(SlotUnavailableError):
            self.store.block_day("violetta", midnight.date(), tz=self.tz)
        self.assertFalse(self.store.is_day_blocked("violetta", midnight.date()))
        # The prior appointment ends exactly at this master's blocked midnight.
        self.book(request_id="adjacent", master_id="adjacent",
                  starts_at=midnight - timedelta(hours=2))
        self.store.block_day("adjacent", midnight.date(), tz=self.tz)
        self.assertEqual(self.store.get_booking(original.id), original)

    def test_create_and_reschedule_cannot_cross_into_a_blocked_day(self):
        original = self.book()
        midnight = self.start.replace(hour=0) + timedelta(days=1)
        self.store.block_day("violetta", midnight.date(), tz=self.tz)
        for target in (midnight, midnight - timedelta(minutes=30)):
            with self.subTest(target=target):
                with self.assertRaises(SlotUnavailableError):
                    self.book(request_id="blocked", starts_at=target)
                with self.assertRaises(SlotUnavailableError):
                    self.move(original, starts_at=target)
        self.assertEqual(self.store.get_booking(original.id), original)
        self.book(request_id="before-block", starts_at=midnight - timedelta(hours=2))
        self.book(request_id="after-block", starts_at=midnight + timedelta(days=1))

    def test_concurrent_block_and_booking_cannot_both_succeed(self):
        barrier = threading.Barrier(2)

        def act(kind):
            barrier.wait(timeout=10)
            try:
                if kind == "book":
                    self.book()
                else:
                    self.store.block_day("violetta", self.start.date(), tz=self.tz)
                return "ok"
            except SlotUnavailableError:
                return "unavailable"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(act, ("book", "block")))
        self.assertEqual(sorted(results), ["ok", "unavailable"])
        booking_exists = bool(self.store.list_user_bookings(42))
        blocked = self.store.is_day_blocked("violetta", self.start.date())
        self.assertNotEqual(booking_exists, blocked)


class StorageReminderTests(StorageFixture):
    def setUp(self):
        super().setUp()
        self.store.initialize()

    def test_due_reminders_require_opt_in_future_status_and_window(self):
        reminder_now = self.start - timedelta(hours=2)
        due = self.book(reminders_enabled=True)
        self.book(request_id="disabled", master_id="disabled")
        self.book(request_id="far", master_id="far", reminders_enabled=True,
                  starts_at=self.start + timedelta(microseconds=1))
        self.book(request_id="past", master_id="past", reminders_enabled=True,
                  starts_at=reminder_now - timedelta(hours=2))
        self.book(request_id="already-started", master_id="started", reminders_enabled=True,
                  starts_at=reminder_now)
        to_cancel = self.book(request_id="cancelled", master_id="cancelled", reminders_enabled=True)
        self.cancel(to_cancel)
        earlier = self.book(request_id="earlier", master_id="earlier", reminders_enabled=True,
                            starts_at=reminder_now + timedelta(minutes=30))
        self.assertEqual(self.store.due_reminders(reminder_now), [earlier, due])
        self.assertEqual(self.store.due_reminders(reminder_now, within_minutes=60), [earlier])

    def test_mark_is_conditional_idempotent_and_does_not_increment_version(self):
        booking = self.book(reminders_enabled=True)
        disabled = self.book(request_id="disabled", master_id="disabled")
        self.assertFalse(self.store.mark_reminder_sent(booking.id, booking.version + 1))
        self.assertFalse(self.store.mark_reminder_sent(disabled.id, disabled.version))
        self.assertFalse(self.store.mark_reminder_sent(999, 1))
        self.assertTrue(self.store.mark_reminder_sent(booking.id, booking.version))
        self.assertFalse(self.store.mark_reminder_sent(booking.id, booking.version))
        latest = self.store.get_booking(booking.id)
        self.assertEqual(latest.version, booking.version)
        self.assertEqual(latest.reminder_sent_version, booking.version)
        self.assertEqual(self.store.due_reminders(self.start - timedelta(hours=1)), [])

    def test_reschedule_resets_reminder_and_rejects_stale_mark(self):
        original = self.book(reminders_enabled=True)
        self.store.mark_reminder_sent(original.id, original.version)
        moved = self.move(original)
        self.assertFalse(self.store.mark_reminder_sent(original.id, original.version))
        current = moved.starts_at - timedelta(hours=1)
        self.assertEqual(self.store.due_reminders(current), [moved])
        self.assertTrue(self.store.mark_reminder_sent(moved.id, moved.version))
        self.assertEqual(self.store.due_reminders(current), [])
        cancelled = self.cancel(moved)
        self.assertFalse(self.store.mark_reminder_sent(cancelled.id, cancelled.version))
        self.assertEqual(self.store.due_reminders(current), [])

    def test_reminder_preference_can_change_explicitly_when_rescheduling(self):
        original = self.book()
        opted_in = self.move(original, reminders_enabled=True)
        self.assertIs(opted_in.reminders_enabled, True)
        current = opted_in.starts_at - timedelta(hours=1)
        self.assertEqual(self.store.due_reminders(current), [opted_in])
        opted_out = self.move(opted_in, reminders_enabled=False)
        self.assertIs(opted_out.reminders_enabled, False)
        self.assertEqual(self.store.due_reminders(opted_out.starts_at - timedelta(hours=1)), [])

    def test_reminder_arguments_and_idempotency_include_opt_in(self):
        original = self.book(reminders_enabled=True)
        self.assertEqual(self.book(reminders_enabled=True), original)
        with self.assertRaises(ValueError):
            self.book(reminders_enabled=False)
        with self.assertRaises(ValueError):
            self.store.due_reminders(self.now.replace(tzinfo=None))
        for window in (0, -1, True, 1.5):
            with self.subTest(window=window), self.assertRaises(ValueError):
                self.store.due_reminders(self.now, within_minutes=window)


if __name__ == "__main__":
    unittest.main()
