from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.catalog import SERVICES


services_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text=service.name, callback_data=f"service_{service.id}")]
        for service in SERVICES.values()
    ]
)
