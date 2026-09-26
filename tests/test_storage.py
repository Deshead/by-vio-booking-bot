import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage import BookingStore, SlotUnavailableError


class BookingStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "bookings.sqlite3"
        self.store = BookingStore(self.path)
        self.store.initialize()
        self.local_tz = timezone(timedelta(hours=7))
        self.start = datetime(2026, 10, 1, 10, 0, tzinfo=self.local_tz)

    def details(self, **overrides):
        values = dict(
            request_id="request-one",
            user_id=42,
            service_id="manicure",
            service_name="Маникюр",
            master_id="violetta",
            master_name="Виолетта",
            starts_at=self.start,
            duration_minutes=60,
            client_name="Анна",
            phone="+79991234567",
        )
        values.update(overrides)
        return values

    def test_booking_persists_and_initialize_can_be_repeated(self):
        original = self.store.create_booking(**self.details())
        reopened = BookingStore(self.path)
        reopened.initialize()
        self.assertEqual(reopened.list_user_bookings(42), [original])
        self.assertEqual(original.starts_at, self.start)
        self.assertEqual(original.ends_at, self.start + timedelta(hours=1))
        self.assertEqual(original.starts_at.tzinfo, timezone.utc)

    def test_queries_isolate_users_and_sort_by_start(self):
        later = self.store.create_booking(**self.details(
            request_id="later", starts_at=self.start + timedelta(hours=3)
        ))
        earlier = self.store.create_booking(**self.details())
        private = self.store.create_booking(**self.details(
            request_id="private", user_id=99,
            starts_at=self.start + timedelta(hours=2),
        ))
        self.assertEqual(self.store.list_user_bookings(42), [earlier, later])
        self.assertEqual(self.store.list_user_bookings(99), [private])
        self.assertEqual(self.store.list_user_bookings(100), [])
        self.assertEqual(
            self.store.list_user_bookings(42, now=self.start), [earlier, later]
        )
        self.assertEqual(
            self.store.list_user_bookings(42, now=self.start + timedelta(seconds=1)),
            [later],
        )

    def test_overlap_rejected_in_all_directions(self):
        self.store.create_booking(**self.details(duration_minutes=120))
        cases = [(-30, 60), (0, 120), (30, 30), (90, 60), (-30, 180)]
        for offset, duration in cases:
            with self.subTest(offset=offset, duration=duration):
                with self.assertRaises(SlotUnavailableError):
                    self.store.create_booking(**self.details(
                        request_id=f"overlap-{offset}-{duration}",
                        starts_at=self.start + timedelta(minutes=offset),
                        duration_minutes=duration,
                    ))
        self.assertEqual(len(self.store.list_user_bookings(42)), 1)

    def test_adjacent_bookings_and_another_master_are_allowed(self):
        self.store.create_booking(**self.details())
        self.store.create_booking(**self.details(
            request_id="before", starts_at=self.start - timedelta(hours=1)
        ))
        self.store.create_booking(**self.details(
            request_id="after", starts_at=self.start + timedelta(hours=1)
        ))
        self.store.create_booking(**self.details(
            request_id="another-master", master_id="other", master_name="Other"
        ))
        self.assertEqual(len(self.store.list_user_bookings(42)), 4)

    def test_request_id_retry_is_idempotent_even_in_another_timezone(self):
        original = self.store.create_booking(**self.details())
        retry = self.store.create_booking(**self.details(
            starts_at=self.start.astimezone(timezone.utc)
        ))
        self.assertEqual(retry, original)
        self.assertEqual(self.store.list_user_bookings(42), [original])

    def test_request_id_cannot_be_reused_for_another_user_or_data(self):
        original = self.store.create_booking(**self.details())
        for changed in (
            {"user_id": 99}, {"client_name": "Someone else"},
            {"phone": "+79999999999"}, {"service_id": "pedicure"},
            {"starts_at": self.start + timedelta(days=1)},
            {"duration_minutes": 30}, {"master_id": "other"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.store.create_booking(**self.details(**changed))
        self.assertEqual(self.store.list_user_bookings(42), [original])
        self.assertEqual(self.store.list_user_bookings(99), [])

    def test_simultaneous_bookings_reserve_slot_only_once(self):
        barrier = threading.Barrier(4)

        def book(index):
            contender = BookingStore(self.path)
            barrier.wait(timeout=10)
            try:
                return contender.create_booking(**self.details(
                    request_id=f"concurrent-{index}", user_id=index
                ))
            except SlotUnavailableError:
                return None

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(book, range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.store.busy_intervals(
            "violetta", self.start.date(), tz=self.local_tz
        )), 1)

    def test_simultaneous_retry_returns_the_same_booking(self):
        barrier = threading.Barrier(4)

        def book(_):
            barrier.wait(timeout=10)
            return BookingStore(self.path).create_booking(**self.details())

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(book, range(4)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.store.list_user_bookings(42), [results[0]])

    def test_busy_intervals_respect_local_day_and_crossing_midnight(self):
        midnight = self.start.replace(hour=0)
        crossing = self.store.create_booking(**self.details(
            starts_at=midnight - timedelta(minutes=30)
        ))
        daytime = self.store.create_booking(**self.details(request_id="daytime"))
        self.store.create_booking(**self.details(
            request_id="previous", starts_at=midnight - timedelta(minutes=90)
        ))
        self.store.create_booking(**self.details(
            request_id="next", starts_at=midnight + timedelta(days=1)
        ))
        self.store.create_booking(**self.details(
            request_id="other", master_id="other"
        ))
        intervals = self.store.busy_intervals(
            "violetta", self.start.date(), tz=self.local_tz
        )
        self.assertEqual(intervals, [
            (crossing.starts_at, crossing.ends_at),
            (daytime.starts_at, daytime.ends_at),
        ])
        self.assertTrue(all(value.tzinfo == self.local_tz
                            for interval in intervals for value in interval))

    def test_naive_dates_and_invalid_duration_are_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_booking(**self.details(
                starts_at=self.start.replace(tzinfo=None)
            ))
        with self.assertRaises(ValueError):
            self.store.list_user_bookings(42, now=self.start.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            self.store.busy_intervals("violetta", self.start.date(), tz=None)
        for duration in (0, -1, 1.5, True):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.store.create_booking(**self.details(duration_minutes=duration))

    def test_unicode_apostrophes_and_sql_text_are_stored_as_data(self):
        booking = self.store.create_booking(**self.details(
            client_name="О'Коннор; DROP TABLE bookings; --"
        ))
        self.assertEqual(self.store.list_user_bookings(42), [booking])


if __name__ == "__main__":
    unittest.main()
