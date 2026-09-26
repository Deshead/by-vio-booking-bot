"""Private, authorized master dashboard. All writes require confirmation."""

import asyncio
import csv
import io
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from app.catalog import BOOKING_DAYS, BUSINESS_TZ, MASTERS, TIMEZONE_LABEL, is_working_day, now_local
from app.keyboards.admin import button, confirmation_keyboard, dashboard_keyboard, home_keyboard
from app.settings import Settings
from app.storage import (
    Booking,
    BookingChangedError,
    BookingNotFoundError,
    BookingStore,
    InvalidBookingActionError,
    SlotUnavailableError,
)


TITLE = "BY VIO · КАБИНЕТ МАСТЕРА"
PAGE_SIZE = 4
PERIOD_NAMES = {
    "today": "Сегодня", "tomorrow": "Завтра", "week": "Ближайшие 7 дней",
    "history": "История и отмены · последний год",
}


def allowed(message: Message, user_id: int, settings: Settings) -> bool:
    return message.chat.type == "private" and settings.is_admin(user_id)


def period_bounds(period: str, current: datetime) -> tuple[datetime, datetime]:
    start = datetime.combine(current.date(), time.min, BUSINESS_TZ)
    if period == "tomorrow":
        start += timedelta(days=1)
    if period == "history":
        return start - timedelta(days=365), start + timedelta(days=BOOKING_DAYS)
    return start, start + timedelta(days=BOOKING_DAYS if period == "week" else 1)


def booking_status(booking: Booking, current: datetime) -> str:
    if booking.status == "cancelled":
        return "Отменена"
    return "Завершена" if booking.ends_at <= current else "Подтверждена"


def booking_card(booking: Booking, current: datetime) -> str:
    start = booking.starts_at.astimezone(BUSINESS_TZ)
    end = booking.ends_at.astimezone(BUSINESS_TZ)
    return (
        f"ЗАПИСЬ №{booking.id} · {booking_status(booking, current)}\n"
        f"{start:%d.%m.%Y} · {start:%H:%M}–{end:%H:%M}\n"
        f"{booking.service_name}\n"
        f"Мастер: {booking.master_name}\n"
        f"Клиент: {booking.client_name}\n"
        f"Телефон: {booking.phone}"
    )


async def show_dashboard(message: Message, booking_store: BookingStore, settings: Settings) -> None:
    current = now_local()
    start, end = period_bounds("week", current)
    records = await asyncio.to_thread(booking_store.list_bookings, start, end)
    lines = [TITLE, "", f"{current:%d.%m.%Y} · {TIMEZONE_LABEL}", ""]
    for period in ("today", "tomorrow", "week"):
        from_time, to_time = period_bounds(period, current)
        selected = [item for item in records if from_time <= item.starts_at < to_time]
        hours = sum(item.duration_minutes for item in selected) / 60
        lines.append(f"{PERIOD_NAMES[period]}: записей {len(selected)} · занято {hours:g} ч")
    if settings.demo_mode:
        lines.extend(["", "Деморежим · здесь только ваш пример салона."])
    await message.answer("\n".join(lines), reply_markup=dashboard_keyboard(), parse_mode=None)


async def show_records(message: Message, booking_store: BookingStore, period: str, page: int) -> None:
    current = now_local()
    start, end = period_bounds(period, current)
    records = await asyncio.to_thread(
        booking_store.list_bookings, start, end, include_cancelled=period == "history"
    )
    if period == "history":
        records = sorted(
            (item for item in records if item.status == "cancelled" or item.ends_at <= current),
            key=lambda item: item.starts_at, reverse=True,
        )
    pages = max(1, (len(records) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    visible = records[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    text = f"{TITLE}\n\n{PERIOD_NAMES[period]} · {len(records)} записей\nВремя: {TIMEZONE_LABEL}"
    if not visible:
        text += "\n\nЗдесь пока нет записей."
    else:
        text += "\n\n" + "\n\n────────────\n\n".join(booking_card(item, current) for item in visible)
    rows = [[button(
        f"Открыть №{item.id} · {item.starts_at.astimezone(BUSINESS_TZ):%d.%m %H:%M}",
        f"card:{item.id}",
    )] for item in visible]
    navigation = []
    if page:
        navigation.append(button("← Назад", f"list:{period}:{page - 1}"))
    if page + 1 < pages:
        navigation.append(button("Далее →", f"list:{period}:{page + 1}"))
    if navigation:
        rows.append(navigation)
        text += f"\n\nСтраница {page + 1} из {pages}"
    rows.append([button("← Кабинет мастера", "home")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode=None)


async def show_card(message: Message, booking_store: BookingStore, booking_id: int) -> None:
    booking = await asyncio.to_thread(booking_store.get_booking, booking_id)
    if booking is None:
        await message.answer("Запись не найдена. Обновите расписание.", reply_markup=home_keyboard())
        return
    current = now_local()
    rows = []
    if booking.status == "confirmed" and booking.starts_at > current:
        rows.append([button("Отменить запись", f"cancel:{booking.id}:{booking.version}")])
    rows.append([button("← Расписание на 7 дней", "list:week:0")])
    rows.append([button("← Кабинет мастера", "home")])
    await message.answer(
        f"{TITLE}\n\n{booking_card(booking, current)}\n\nВремя: {TIMEZONE_LABEL}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode=None,
    )


async def show_days(message: Message, booking_store: BookingStore) -> None:
    first = now_local().date()
    master = next(iter(MASTERS.values()))
    blocked = set(await asyncio.to_thread(
        booking_store.list_blocked_days, master.id, first, first + timedelta(days=BOOKING_DAYS - 1)
    ))
    lines = [TITLE, "", f"РАБОЧИЕ ДНИ · {master.name}", "График 2/2 · 09:00–18:00", ""]
    rows = []
    weekdays = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
    for offset in range(BOOKING_DAYS):
        day = first + timedelta(days=offset)
        label = f"{day:%d.%m} · {weekdays[day.weekday()]}"
        if day in blocked:
            lines.append(f"{label} · закрыт вручную")
            rows.append([button(f"Открыть {label}", f"day:{day.isoformat()}:unblock")])
        elif is_working_day(day):
            lines.append(f"{label} · рабочий день")
            rows.append([button(f"Закрыть {label}", f"day:{day.isoformat()}:block")])
        else:
            lines.append(f"{label} · выходной по графику")
    lines.extend(["", "День с подтверждёнными записями можно закрыть после их отмены или переноса."])
    rows.append([button("← Кабинет мастера", "home")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode=None)


def csv_cell(value: object) -> str:
    text = str(value)
    # A quoted CSV cell alone does not prevent formulas in spreadsheet apps.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


async def export_records(message: Message, booking_store: BookingStore) -> None:
    current = now_local()
    start, end = period_bounds("week", current)
    records = await asyncio.to_thread(booking_store.list_bookings, start, end, include_cancelled=True)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Номер", "Дата", "Начало", "Конец", "Услуга", "Мастер", "Клиент", "Телефон", "Статус"])
    for item in records:
        begins = item.starts_at.astimezone(BUSINESS_TZ)
        finishes = item.ends_at.astimezone(BUSINESS_TZ)
        writer.writerow([csv_cell(value) for value in (
            item.id, f"{begins:%d.%m.%Y}", f"{begins:%H:%M}", f"{finishes:%H:%M}",
            item.service_name, item.master_name, item.client_name, item.phone, booking_status(item, current),
        )])
    document = BufferedInputFile(buffer.getvalue().encode("utf-8-sig"), filename=f"by-vio-{current:%Y%m%d}.csv")
    await message.answer_document(document, caption=f"BY VIO · расписание на 7 дней\nВремя: {TIMEZONE_LABEL}")


async def admin_handler(message: Message, state: FSMContext, settings: Settings, booking_store: BookingStore) -> None:
    if message.from_user is None or not allowed(message, message.from_user.id, settings):
        await message.answer("Кабинет доступен мастеру в личном чате с ботом.")
        return
    await state.clear()
    await show_dashboard(message, booking_store, settings)


async def admin_export_handler(message: Message, settings: Settings, booking_store: BookingStore) -> None:
    if message.from_user is None or not allowed(message, message.from_user.id, settings):
        await message.answer("Выгрузка доступна мастеру в личном чате с ботом.")
        return
    await export_records(message, booking_store)


async def confirm_cancel(message: Message, state: FSMContext, booking_store: BookingStore, booking_id: int, version: int) -> None:
    booking = await asyncio.to_thread(booking_store.get_booking, booking_id)
    current = now_local()
    if booking is None or booking.version != version:
        await message.answer("Запись изменилась. Обновите расписание.", reply_markup=home_keyboard())
        return
    if booking.status != "confirmed" or booking.starts_at <= current:
        await message.answer("Эту запись уже нельзя отменить.", reply_markup=home_keyboard())
        return
    token = uuid4().hex[:12]
    await state.update_data(admin_pending={"token": token, "action": "cancel", "booking_id": booking.id, "version": booking.version})
    await message.answer(
        f"ОТМЕНИТЬ ЗАПИСЬ?\n\n{booking_card(booking, current)}\n\nВремя снова станет доступно для записи.",
        reply_markup=confirmation_keyboard(token, "Да, отменить запись"), parse_mode=None,
    )


async def confirm_day(message: Message, state: FSMContext, booking_store: BookingStore, day: date, action: str) -> None:
    first = now_local().date()
    if not first <= day < first + timedelta(days=BOOKING_DAYS):
        await message.answer("Эта дата уже недоступна. Откройте рабочие дни снова.", reply_markup=home_keyboard())
        return
    master = next(iter(MASTERS.values()))
    blocked = await asyncio.to_thread(booking_store.is_day_blocked, master.id, day)
    if (action == "block" and (blocked or not is_working_day(day))) or (action == "unblock" and not blocked):
        await message.answer("Расписание изменилось. Обновите рабочие дни.", reply_markup=home_keyboard())
        return
    token = uuid4().hex[:12]
    await state.update_data(admin_pending={"token": token, "action": action, "day": day.isoformat(), "master_id": master.id})
    verb = "Закрыть" if action == "block" else "Открыть"
    note = "Новые записи на этот день будут недоступны." if action == "block" else "Свободные окна снова появятся у клиентов."
    if action == "unblock" and not is_working_day(day):
        note = "Ручное закрытие будет снято. По графику 2/2 этот день остаётся выходным."
    await message.answer(
        f"{verb.upper()} {day:%d.%m.%Y}?\n\nМастер: {master.name}\n{note}",
        reply_markup=confirmation_keyboard(token, f"Да, {verb.lower()} день"), parse_mode=None,
    )


async def apply_pending(message: Message, state: FSMContext, booking_store: BookingStore, token: str) -> None:
    pending = (await state.get_data()).get("admin_pending")
    if not pending or pending.get("token") != token:
        await message.answer("Это подтверждение устарело. Выберите действие снова.", reply_markup=home_keyboard())
        return
    await state.update_data(admin_pending=None)
    try:
        if pending["action"] == "cancel":
            await asyncio.to_thread(
                booking_store.cancel_booking, pending["booking_id"], user_id=None,
                expected_version=pending["version"], now=now_local(),
            )
            await message.answer(
                f"Запись №{pending['booking_id']} отменена. Время снова доступно.\nКлиент увидит статус в разделе «Мои записи».",
                reply_markup=home_keyboard(),
            )
        else:
            day = date.fromisoformat(pending["day"])
            first = now_local().date()
            if not first <= day < first + timedelta(days=BOOKING_DAYS):
                await message.answer("Дата уже недоступна. Откройте рабочие дни снова.", reply_markup=home_keyboard())
                return
            if pending["action"] == "block":
                await asyncio.to_thread(booking_store.block_day, pending["master_id"], day, tz=BUSINESS_TZ)
                await message.answer(f"{day:%d.%m.%Y} закрыт для новых записей.")
            else:
                await asyncio.to_thread(booking_store.unblock_day, pending["master_id"], day)
                await message.answer(f"Ручное закрытие на {day:%d.%m.%Y} снято.")
            await show_days(message, booking_store)
    except SlotUnavailableError:
        await message.answer("На этот день есть подтверждённые записи. Сначала отмените или перенесите их, затем закройте день.", reply_markup=home_keyboard())
    except (BookingNotFoundError, BookingChangedError, InvalidBookingActionError):
        await message.answer("Запись уже изменилась или недоступна для этого действия. Обновите расписание.", reply_markup=home_keyboard())


async def admin_callback(callback: CallbackQuery, state: FSMContext, settings: Settings, booking_store: BookingStore) -> None:
    message = callback.message
    if not isinstance(message, Message) or not allowed(message, callback.from_user.id, settings):
        await callback.answer("Кабинет доступен только мастеру в личном чате.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    await callback.answer()
    await state.set_state(None)
    if action != "apply":
        await state.update_data(admin_pending=None)
    try:
        if action == "home" and len(parts) == 2:
            await show_dashboard(message, booking_store, settings)
        elif action == "list" and len(parts) == 4 and parts[2] in PERIOD_NAMES:
            await show_records(message, booking_store, parts[2], int(parts[3]))
        elif action == "card" and len(parts) == 3:
            await show_card(message, booking_store, int(parts[2]))
        elif action == "cancel" and len(parts) == 4:
            await confirm_cancel(message, state, booking_store, int(parts[2]), int(parts[3]))
        elif action == "days" and len(parts) == 2:
            await show_days(message, booking_store)
        elif action == "day" and len(parts) == 4 and parts[3] in {"block", "unblock"}:
            await confirm_day(message, state, booking_store, date.fromisoformat(parts[2]), parts[3])
        elif action == "apply" and len(parts) == 3:
            await apply_pending(message, state, booking_store, parts[2])
        elif action == "export" and len(parts) == 2:
            await export_records(message, booking_store)
        else:
            await message.answer("Кнопка устарела. Откройте кабинет снова командой /admin.", reply_markup=home_keyboard())
    except (ValueError, OverflowError):
        await message.answer("Кнопка недоступна. Откройте кабинет снова командой /admin.", reply_markup=home_keyboard())


def create_router() -> Router:
    admin_router = Router(name="admin")
    admin_router.message.register(admin_handler, Command("admin"))
    admin_router.message.register(admin_handler, F.text == "🛠 Кабинет мастера")
    admin_router.message.register(admin_export_handler, Command("admin_export"))
    admin_router.callback_query.register(admin_callback, F.data.startswith("adm:"))
    return admin_router


router = create_router()
