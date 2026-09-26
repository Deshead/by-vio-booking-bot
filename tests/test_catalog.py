import unittest
from datetime import date, datetime, timedelta

from app.catalog import BUSINESS_TZ, SERVICES, candidate_slots, is_working_day


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 24, 8, tzinfo=BUSINESS_TZ)

    def test_confirmed_two_on_two_off_schedule(self):
        self.assertTrue(is_working_day(date(2026, 9, 24)))
        self.assertFalse(is_working_day(date(2026, 9, 25)))
        self.assertFalse(is_working_day(date(2026, 9, 26)))
        self.assertTrue(is_working_day(date(2026, 9, 27)))
        self.assertTrue(is_working_day(date(2026, 9, 28)))
        self.assertFalse(is_working_day(date(2026, 9, 29)))
        self.assertFalse(is_working_day(date(2026, 9, 30)))
        self.assertTrue(is_working_day(date(2026, 10, 1)))

    def test_two_hour_services_fit_inside_workday(self):
        for service_id, service in SERVICES.items():
            slots = candidate_slots(date(2026, 9, 27), service_id, now=self.now)
            self.assertEqual(service.duration_minutes, 120)
            self.assertEqual([slot.hour for slot in slots], list(range(9, 17)))
            self.assertTrue(all(
                (slot + timedelta(minutes=service.duration_minutes)).hour <= 18
                for slot in slots
            ))

    def test_days_off_past_dates_and_dates_outside_week_are_not_offered(self):
        for day in (date(2026, 9, 23), date(2026, 9, 25), date(2026, 10, 1)):
            self.assertEqual(candidate_slots(day, "manicure", now=self.now), [])

    def test_elapsed_times_are_not_offered(self):
        current = self.now.replace(hour=15, minute=30)
        slots = candidate_slots(current.date(), "manicure", now=current)
        self.assertEqual([slot.hour for slot in slots], [16])


if __name__ == "__main__":
    unittest.main()
