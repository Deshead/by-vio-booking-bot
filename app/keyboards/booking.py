"""Клавиатуры пошаговой записи на услугу."""

from datetime import date, datetime
from typing import Iterable

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def callback_data(flow: str, action: str, value: str = "") -> str:
    return f"book:{flow}:{action}:{value}"


def cancel_button(flow: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="Отмена", callback_data=callback_data(flow, "cancel")
    )


def masters_keyboard(flow: str, masters: Iterable) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=master.name, callback_data=callback_data(flow, "master", master.id))]
        for master in masters
    ]
    rows.append([cancel_button(flow)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_keyboard(flow: str, days: list[date], page: int = 0) -> InlineKeyboardMarkup:
    page_size = 7
    pages = max(1, (len(days) + page_size - 1) // page_size)
    page = min(max(page, 0), pages - 1)
    rows = [
        [InlineKeyboardButton(
            text=f"{day:%d.%m} · {WEEKDAYS[day.weekday()]}",
            callback_data=callback_data(flow, "date", day.isoformat()),
        )]
        for day in days[page * page_size : (page + 1) * page_size]
    ]
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(text="← Раньше", callback_data=callback_data(flow, "page", str(page - 1))))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton(text="Позже →", callback_data=callback_data(flow, "page", str(page + 1))))
    if navigation:
        rows.append(navigation)
    rows.append([cancel_button(flow)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def times_keyboard(flow: str, slots: list[datetime]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"{slot:%H:%M}", callback_data=callback_data(flow, "time", f"{slot:%Y%m%d%H%M}"))
        for slot in slots
    ]
    rows = [buttons[index:index + 3] for index in range(0, len(buttons), 3)]
    rows.append([InlineKeyboardButton(text="← Другая дата", callback_data=callback_data(flow, "dates"))])
    rows.append([cancel_button(flow)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirmation_keyboard(
    flow: str, confirmation_token: str, *, reminders_enabled: bool = False,
    rescheduling: bool = False,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'🔔' if reminders_enabled else '🔕'} Напомнить за 2 часа: {'вкл' if reminders_enabled else 'выкл'}",
            callback_data=callback_data(flow, "reminder", confirmation_token),
        )],
        [InlineKeyboardButton(text="✅ Подтвердить перенос" if rescheduling else "✅ Подтвердить запись", callback_data=callback_data(flow, "confirm", confirmation_token))],
        [InlineKeyboardButton(text="← Другая дата и время", callback_data=callback_data(flow, "dates"))],
        [cancel_button(flow)],
    ])


cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]], resize_keyboard=True
)

phone_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Отправить мой телефон", request_contact=True)],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)
