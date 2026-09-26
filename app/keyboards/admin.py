"""Compact inline controls for the private master workspace."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"adm:{data}")


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button("Сегодня", "list:today:0"), button("Завтра", "list:tomorrow:0")],
        [button("Расписание на 7 дней", "list:week:0")],
        [button("История и отмены", "list:history:0")],
        [button("Управление рабочими днями", "days")],
        [button("Скачать расписание · CSV", "export")],
    ])


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[button("← Кабинет мастера", "home")]])


def confirmation_keyboard(token: str, label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [button(label, f"apply:{token}")],
        [button("Оставить без изменений", "home")],
    ])
