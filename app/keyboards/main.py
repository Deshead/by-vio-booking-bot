from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from app.settings import Settings


def main_menu_for(user_id: int, settings: Settings) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📝 Записаться")],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="💅 Услуги")],
        [KeyboardButton(text="ℹ️ О нас"), KeyboardButton(text="❔ Помощь")],
    ]
    if settings.is_admin(user_id):
        rows.append([KeyboardButton(text="🛠 Кабинет мастера")])
    if settings.demo_mode:
        rows.append([KeyboardButton(text="✨ Пример записей")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие ↓",
    )
