"""Services and the salon's booking calendar (all times are local)."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone


BUSINESS_TZ = timezone(timedelta(hours=7), "Asia/Novosibirsk")
TIMEZONE_LABEL = "Новосибирск (UTC+7)"
BOOKING_DAYS = 7
WORK_START = time(9)
WORK_END = time(18)
SLOT_STEP_MINUTES = 60
# Two work days followed by two days off. September 25–26 are days off.
WORK_CYCLE_START = date(2026, 9, 27)


@dataclass(frozen=True)
class Service:
    id: str
    name: str
    duration_minutes: int


@dataclass(frozen=True)
class Master:
    id: str
    name: str


SERVICES = {
    "manicure": Service("manicure", "💅 Маникюр", 120),
    "manicure_gel": Service("manicure_gel", "✨ Маникюр + Френч", 120),
    "pedicure": Service("pedicure", "🦶 Педикюр", 120),
}
MASTERS = {"violetta": Master("violetta", "Виолетта")}


def now_local() -> datetime:
    return datetime.now(BUSINESS_TZ)


def is_working_day(day: date) -> bool:
    return (day - WORK_CYCLE_START).days % 4 < 2


def candidate_slots(
    day: date, service_id: str, *, now: datetime | None = None,
    duration_minutes: int | None = None,
) -> list[datetime]:
    """Return valid start times before removing existing appointments."""
    current = now if now is not None else now_local()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(BUSINESS_TZ)
    if not current.date() <= day < current.date() + timedelta(days=BOOKING_DAYS):
        return []
    if not is_working_day(day):
        return []
    service = SERVICES[service_id]
    minutes = service.duration_minutes if duration_minutes is None else duration_minutes
    if minutes <= 0:
        raise ValueError("duration_minutes must be positive")
    duration = timedelta(minutes=minutes)
    end_of_day = datetime.combine(day, WORK_END, BUSINESS_TZ)
    start = datetime.combine(day, WORK_START, BUSINESS_TZ)
    result = []
    while start + duration <= end_of_day:
        if start > current:
            result.append(start)
        start += timedelta(minutes=SLOT_STEP_MINUTES)
    return result
