import asyncio
from datetime import timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.catalog import BOOKING_DAYS, BUSINESS_TZ, MASTERS, SERVICES, TIMEZONE_LABEL, candidate_slots, now_local
from app.keyboards.main import main_menu_for
from app.settings import Settings
from app.storage import BookingStore, SlotUnavailableError


router = Router(name="commands")


@router.message(Command("myid"))
async def myid(message: Message):
    if message.chat.type != "private" or message.from_user is None:
        return
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(Command("help"))
@router.message(F.text == "❔ Помощь")
async def help_handler(message: Message, state: FSMContext, settings: Settings):
    if message.chat.type != "private" or message.from_user is None:
        await message.answer("Откройте личный чат с ботом — там доступны запись и кабинет.")
        return
    await state.clear()
    text = (
        "BY VIO · КАК ЭТО РАБОТАЕТ\n\n"
        "01  Выберите услугу и мастера.\n"
        "02  Найдите удобные дату и время.\n"
        "03  Укажите имя и телефон, проверьте данные.\n"
        "04  Подтвердите запись — готово!\n\n"
        "📋 Мои записи — перенос и отмена предстоящего визита.\n"
        "🔔 Напоминание за 2 часа можно включить перед подтверждением.\n"
        "/cancel — выйти из текущего оформления.\n\n"
        f"Время салона: {TIMEZONE_LABEL}.\n"
        "Связаться с салоном: 88005553535."
    )
    if settings.demo_mode:
        text += (
            "\n\n✨ Демо для портфолио\n"
            "У вас отдельный кабинет и пробные записи. Попробуйте «Пример записей», "
            "затем откройте кабинет мастера. Другие посетители ваши данные не видят."
        )
    await message.answer(text, reply_markup=main_menu_for(message.from_user.id, settings))


@router.message(Command("services"))
@router.message(F.text == "💅 Услуги")
async def services_handler(message: Message, state: FSMContext, settings: Settings):
    if message.chat.type != "private" or message.from_user is None:
        return
    await state.clear()
    lines = ["BY VIO · МЕНЮ УСЛУГ", ""]
    for service in SERVICES.values():
        lines.extend([service.name, f"Мастер Виолетта · {service.duration_minutes // 60} часа", ""])
    lines.extend(["Салон «Ювента»", "Нажмите «📝 Записаться», чтобы увидеть свободные окна."])
    await message.answer("\n".join(lines), reply_markup=main_menu_for(message.from_user.id, settings))


@router.message(Command("demo"))
@router.message(F.text == "✨ Пример записей")
async def demo_handler(message: Message, state: FSMContext, settings: Settings, booking_store: BookingStore):
    if message.chat.type != "private" or message.from_user is None:
        return
    if not settings.demo_mode:
        await message.answer("Примеры доступны только в демонстрационном режиме.")
        return
    await state.clear()
    user_id = message.from_user.id
    existing = await asyncio.to_thread(booking_store.list_user_bookings, user_id)
    if existing:
        await message.answer("Ваши пробные записи уже доступны в «📋 Мои записи» и кабинете мастера.",
                             reply_markup=main_menu_for(user_id, settings))
        return
    created = 0
    current = now_local()
    master = MASTERS["violetta"]
    for offset in range(BOOKING_DAYS):
        day = current.date() + timedelta(days=offset)
        for slot in candidate_slots(day, "manicure", now=current):
            try:
                await asyncio.to_thread(
                    booking_store.create_booking,
                    request_id=f"demo:{user_id}:{created}", user_id=user_id,
                    service_id="manicure", service_name=SERVICES["manicure"].name,
                    master_id=master.id, master_name=master.name,
                    starts_at=slot, duration_minutes=120,
                    client_name=("Анна · пример", "Мария · пример")[created],
                    phone="+70000000000", reminders_enabled=False,
                )
            except SlotUnavailableError:
                continue
            created += 1
            if created == 2:
                break
        if created == 2:
            break
    await message.answer(
        f"BY VIO · ВАШЕ ДЕМО\n\nСоздано примеров: {created}.\n"
        "Имена и номер в них вымышленные.\n\n"
        "Откройте «📋 Мои записи»: перенесите визит или отмените его. "
        "В кабинете мастера проверьте загрузку и управление выходными.",
        reply_markup=main_menu_for(user_id, settings),
    )
