"""Private appointment cards, confirmed cancellation and safe rescheduling."""

import asyncio
from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.catalog import BUSINESS_TZ, TIMEZONE_LABEL, now_local
from app.handlers.booking import begin_reschedule, menu_for
from app.storage import Booking, BookingChangedError, BookingNotFoundError, BookingStore, InvalidBookingActionError

router = Router(name="appointments")


def appointment_text(booking: Booking, *, demo: bool = False) -> str:
    start = booking.starts_at.astimezone(BUSINESS_TZ)
    end = booking.ends_at.astimezone(BUSINESS_TZ)
    current = now_local()
    if booking.status == "cancelled":
        status = "Отменена"
    elif booking.ends_at <= current:
        status = "Завершена"
    elif booking.starts_at <= current:
        status = "В процессе"
    else:
        status = "Подтверждена"
    title = "Демо-запись" if demo else "Запись"
    return (
        f"{title} №{booking.id} · {status}\n\n"
        f"{booking.service_name}\nМастер: {booking.master_name}\n\n"
        f"📅 {start:%d.%m.%Y}\n🕒 {start:%H:%M}–{end:%H:%M}\n{TIMEZONE_LABEL}\n\n"
        f"Имя: {booking.client_name}\nТелефон: {booking.phone}\n"
        f"🔔 За 2 часа: {'включено' if booking.reminders_enabled else 'выключено'}"
    )


def list_button(label: str, section: str, page: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=f"appt:list:{section}:{page}")


async def show_appointments(
    message: Message, booking_store: BookingStore, user_id: int, settings=None,
    *, section: str = "upcoming", page: int = 0, restore_menu: bool = False,
) -> None:
    items = await asyncio.to_thread(booking_store.list_user_bookings, user_id)
    current = now_local()
    upcoming = sorted(
        (item for item in items if item.status == "confirmed" and item.ends_at > current),
        key=lambda item: item.starts_at,
    )
    history = sorted(
        (item for item in items if item.status != "confirmed" or item.ends_at <= current),
        key=lambda item: item.starts_at, reverse=True,
    )
    selected = history if section == "history" else upcoming
    page = min(max(page, 0), max(0, len(selected) - 1))
    heading = "ИСТОРИЯ" if section == "history" else "МОИ ЗАПИСИ"
    rows = []
    if selected:
        booking = selected[page]
        text = f"BY VIO · {heading}\n\n{appointment_text(booking, demo=bool(settings and settings.demo_mode))}"
        text += f"\n\n{page + 1} из {len(selected)}"
        if booking.status == "confirmed" and booking.starts_at > current:
            rows.append([
                InlineKeyboardButton(text="🗓 Перенести", callback_data=f"appt:move:{booking.id}:{booking.version}"),
                InlineKeyboardButton(text="Отменить", callback_data=f"appt:cancel:{booking.id}:{booking.version}"),
            ])
        arrows = []
        if page > 0:
            arrows.append(list_button("← Назад", section, page - 1))
        if page + 1 < len(selected):
            arrows.append(list_button("Далее →", section, page + 1))
        if arrows:
            rows.append(arrows)
    else:
        empty = "История пока пуста." if section == "history" else "У вас пока нет записей." if not items else "Предстоящих записей пока нет."
        text = f"BY VIO · {heading}\n\n{empty}\nВыбрать услугу — «📝 Записаться»."
    rows.append([
        list_button(f"{'• ' if section == 'upcoming' else ''}Предстоящие · {len(upcoming)}", "upcoming"),
        list_button(f"{'• ' if section == 'history' else ''}История · {len(history)}", "history"),
    ])
    if restore_menu and items:
        await message.answer(
            "BY VIO · ЛИЧНЫЙ КАБИНЕТ\nВаши записи — в карточках ниже.",
            reply_markup=menu_for(user_id, settings),
        )
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if items else menu_for(user_id, settings)
    await message.answer(text, reply_markup=markup, parse_mode=None)


@router.message(Command("bookings"))
async def bookings_command(message: Message, state: FSMContext, booking_store: BookingStore, settings=None) -> None:
    if message.chat.type != "private" or message.from_user is None:
        await message.answer("Ваши записи доступны в личном чате с ботом.")
        return
    await state.clear()
    await show_appointments(message, booking_store, message.from_user.id, settings, restore_menu=True)


@router.callback_query(F.data.startswith("appt:"))
async def appointment_callback(
    callback: CallbackQuery, state: FSMContext, booking_store: BookingStore, settings=None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != "private":
        await callback.answer("Записи доступны только в личном чате с ботом.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) not in {4, 5}:
        await callback.answer("Кнопка устарела. Откройте «📋 Мои записи».", show_alert=True)
        return
    action = parts[1]
    if action == "list":
        if len(parts) != 4 or parts[2] not in {"upcoming", "history"} or not parts[3].isascii() or not parts[3].isdecimal() or len(parts[3]) > 8:
            await callback.answer("Эта страница недоступна.", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await show_appointments(message, booking_store, callback.from_user.id, settings, section=parts[2], page=int(parts[3]))
        return
    if action not in {"move", "cancel", "cancelok"} or len(parts) != (5 if action == "cancelok" else 4):
        await callback.answer("Кнопка устарела. Откройте «📋 Мои записи».", show_alert=True)
        return
    try:
        booking_id, version = int(parts[2]), int(parts[3])
        if not (0 < booking_id < 2**63 and 0 < version < 2**63):
            raise ValueError
    except ValueError:
        await callback.answer("Эта запись недоступна.", show_alert=True)
        return
    if action == "cancelok":
        pending = (await state.get_data()).get("pending_cancellation", {})
        if pending != {"id": booking_id, "version": version, "token": parts[4]}:
            await callback.answer("Это подтверждение устарело. Откройте «📋 Мои записи».", show_alert=True)
            return
    await callback.answer()
    booking = await asyncio.to_thread(booking_store.get_booking, booking_id, user_id=callback.from_user.id)
    if booking is None:
        await message.answer("Эта запись недоступна. Откройте «📋 Мои записи».")
        return
    if booking.version != version or booking.status != "confirmed" or booking.starts_at <= now_local():
        await state.clear()
        await message.answer("Запись уже изменилась или действие недоступно. Откройте «📋 Мои записи» заново.")
        return
    if action == "move":
        await begin_reschedule(message, state, booking, settings)
    elif action == "cancel":
        await state.clear()
        token = uuid4().hex[:12]
        await state.update_data(pending_cancellation={"id": booking_id, "version": version, "token": token})
        await message.answer(
            f"BY VIO · ОТМЕНА\n\n{appointment_text(booking, demo=bool(settings and settings.demo_mode))}\n\nОтменить эту запись? Время освободится.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Да, отменить запись", callback_data=f"appt:cancelok:{booking_id}:{version}:{token}")],
                [list_button("Оставить запись", "upcoming")],
            ]), parse_mode=None,
        )
    else:
        try:
            await asyncio.to_thread(
                booking_store.cancel_booking, booking_id, user_id=callback.from_user.id,
                expected_version=version, now=now_local(),
            )
        except (BookingChangedError, BookingNotFoundError, InvalidBookingActionError):
            await state.clear()
            await message.answer("Запись уже изменилась. Откройте «📋 Мои записи» заново.")
            return
        await state.clear()
        label = "Демо-запись" if settings and settings.demo_mode else "Запись"
        await message.answer(
            f"BY VIO · ГОТОВО\n\n{label} №{booking_id} отменена.\nОна сохранена в истории.",
            reply_markup=menu_for(callback.from_user.id, settings),
        )
