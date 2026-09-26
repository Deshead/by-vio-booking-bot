from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.keyboards.main import main_menu_for
from app.settings import Settings


router = Router()


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext, settings: Settings):
    await state.clear()
    if message.chat.type != "private":
        await message.answer("Для записи на услуги откройте личный чат с ботом.")
        return
    text = (
        "BY VIO\n"
        "Маникюр • Френч • Педикюр\n\n"
        "Время для себя — начинается здесь ✨\n\n"
        "Выберите услугу и удобное окно. Я помогу записаться "
        "к Виолетте в салон «Ювента» и сохраню детали визита.\n\n"
        "🕘 09:00–18:00 · по графику мастера\n"
        "📍 Новосибирск · UTC+7\n\n"
        "Начнём? Нажмите «📝 Записаться» ↓"
    )
    if settings.demo_mode:
        text += "\n\nДЕМО · Ваши записи и кабинет доступны только вам. Реальная запись в салон не создаётся."
    await message.answer(text, reply_markup=main_menu_for(message.from_user.id, settings))
