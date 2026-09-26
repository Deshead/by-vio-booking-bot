"""Личная запись: услуга, мастер, время и контактные данные."""

import asyncio
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from app.catalog import (
    BOOKING_DAYS,
    BUSINESS_TZ,
    MASTERS,
    SERVICES,
    TIMEZONE_LABEL,
    candidate_slots,
    now_local,
)
from app.keyboards.main import main_menu_for
from app.settings import Settings
from app.keyboards.booking import (
    cancel_keyboard,
    confirmation_keyboard,
    dates_keyboard,
    masters_keyboard,
    phone_keyboard,
    times_keyboard,
)
from app.keyboards.services import services_keyboard
from app.storage import (
    Booking, BookingChangedError, BookingNotFoundError, BookingStore,
    InvalidBookingActionError, SlotUnavailableError,
)


router = Router(name="booking")
logger = logging.getLogger(__name__)


class BookingStates(StatesGroup):
    service = State()
    master = State()
    date = State()
    time = State()
    name = State()
    phone = State()
    confirm = State()


def normalize_name(value: str) -> str | None:
    """Разрешаем буквы разных языков, пробелы, дефисы и апострофы."""
    if any(unicodedata.category(char).startswith("C") for char in value):
        return None
    name = " ".join(value.split())
    if not 2 <= len(name) <= 80:
        return None
    if not all(unicodedata.category(char)[0] in {"L", "M"} or char in " -'’" for char in name):
        return None
    if sum(char.isalpha() for char in name) < 2:
        return None
    return name


def normalize_phone(value: str) -> str | None:
    compact = re.sub(r"[ ()\-.]", "", value.strip())
    if re.fullmatch(r"8[0-9]{10}", compact):
        compact = "+7" + compact[1:]
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", compact):
        return None
    if compact.startswith("+7") and len(compact) != 12:
        return None
    return compact


def menu_for(user_id: int, settings=None):
    return main_menu_for(user_id, settings or Settings(demo_mode=False))


async def begin_booking(message: Message, state: FSMContext, settings=None) -> None:
    if message.chat.type != "private":
        await message.answer("Для записи откройте личный чат с ботом и нажмите /start.")
        return
    await state.clear()
    await state.update_data(
        flow_id=uuid4().hex[:12], request_id=uuid4().hex,
        reminders_enabled=False, demo_mode=bool(settings and settings.demo_mode),
    )
    await state.set_state(BookingStates.service)
    await message.answer("BY VIO · ЗАПИСЬ\n\n1/6 · Услуга\nВыберите уход, который вам подходит.", reply_markup=services_keyboard)


async def begin_reschedule(
    message: Message, state: FSMContext, booking: Booking, settings=None,
) -> None:
    if message.chat.type != "private":
        await message.answer("Перенос доступен в личном чате с ботом.")
        return
    if booking.service_id not in SERVICES or booking.master_id not in MASTERS:
        await message.answer("Для переноса этой услуги свяжитесь с салоном: 88005553535.")
        return
    await state.clear()
    await state.update_data(
        flow_id=uuid4().hex[:12], request_id=uuid4().hex,
        reschedule_id=booking.id, expected_version=booking.version,
        service_id=booking.service_id, service_name=booking.service_name,
        master_id=booking.master_id, master_name=booking.master_name,
        duration_minutes=booking.duration_minutes,
        client_name=booking.client_name, phone=booking.phone,
        reminders_enabled=booking.reminders_enabled,
        original_starts_at=booking.starts_at.isoformat(),
        demo_mode=bool(settings and settings.demo_mode),
    )
    await message.answer(
        f"BY VIO · ПЕРЕНОС №{booking.id}\n\n"
        "Выберите новую дату и время.\nПрежняя запись остаётся за вами до подтверждения."
    )
    await show_dates(message, state)


def available_days(service_id: str, duration_minutes: int | None = None) -> list[date]:
    current = now_local()
    days = [current.date() + timedelta(days=offset) for offset in range(BOOKING_DAYS)]
    return [day for day in days if candidate_slots(day, service_id, now=current, duration_minutes=duration_minutes)]


async def free_slots(data: dict, booking_store: BookingStore) -> list[datetime]:
    day = date.fromisoformat(data["date"])
    slots = candidate_slots(day, data["service_id"], duration_minutes=data.get("duration_minutes"))
    busy = await asyncio.to_thread(
        booking_store.busy_intervals, data["master_id"], day, tz=BUSINESS_TZ,
        exclude_booking_id=data.get("reschedule_id"),
    )
    duration = timedelta(minutes=data.get("duration_minutes", SERVICES[data["service_id"]].duration_minutes))
    return [slot for slot in slots if not any(slot < end and slot + duration > start for start, end in busy)]


async def show_dates(message: Message, state: FSMContext, *, page: int = 0) -> None:
    data = await state.get_data()
    await state.set_state(BookingStates.date)
    days = available_days(data["service_id"], data.get("duration_minutes"))
    if not days:
        await message.answer("В ближайшие дни нет доступных дат для этой услуги. Попробуйте записаться позже или выберите другую услугу через меню.", reply_markup=dates_keyboard(data["flow_id"], []))
        return
    await message.answer(
        f"BY VIO · ЗАПИСЬ\n\n3/6 · Дата\nВыберите дату на ближайшую неделю.\nВремя: {TIMEZONE_LABEL}.",
        reply_markup=dates_keyboard(data["flow_id"], days, page),
    )


async def show_times(message: Message, state: FSMContext, booking_store: BookingStore) -> None:
    data = await state.get_data()
    slots = await free_slots(data, booking_store)
    await state.set_state(BookingStates.time)
    day = date.fromisoformat(data["date"])
    duration = data.get("duration_minutes", SERVICES[data["service_id"]].duration_minutes)
    duration_text = f"{duration // 60} ч" if duration % 60 == 0 else f"{duration} мин"
    text = (
        f"BY VIO · ЗАПИСЬ\n\n4/6 · Время\n"
        f"{day:%d.%m.%Y} · {duration_text}\n{TIMEZONE_LABEL}\n\nВыберите удобное время:"
        if slots else "На эту дату свободного времени уже нет. Выберите другую дату."
    )
    await message.answer(text, reply_markup=times_keyboard(data["flow_id"], slots))


def booking_summary(data: dict) -> str:
    starts_at = datetime.fromisoformat(data["starts_at"]).astimezone(BUSINESS_TZ)
    service = SERVICES[data["service_id"]]
    master = MASTERS[data["master_id"]]
    ends_at = starts_at + timedelta(minutes=data.get("duration_minutes", service.duration_minutes))
    reminder = "включено" if data.get("reminders_enabled", False) else "выключено"
    return (
        f"Услуга: {data.get('service_name', service.name)}\n"
        f"Мастер: {data.get('master_name', master.name)}\n"
        f"Дата: {starts_at:%d.%m.%Y}\n"
        f"Время: {starts_at:%H:%M}–{ends_at:%H:%M} ({TIMEZONE_LABEL})\n"
        f"Имя: {data['client_name']}\n"
        f"Телефон: {data['phone']}\n\n"
        f"🔔 Напоминание за 2 часа: {reminder}"
    )


async def show_confirmation(message: Message, state: FSMContext, *, replace: bool = False) -> None:
    await state.update_data(confirmation_token=uuid4().hex[:12])
    data = await state.get_data()
    await state.set_state(BookingStates.confirm)
    title = "ДЕМО-ЗАПИСЬ" if data.get("demo_mode") else "ЗАПИСЬ"
    text = f"BY VIO · {title}\n\n6/6 · Проверьте данные\n\n{booking_summary(data)}"
    if data.get("reschedule_id"):
        original = datetime.fromisoformat(data["original_starts_at"]).astimezone(BUSINESS_TZ)
        text += f"\n\nПеренос №{data['reschedule_id']}\nПрежнее время: {original:%d.%m · %H:%M}"
    if not replace:
        await message.answer("Почти готово — подтвердите запись ниже.", reply_markup=ReplyKeyboardRemove())
    send = message.edit_text if replace else message.answer
    await send(
        text,
        reply_markup=confirmation_keyboard(
            data["flow_id"], data["confirmation_token"],
            reminders_enabled=data.get("reminders_enabled", False),
            rescheduling=bool(data.get("reschedule_id")),
        ),
        parse_mode=None,
    )


async def cancel_booking(message: Message, state: FSMContext, settings=None) -> None:
    current_state = await state.get_state()
    data = await state.get_data()
    await state.clear()
    text = (
        "Оформление записи отменено. Выберите действие в меню."
        if current_state else "Сейчас нет незавершённой записи. Выберите действие в меню."
    )
    if data.get("reschedule_id"):
        text = "Перенос отменён. Прежняя запись сохранена."
    await message.answer(text, reply_markup=menu_for(message.chat.id, settings))


@router.message(Command("cancel"))
@router.message(F.text.in_({"Отмена", "❌ Отмена"}))
async def cancel_handler(message: Message, state: FSMContext, settings=None) -> None:
    if message.chat.type != "private":
        await message.answer("Для записи используйте личный чат с ботом.")
        return
    await cancel_booking(message, state, settings)


@router.callback_query(F.data.startswith("service_"))
async def service_handler(callback: CallbackQuery, state: FSMContext) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != "private":
        await callback.answer("Для записи откройте личный чат с ботом.", show_alert=True)
        return
    service_id = (callback.data or "").removeprefix("service_")
    if await state.get_state() != BookingStates.service.state or service_id not in SERVICES:
        await callback.answer("Эта кнопка устарела. Нажмите «📝 Записаться» в меню.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(service_id=service_id)
    await state.set_state(BookingStates.master)
    data = await state.get_data()
    await message.answer("BY VIO · ЗАПИСЬ\n\n2/6 · Ваш мастер\nВыберите специалиста:", reply_markup=masters_keyboard(data["flow_id"], MASTERS.values()))


@router.callback_query(F.data.startswith("book:"))
async def booking_callback(callback: CallbackQuery, state: FSMContext, booking_store: BookingStore, settings=None) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != "private":
        await callback.answer("Для записи откройте личный чат с ботом.", show_alert=True)
        return
    parts = (callback.data or "").split(":", 3)
    if len(parts) != 4:
        await callback.answer("Кнопка недоступна. Начните запись через меню.", show_alert=True)
        return
    _, flow, action, value = parts
    data = await state.get_data()
    current_state = await state.get_state()
    if action == "confirm" and flow == data.get("last_confirmed_flow"):
        await callback.answer("Эта запись уже подтверждена. Посмотрите «📋 Мои записи».", show_alert=True)
        return
    expected_states = {
        "master": {BookingStates.master.state},
        "page": {BookingStates.date.state},
        "date": {BookingStates.date.state},
        "time": {BookingStates.time.state},
        "dates": {BookingStates.time.state, BookingStates.confirm.state},
        "confirm": {BookingStates.confirm.state},
        "reminder": {BookingStates.confirm.state},
        "cancel": {item.state for item in BookingStates.__all_states__},
    }
    if flow != data.get("flow_id") or current_state not in expected_states.get(action, set()):
        await callback.answer("Эта кнопка устарела. Используйте последнее сообщение бота или начните запись через меню.", show_alert=True)
        return
    if action == "master" and value not in MASTERS:
        await callback.answer("Этот мастер недоступен.", show_alert=True)
        return
    if action == "page" and (not value.isascii() or not value.isdecimal() or len(value) > 3):
        await callback.answer("Эта страница недоступна.", show_alert=True)
        return
    if action == "date":
        try:
            chosen_day = date.fromisoformat(value)
        except ValueError:
            await callback.answer("Эта дата недоступна.", show_alert=True)
            return
        if chosen_day not in available_days(data["service_id"], data.get("duration_minutes")):
            await callback.answer("Эта дата уже недоступна. Выберите другую дату.", show_alert=True)
            await show_dates(message, state)
            return
    if action == "time" and not re.fullmatch(r"[0-9]{12}", value):
        await callback.answer("Это время недоступно. Выберите время в последнем сообщении бота.", show_alert=True)
        return
    if action == "time" and not value.startswith(data["date"].replace("-", "")):
        await callback.answer("Это кнопка другой даты. Выберите время в новом сообщении бота.", show_alert=True)
        await show_times(message, state, booking_store)
        return
    if action in {"confirm", "reminder"} and value != data.get("confirmation_token"):
        await callback.answer("Данные записи изменились. Подтвердите их в последнем сообщении бота.", show_alert=True)
        return
    await callback.answer()
    if action == "cancel":
        await cancel_booking(message, state, settings)
    elif action == "reminder":
        await state.update_data(reminders_enabled=not data.get("reminders_enabled", False))
        await show_confirmation(message, state, replace=True)
    elif action == "master":
        await state.update_data(master_id=value)
        await show_dates(message, state)
    elif action == "page":
        await show_dates(message, state, page=int(value))
    elif action == "dates":
        await show_dates(message, state)
    elif action == "date":
        await state.update_data(date=value)
        await show_times(message, state, booking_store)
    elif action == "time":
        slots = await free_slots(data, booking_store)
        chosen = next((slot for slot in slots if slot.strftime("%Y%m%d%H%M") == value), None)
        if chosen is None:
            await message.answer("Это время уже занято или недоступно. Выберите другое.")
            await show_times(message, state, booking_store)
            return
        await state.update_data(starts_at=chosen.isoformat())
        if data.get("client_name") and data.get("phone"):
            await show_confirmation(message, state)
        else:
            await state.set_state(BookingStates.name)
            await message.answer("BY VIO · ЗАПИСЬ\n\n5/6 · Знакомство\nКак вас зовут? Напишите имя.", reply_markup=cancel_keyboard)
    elif action == "confirm":
        await confirm_booking(callback, state, booking_store, settings)


@router.message(BookingStates.name, F.chat.type == "private")
async def name_handler(message: Message, state: FSMContext) -> None:
    name = normalize_name(message.text or "")
    if name is None:
        await message.answer("Введите имя от 2 до 80 символов, используя буквы, пробелы или дефис.", reply_markup=cancel_keyboard)
        return
    await state.update_data(client_name=name)
    await state.set_state(BookingStates.phone)
    await message.answer(
        "BY VIO · ЗАПИСЬ\n\n5/6 · Контакты\nОставьте телефон для связи.\n\nНажмите кнопку или введите номер:\n+79991234567 / 89991234567",
        reply_markup=phone_keyboard,
    )


@router.message(BookingStates.phone, F.chat.type == "private")
async def phone_handler(message: Message, state: FSMContext) -> None:
    if message.contact:
        if not message.from_user or message.contact.user_id != message.from_user.id:
            await message.answer("Нужен ваш номер. Нажмите «📱 Отправить мой телефон» или введите свой номер вручную.", reply_markup=phone_keyboard)
            return
        raw_phone = message.contact.phone_number
        if re.fullmatch(r"[1-9][0-9]{7,14}", raw_phone):
            raw_phone = "+" + raw_phone
    else:
        raw_phone = message.text or ""
    phone = normalize_phone(raw_phone)
    if phone is None:
        await message.answer("Не удалось распознать телефон. Пример: +79991234567 или 89991234567. Международный номер: + и от 8 до 15 цифр.", reply_markup=phone_keyboard)
        return
    await state.update_data(phone=phone)
    await show_confirmation(message, state)


async def confirm_booking(callback: CallbackQuery, state: FSMContext, booking_store: BookingStore, settings=None) -> None:
    message = callback.message
    if not isinstance(message, Message):
        return
    data = await state.get_data()
    starts_at = datetime.fromisoformat(data["starts_at"]).astimezone(BUSINESS_TZ)
    service = SERVICES[data["service_id"]]
    if starts_at not in candidate_slots(starts_at.date(), service.id, duration_minutes=data.get("duration_minutes")):
        await message.answer("Выбранное время уже недоступно. Выберите новое; имя и телефон сохранены в текущей форме.")
        await show_dates(message, state)
        return
    master = MASTERS[data["master_id"]]
    try:
        if data.get("reschedule_id"):
            booking = await asyncio.to_thread(
                booking_store.reschedule_booking,
                data["reschedule_id"], user_id=callback.from_user.id,
                expected_version=data["expected_version"], starts_at=starts_at,
                now=now_local(), reminders_enabled=data.get("reminders_enabled", False),
            )
        else:
            booking = await asyncio.to_thread(
                booking_store.create_booking,
                request_id=data["request_id"],
                user_id=callback.from_user.id,
                service_id=service.id,
                service_name=service.name,
                master_id=master.id,
                master_name=master.name,
                starts_at=starts_at,
                duration_minutes=service.duration_minutes,
                client_name=data["client_name"],
                phone=data["phone"],
                reminders_enabled=data.get("reminders_enabled", False),
            )
    except SlotUnavailableError:
        await message.answer("Это время только что заняли. Выберите другое; имя и телефон сохранены в текущей форме.")
        await show_times(message, state, booking_store)
        return
    except (BookingChangedError, BookingNotFoundError, InvalidBookingActionError):
        await state.clear()
        await message.answer(
            "Запись уже изменилась или перенос недоступен. Откройте «📋 Мои записи» заново.",
            reply_markup=menu_for(callback.from_user.id, settings),
        )
        return
    except Exception:
        logger.exception("Не удалось сохранить запись")
        await message.answer("Не удалось сохранить запись. Попробуйте снова нажать «Подтвердить запись» через минуту.")
        return
    await state.clear()
    await state.update_data(last_confirmed_flow=data["flow_id"], last_booking_id=booking.id)
    label = "Демо-запись" if data.get("demo_mode") else "Запись"
    outcome = "перенесена" if data.get("reschedule_id") else "подтверждена"
    await message.answer(
        f"BY VIO · ГОТОВО\n\n✅ {label} №{booking.id} {outcome}!\n\n{booking_summary(data)}\n\nУправление записью — в «📋 Мои записи».",
        reply_markup=menu_for(callback.from_user.id, settings),
        parse_mode=None,
    )


@router.message(StateFilter(BookingStates), F.chat.type == "private")
async def booking_message_fallback(message: Message) -> None:
    await message.answer("Выберите вариант кнопкой в последнем сообщении бота. Для отмены записи отправьте /cancel.")


@router.message(StateFilter(None), F.chat.type == "private", F.contact)
@router.message(StateFilter(None), F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def idle_message_handler(message: Message, settings=None) -> None:
    await message.answer(
        "Чтобы оформить запись, нажмите «📝 Записаться». Если оформление прервалось, начните заново. Подтверждённые записи доступны в «📋 Мои записи».",
        reply_markup=menu_for(message.chat.id, settings),
    )
