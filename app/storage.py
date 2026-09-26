"""Persistent bookings with atomic protection against overlapping appointments."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class SlotUnavailableError(Exception):
    """The master is booked or unavailable during the requested interval."""


class BookingNotFoundError(Exception):
    """The booking does not exist or does not belong to the requested user."""


class BookingChangedError(Exception):
    """The booking was changed after the action buttons were displayed."""


class InvalidBookingActionError(Exception):
    """The requested operation is not valid for this booking anymore."""


@dataclass(frozen=True)
class Booking:
    id: int
    request_id: str
    user_id: int
    service_id: str
    service_name: str
    master_id: str
    master_name: str
    starts_at: datetime
    duration_minutes: int
    client_name: str
    phone: str
    ends_at: datetime
    status: str = "confirmed"
    version: int = 1
    reminders_enabled: bool = False
    reminder_sent_version: int = 0


def _utc_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Booking dates must include a timezone")
    elapsed = value.astimezone(timezone.utc) - _EPOCH
    return ((elapsed.days * 86400 + elapsed.seconds) * 1_000_000
            + elapsed.microseconds)


def _datetime_from_microseconds(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _booking_from_row(row: sqlite3.Row) -> Booking:
    values = dict(row)
    values["starts_at"] = _datetime_from_microseconds(values["starts_at"])
    values["ends_at"] = _datetime_from_microseconds(values["ends_at"])
    values["reminders_enabled"] = bool(values["reminders_enabled"])
    return Booking(**values)


class BookingStore:
    """A synchronous store; async handlers should call it via asyncio.to_thread.

    All stored instants and returned Booking dates use UTC. Each operation owns
    its connection so the same store can safely be used by multiple threads.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            # Keep schema upgrades atomic, including two simultaneous startups.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    id INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    service_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    master_id TEXT NOT NULL,
                    master_name TEXT NOT NULL,
                    starts_at INTEGER NOT NULL,
                    duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
                    client_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    ends_at INTEGER NOT NULL CHECK (ends_at > starts_at),
                    status TEXT NOT NULL DEFAULT 'confirmed'
                        CHECK (status IN ('confirmed', 'cancelled')),
                    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
                    reminders_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK (reminders_enabled IN (0, 1)),
                    reminder_sent_version INTEGER NOT NULL DEFAULT 0
                        CHECK (reminder_sent_version >= 0)
                )
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(bookings)")}
            # These definitions are fixed application constants, not SQL input.
            additions = {
                "status": "TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled'))",
                "version": "INTEGER NOT NULL DEFAULT 1 CHECK (version > 0)",
                "reminders_enabled": "INTEGER NOT NULL DEFAULT 0 CHECK (reminders_enabled IN (0, 1))",
                "reminder_sent_version": "INTEGER NOT NULL DEFAULT 0 CHECK (reminder_sent_version >= 0)",
            }
            for name, definition in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE bookings ADD COLUMN {name} {definition}")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS blocked_days (
                    master_id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    starts_at INTEGER NOT NULL,
                    ends_at INTEGER NOT NULL CHECK (ends_at > starts_at),
                    PRIMARY KEY (master_id, day)
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS bookings_master_time
                ON bookings(master_id, starts_at, ends_at)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS bookings_user_time
                ON bookings(user_id, starts_at)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS blocked_days_master_time
                ON blocked_days(master_id, starts_at, ends_at)
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS bookings_reminders
                ON bookings(starts_at)
                WHERE status = 'confirmed' AND reminders_enabled = 1
            """)

    @staticmethod
    def _require_booking(
        connection: sqlite3.Connection, booking_id: int, user_id: int | None
    ) -> Booking:
        sql = "SELECT * FROM bookings WHERE id = ?"
        parameters = [booking_id]
        if user_id is not None:
            sql += " AND user_id = ?"
            parameters.append(user_id)
        row = connection.execute(sql, parameters).fetchone()
        if row is None:
            raise BookingNotFoundError("Booking not found")
        return _booking_from_row(row)

    @staticmethod
    def _check_availability(
        connection: sqlite3.Connection,
        master_id: str,
        starts_us: int,
        ends_us: int,
        *,
        exclude_id: int | None = None,
    ) -> None:
        sql = """
            SELECT 1 FROM bookings
            WHERE master_id = ? AND status = 'confirmed'
            AND starts_at < ? AND ends_at > ?
        """
        parameters = [master_id, ends_us, starts_us]
        if exclude_id is not None:
            sql += " AND id != ?"
            parameters.append(exclude_id)
        conflict = connection.execute(sql + " LIMIT 1", parameters).fetchone()
        if conflict is not None:
            raise SlotUnavailableError("This time is already booked")
        blocked = connection.execute("""
            SELECT 1 FROM blocked_days
            WHERE master_id = ? AND starts_at < ? AND ends_at > ?
            LIMIT 1
        """, (master_id, ends_us, starts_us)).fetchone()
        if blocked is not None:
            raise SlotUnavailableError("The master is unavailable on this day")

    def create_booking(
        self,
        *,
        request_id: str,
        user_id: int,
        service_id: str,
        service_name: str,
        master_id: str,
        master_name: str,
        starts_at: datetime,
        duration_minutes: int,
        client_name: str,
        phone: str,
        reminders_enabled: bool = False,
    ) -> Booking:
        if (not isinstance(duration_minutes, int)
                or isinstance(duration_minutes, bool)
                or duration_minutes <= 0):
            raise ValueError("Booking duration must be a positive integer")
        if not isinstance(reminders_enabled, bool):
            raise ValueError("reminders_enabled must be a boolean")
        starts_us = _utc_microseconds(starts_at)
        ends_at = starts_at.astimezone(timezone.utc) + timedelta(
            minutes=duration_minutes
        )
        ends_us = _utc_microseconds(ends_at)
        values = {
            "request_id": request_id,
            "user_id": user_id,
            "service_id": service_id,
            "service_name": service_name,
            "master_id": master_id,
            "master_name": master_name,
            "starts_at": starts_us,
            "duration_minutes": duration_minutes,
            "client_name": client_name,
            "phone": phone,
            "ends_at": ends_us,
            "reminders_enabled": int(reminders_enabled),
        }
        for field in (
            "request_id", "service_id", "service_name", "master_id",
            "master_name", "client_name", "phone",
        ):
            if not isinstance(values[field], str) or not values[field].strip():
                raise ValueError(f"{field} must not be empty")

        with closing(self._connect()) as connection, connection:
            # Serialize the availability check and insert across processes too.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM bookings WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                if any(existing[field] != value for field, value in values.items()):
                    raise ValueError("The request ID belongs to another booking")
                return _booking_from_row(existing)

            self._check_availability(connection, master_id, starts_us, ends_us)

            cursor = connection.execute("""
                INSERT INTO bookings (
                    request_id, user_id, service_id, service_name, master_id,
                    master_name, starts_at, duration_minutes, client_name, phone,
                    ends_at, reminders_enabled
                ) VALUES (
                    :request_id, :user_id, :service_id, :service_name, :master_id,
                    :master_name, :starts_at, :duration_minutes, :client_name,
                    :phone, :ends_at, :reminders_enabled
                )
            """, values)
            row = connection.execute(
                "SELECT * FROM bookings WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return _booking_from_row(row)

    def list_user_bookings(
        self, user_id: int, *, now: datetime | None = None
    ) -> list[Booking]:
        sql = "SELECT * FROM bookings WHERE user_id = ?"
        parameters = [user_id]
        if now is not None:
            sql += " AND starts_at >= ?"
            parameters.append(_utc_microseconds(now))
        sql += " ORDER BY starts_at, id"
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [_booking_from_row(row) for row in rows]

    def get_booking(self, booking_id: int, *, user_id: int | None = None) -> Booking | None:
        with closing(self._connect()) as connection:
            try:
                return self._require_booking(connection, booking_id, user_id)
            except BookingNotFoundError:
                return None

    def cancel_booking(
        self,
        booking_id: int,
        *,
        user_id: int | None,
        expected_version: int,
        now: datetime,
    ) -> Booking:
        now_us = _utc_microseconds(now)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            booking = self._require_booking(connection, booking_id, user_id)
            # A retried cancellation must not disclose another user's booking.
            if booking.status == "cancelled":
                return booking
            if booking.version != expected_version:
                raise BookingChangedError("Booking details have changed")
            if booking.status != "confirmed" or _utc_microseconds(booking.starts_at) <= now_us:
                raise InvalidBookingActionError("Only future confirmed bookings can be cancelled")
            connection.execute("""
                UPDATE bookings SET status = 'cancelled', version = version + 1
                WHERE id = ?
            """, (booking_id,))
            return self._require_booking(connection, booking_id, user_id)

    def reschedule_booking(
        self,
        booking_id: int,
        *,
        user_id: int,
        expected_version: int,
        starts_at: datetime,
        now: datetime,
        reminders_enabled: bool | None = None,
    ) -> Booking:
        if user_id is None:
            raise ValueError("Rescheduling requires the booking owner's user ID")
        if reminders_enabled is not None and not isinstance(reminders_enabled, bool):
            raise ValueError("reminders_enabled must be a boolean or None")
        starts_us = _utc_microseconds(starts_at)
        now_us = _utc_microseconds(now)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            booking = self._require_booking(connection, booking_id, user_id)
            if booking.version != expected_version:
                raise BookingChangedError("Booking details have changed")
            if booking.status != "confirmed" or _utc_microseconds(booking.starts_at) <= now_us:
                raise InvalidBookingActionError("Only future confirmed bookings can be rescheduled")
            if starts_us <= now_us:
                raise InvalidBookingActionError("The new appointment must be in the future")
            ends_at = starts_at.astimezone(timezone.utc) + timedelta(minutes=booking.duration_minutes)
            ends_us = _utc_microseconds(ends_at)
            self._check_availability(
                connection, booking.master_id, starts_us, ends_us, exclude_id=booking_id
            )
            reminder_flag = booking.reminders_enabled if reminders_enabled is None else reminders_enabled
            connection.execute("""
                UPDATE bookings SET starts_at = ?, ends_at = ?,
                    version = version + 1, reminder_sent_version = 0,
                    reminders_enabled = ?
                WHERE id = ?
            """, (starts_us, ends_us, int(reminder_flag), booking_id))
            return self._require_booking(connection, booking_id, user_id)

    def list_bookings(
        self,
        start: datetime,
        end: datetime,
        *,
        master_id: str | None = None,
        include_cancelled: bool = False,
    ) -> list[Booking]:
        start_us, end_us = _utc_microseconds(start), _utc_microseconds(end)
        if end_us < start_us:
            raise ValueError("The end of the interval cannot precede its start")
        sql = "SELECT * FROM bookings WHERE starts_at >= ? AND starts_at < ?"
        parameters = [start_us, end_us]
        if master_id is not None:
            sql += " AND master_id = ?"
            parameters.append(master_id)
        if not include_cancelled:
            sql += " AND status = 'confirmed'"
        sql += " ORDER BY starts_at, id"
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [_booking_from_row(row) for row in rows]

    def block_day(self, master_id: str, day: date, *, tz: tzinfo) -> None:
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        starts_us, ends_us = _utc_microseconds(day_start), _utc_microseconds(day_end)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM blocked_days WHERE master_id = ? AND day = ?",
                (master_id, day.isoformat()),
            ).fetchone()
            if existing is not None:
                return
            conflict = connection.execute("""
                SELECT 1 FROM bookings WHERE master_id = ? AND status = 'confirmed'
                    AND starts_at < ? AND ends_at > ? LIMIT 1
            """, (master_id, ends_us, starts_us)).fetchone()
            if conflict is not None:
                raise SlotUnavailableError("This day already has confirmed bookings")
            connection.execute("""
                INSERT INTO blocked_days(master_id, day, starts_at, ends_at)
                VALUES (?, ?, ?, ?)
            """, (master_id, day.isoformat(), starts_us, ends_us))

    def unblock_day(self, master_id: str, day: date) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM blocked_days WHERE master_id = ? AND day = ?",
                (master_id, day.isoformat()),
            )

    def is_day_blocked(self, master_id: str, day: date) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM blocked_days WHERE master_id = ? AND day = ?",
                (master_id, day.isoformat()),
            ).fetchone()
        return row is not None

    def list_blocked_days(self, master_id: str, from_day: date, to_day: date) -> list[date]:
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT day FROM blocked_days
                WHERE master_id = ? AND day >= ? AND day <= ? ORDER BY day
            """, (master_id, from_day.isoformat(), to_day.isoformat())).fetchall()
        return [date.fromisoformat(row["day"]) for row in rows]

    def busy_intervals(
        self, master_id: str, day: date, *, tz: tzinfo,
        exclude_booking_id: int | None = None,
    ) -> list[tuple[datetime, datetime]]:
        """Return whole intervals overlapping the local day, in the given zone."""
        day_start = datetime.combine(day, time.min, tzinfo=tz)
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        starts_us = _utc_microseconds(day_start)
        ends_us = _utc_microseconds(day_end)
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT starts_at, ends_at FROM bookings
                WHERE master_id = ? AND status = 'confirmed'
                    AND starts_at < ? AND ends_at > ?
                    AND (? IS NULL OR id != ?)
                UNION ALL
                SELECT starts_at, ends_at FROM blocked_days
                WHERE master_id = ? AND starts_at < ? AND ends_at > ?
                ORDER BY starts_at, ends_at
            """, (
                master_id, ends_us, starts_us, exclude_booking_id, exclude_booking_id,
                master_id, ends_us, starts_us,
            )).fetchall()
        return [
            (
                _datetime_from_microseconds(row["starts_at"]).astimezone(tz),
                _datetime_from_microseconds(row["ends_at"]).astimezone(tz),
            )
            for row in rows
        ]

    def due_reminders(
        self, now: datetime, *, within_minutes: int = 120
    ) -> list[Booking]:
        if (not isinstance(within_minutes, int) or isinstance(within_minutes, bool)
                or within_minutes <= 0):
            raise ValueError("The reminder window must be a positive integer")
        now_us = _utc_microseconds(now)
        end_us = _utc_microseconds(now.astimezone(timezone.utc) + timedelta(minutes=within_minutes))
        with closing(self._connect()) as connection:
            rows = connection.execute("""
                SELECT * FROM bookings
                WHERE status = 'confirmed' AND reminders_enabled = 1
                    AND starts_at > ? AND starts_at <= ?
                    AND reminder_sent_version != version
                ORDER BY starts_at, id
            """, (now_us, end_us)).fetchall()
        return [_booking_from_row(row) for row in rows]

    def mark_reminder_sent(self, booking_id: int, version: int) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute("""
                UPDATE bookings SET reminder_sent_version = version
                WHERE id = ? AND version = ? AND status = 'confirmed'
                    AND reminders_enabled = 1 AND reminder_sent_version != version
            """, (booking_id, version))
            return cursor.rowcount == 1
