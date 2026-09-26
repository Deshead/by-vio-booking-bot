from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.catalog import now_local
from app.handlers.appointments import show_appointments
from app.handlers.booking import begin_booking, menu_for
from app.storage import BookingStore


router = Router(name="menu")


@router.message(Command("book"))
@router.message(F.text == "📝 Записаться")
async def booking_handler(message: Message, state: FSMContext, settings=None):
    await begin_booking(message, state, settings)


@router.message(Command("my"))
@router.message(F.text == "📋 Мои записи")
async def my_bookings_handler(message: Message, state: FSMContext, booking_store: BookingStore, settings=None):
    if message.chat.type != "private" or message.from_user is None:
        await message.answer("Ваши записи доступны в личном чате с ботом.")
        return
    await state.clear()
    await show_appointments(message, booking_store, message.from_user.id, settings, restore_menu=True)


@router.message(F.text == "ℹ️ О нас")
async def about_handler(message: Message, state: FSMContext, settings=None):
    await state.clear()
    await message.answer(
        "BY VIO · САЛОН\n\n"
        "Бьюти Мастер Виолетта\n"
        "Салон «Ювента»\n\n"
        "Маникюр · Френч · Педикюр\n"
        "🕒 09:00–18:00 · график 2/2\n"
        "Свободные даты — в разделе записи.\n\n"
        "Телефон: 88005553535",
        reply_markup=menu_for(message.chat.id, settings),
    )
