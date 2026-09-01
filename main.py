# -*- coding: utf-8 -*-
"""
Qwitty Bot — объединённый UserBot + GuardBot.

Один Bot/Dispatcher:
- /start -> главное меню с UserBot / GuardBot
- UserBot сохраняет существующую авторизацию и функции Pyrogram
- GuardBot сохраняет существующую систему групп, инвайтов, антиспама и модерации
"""
import asyncio
import sys
import os
import datetime
import time
import glob
import logging
import re
import json
import random
import psutil
from typing import Optional

from aiohttp import web
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, types, F, BaseMiddleware
from aiogram.filters import CommandStart, ChatMemberUpdatedFilter, IS_NOT_MEMBER, MEMBER
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Message, ChatPermissions, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatMemberUpdated,
)
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from pyrogram import Client, enums, filters
from pyrogram.handlers import MessageHandler
from pyrogram.raw import functions
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, Unauthorized, FloodWait

from supabase import create_client, AsyncClient, acreate_client, Client as SupabaseClient

load_dotenv()

if sys.platform != "win32":
    try:
        import uvloop
        uvloop.install()
        print("🔥 [Движок]: uvloop успешно активирован (Linux/macOS)")
    except ImportError:
        print("⚠️ [Движок]: uvloop не установлен, используется стандартный asyncio")
else:
    print("💻 Запуск Скрипта")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "") or SUPABASE_SERVICE_ROLE_KEY
WEB_PORT = int(os.getenv("PORT", "10000"))
BOT_USERNAME = os.getenv("@Qwitty_bot")
ADD_URL = (
    f"https://t.me/{BOT_USERNAME}?startgroup=true&admin="
    "restrict_members+delete_messages+ban_users+invite_users+pin_messages"
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

RU_MONTHS = {
    1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля",
    5: "Мая", 6: "Июня", 7: "Июля", 8: "Августа",
    9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
}

def format_date_ru(dt):
    return f"{dt.day} {RU_MONTHS.get(dt.month, '')} {dt.year} года"

TIMEZONE_NAMES = {
    2: "Athens/Cairo UTC+2",
    3: "Moscow/Istanbul UTC+3",
    4: "Baku/Tbilisi UTC+4",
    5: "Tashkent/Almaty UTC+5",
    6: "Astana/Dhaka UTC+6",
    7: "Bangkok/Jakarta UTC+7",
    8: "Beijing/Singapore UTC+8",
    9: "Tokyo/Seoul UTC+9"
}

REGISTRATION_FLOOD_SECONDS_DEFAULT = 0
USER_MESSAGE_DELETE_DELAY = 3


def format_remaining_time(seconds):
    """Возвращает оставшееся время в удобном виде, включая секунды."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        parts = [f"{days} дн."]
        if hours:
            parts.append(f"{hours} ч.")
        if minutes:
            parts.append(f"{minutes} мин.")
        if secs:
            parts.append(f"{secs} сек.")
        return " ".join(parts)
    if hours:
        parts = [f"{hours} ч."]
        if minutes:
            parts.append(f"{minutes} мин.")
        if secs:
            parts.append(f"{secs} сек.")
        return " ".join(parts)
    if minutes:
        return f"{minutes} мин. {secs} сек." if secs else f"{minutes} мин."
    return f"{secs} сек."


def get_registration_block_until(user_id):
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str) or {}
    try:
        return float(cfg.get("registration_block_until_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def get_registration_block_remaining(user_id):
    return max(0, int(get_registration_block_until(user_id) - time.time() + 0.999))


def is_registration_blocked(user_id):
    return get_registration_block_until(user_id) > time.time()

TEXTS = {
    "btn_start": "Начинаем 🚀", 
    "btn_rules": "Правила 📜",
    "btn_back": "Назад 🔙", 
    "btn_back_menu": "Назад в меню 🔙", 
    "btn_confirm": "Подтвердить ✅", 
    "btn_activity": "Активность 📊",
    "btn_autoresp": "Автоответчик 🤖", 
    "btn_timenick": "Время в профиль 🕒", 
    "btn_247": "Режим 24/7 ⚡️",
    "btn_turn_on": "Включить ▶️",
    "btn_turn_off": "Выключить ❌", 
    "btn_tz_select": "Выбрать часовой пояс 🕒", 
    "btn_refresh": "Обновить 🔄",
    "btn_autoresp_setup": "Изменить текст 📝",
    "btn_im_sure": "Я уверен ✅", 
    "btn_register": "Регистрироваться 📝",
    "btn_userbot": "UserBot 🤖",
    "btn_guardbot": "GuardBot 🛡",
    "msg_root_menu": "Добро пожаловать в Qwitty bot!\n\nВот список наших функций:",
    "msg_userbot_intro": "Перед регистрацией ознакомьтесь с правилами:",
    "msg_start": "Здравствуйте!\nДобро пожаловать в бота автоматизированного управления аккаунтом.\nОзнакомьтесь с правилами.",
    "msg_start_register": "Чтобы зарегистрироваться заново, нажмите кнопку ниже 👇",
    "msg_menu": "Что умеет этот бот?\nВыбирайте доступные функции управления вашим аккаунтом на кнопках снизу:",
    "msg_rules_text": "📜 **Правила использования бота:**\n\n1. Бот только для ознакомительных целей.\n2. Бот работает через юзербота.\n3. Не авторизуйтесь слишком часто.\n5. Все действия автоматизированы.\n\n_Соблюдайте правила для безопасности._",
    "msg_rules_done": "Всё, правила прочитаны! 👍\n\nЖмите кнопку начала ниже, чтобы привязать аккаунт.",
    "msg_phone_req": "Пожалуйста, отправьте ваш номер телефона в международном формате.\nПример: +12345678",
    "msg_code_req": "Код авторизации отправлен в Telegram.\n⚠️ Напишите код через дефис.\nПример: 12-45-6",
    "msg_pwd_req": "Аккаунт защищен облачным паролем.\nВведите его в чат:",
    "msg_success_login": "Бот успешно зашел в аккаунт!\nНажмите кнопку ниже для продолжения.",
    "msg_btn_go": "Поехали ➡️",
    "status_on": "Включен 🟢", 
    "status_off": "Выключен 🔴",
    "msg_already_logged": "Вы уже авторизованы! Переходим в меню...",
    "msg_auth_canceled": "Авторизация отменена.", 
    "msg_sending_req": "Отправка запроса... Подождите.",
    "msg_limit_tg": "⚠️ **Вы поймали флуд от Telegram!**\n\nСлишком часто запрашивалась регистрация/код.\nПовторите через **{0}**.",
    "msg_error_send_code": "Ошибка при отправке кода: {0}\nПопробуйте снова через /start",
    "msg_auth_err": "Произошла ошибка: {0}\nПерезапустите через /start",
    "msg_session_lost": "Сессия разорвана.\nНачните заново через /start",
    "msg_session_missing": "⚠️ Сессия отсутствует.\nНажмите кнопку ниже, чтобы зарегистрироваться заново.",
    "msg_session_revoked": "⚠️ Юзербот отключен.\nПричина: {0}.\n\nНажмите кнопку ниже, чтобы зарегистрироваться заново.",
    "msg_check_code": "🔐 Проверка кода...\n⏳ Осталось: {0} сек.",
    "msg_code_wrong": "Неправильный код.\nНапишите код заново:",
    "msg_check_pwd": "🔐 Проверка 2FA...\n⏳ Осталось: {0} сек.",
    "msg_pwd_wrong": "❌ Неверный пароль!\nВведите заново:",
    "msg_pwd_ok": "Пароль принят!\nЮзербот успешно запущен.",
    "msg_activity_text": "Ваша история активности (за 5 дней):\n\n{0}",
    "msg_timenick_text": "Вывод текущего времени в имя профиля.\n\nТекущий статус: {0}\nПрофиль: {1}\nСмещение часового пояса: UTC+{2}",
    "msg_tz_select": "Выберите ваш часовой пояс👇", 
    "msg_tz_saved": "Часовой пояс изменен на UTC+{0}!",
    "msg_autoresp_text": "🤖 **Автоответчик**\n\nСтатус: {1}\nТекст приветствия:\n👉 \"{0}\"",
    "msg_autoresp_req": "Напишите новый текст приветствия в чат 👇", 
    "msg_autoresp_saved": "Приветствие успешно сохранено! ✅",
    "msg_autoresp_default": "👋 Здравствуйте! Сейчас я не в сети, отвечу позже.",
    "msg_247_text": "⚡️ **Режим 24/7**\n\nСтатус: {0}\nРаботает без суточного лимита.",
    "msg_limit_247_reached": "Режим 24/7 больше не имеет суточного лимита."
}

PROFILE_TIME_OFFSET_SECONDS = 0

def unstyle_text(text):
    return text or ""

def get_current_styled_profile_preview(base_first, base_last, offset, include_nick=True, include_time=True):
    clean_first = (base_first or "User").strip() or "User"
    clean_last = (base_last or "").strip()
    first = clean_first
    last = clean_last
    if include_time:
        tz_now = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=offset, seconds=60)
            + datetime.timedelta(seconds=PROFILE_TIME_OFFSET_SECONDS)
        )
        raw_time = tz_now.strftime("%H:%M")
        time_marker = f"[{raw_time}]"
        if last:
            last = f"{last} {time_marker}"
        else:
            first = f"{first} {time_marker}"
    return f"{first}\n{last}" if last else first

async def ensure_profile_base(user_id, me=None):
    data = get_user_state(user_id)
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    if me is None and data.get("client") and data["client"].is_connected:
        me = await data["client"].get_me()
    if me:
        if not cfg.get("profile_base_first_name"):
            clean_first = re.sub(r"\s*\[[^\]]+\]", "", me.first_name or "User").strip()
            cfg["profile_base_first_name"] = clean_first or "User"
        if not cfg.get("profile_base_last_name"):
            clean_last = re.sub(r"\s*\[[^\]]+\]", "", me.last_name or "").strip()
            cfg["profile_base_last_name"] = clean_last
        MEMORY_DB["config"][uid_str] = cfg
        asyncio.create_task(async_db_save("config", uid_str, cfg))
    return cfg

MEMORY_DB = {"config": {}, "activity": {}, "logs": {}}
USER_DATA = {}

def db_get_data(table: str, user_id: str):
    if not supabase:
        return {}
    try:
        res = supabase.table(table).select("data").eq("id", str(user_id)).execute()
        if res.data:
            return res.data[0].get("data", {})
    except Exception as e:
        logging.error(f"Error fetching Supabase {table}: {e}")
    return {}

def db_get_all_config():
    if not supabase:
        return []
    try:
        res = supabase.table("config").select("id, data").execute()
        return res.data or []
    except Exception as e:
        logging.error(f"Error fetching all Supabase configs: {e}")
        return []

def db_save_data(table: str, user_id: str, data: dict):
    if not supabase:
        return
    try:
        supabase.table(table).upsert({"id": str(user_id), "data": data}).execute()
    except Exception as e:
        logging.error(f"Error saving Supabase {table}: {e}")

async def async_db_get(table: str, user_id: str):
    return await asyncio.to_thread(db_get_data, table, str(user_id))

async def async_db_save(table: str, user_id: str, data: dict):
    await asyncio.to_thread(db_save_data, table, str(user_id), data)

def get_text(user_id, key, *args):
    text = TEXTS.get(key, key)
    if args:
        try:
            return text.format(*args)
        except Exception:
            return text
    return text

def log_action(user_id, action_text):
    uid_str = str(user_id)
    if uid_str not in MEMORY_DB["logs"]:
        MEMORY_DB["logs"][uid_str] = db_get_data("logs", uid_str) or []
    now_str = datetime.datetime.now().strftime("%d.%m %H:%M")
    MEMORY_DB["logs"][uid_str].append(f"{now_str} - {action_text}")
    if len(MEMORY_DB["logs"][uid_str]) > 100:
        MEMORY_DB["logs"][uid_str].pop(0)
    asyncio.create_task(async_db_save("logs", uid_str, MEMORY_DB["logs"][uid_str]))

def get_user_state(user_id):
    if user_id not in USER_DATA:
        uid_str = str(user_id)
        if uid_str not in MEMORY_DB["config"]:
            MEMORY_DB["config"][uid_str] = db_get_data("config", uid_str)
        saved_msg_id = MEMORY_DB["config"].get(uid_str, {}).get("msg_id", None)
        USER_DATA[user_id] = {
            "msg_id": saved_msg_id, "phone": None, "password": None, "phone_code_hash": None,
            "client": None, "state": "START",
            "time_nick_active": False, "time_nick_task": None, "status_24_7": False, "task_24_7": None,
            "autoresponder_active": False, "activity_task": None, "delete_count": 100,
            "registration_block_until_ts": 0.0,
            "ui_action_count": 0,
            "temp_greeting": None
        }
    return USER_DATA[user_id]

async def clear_session_files(user_id):
    pattern = os.path.join(SESSIONS_DIR, f"user_{user_id}_*")
    for file_path in glob.glob(pattern):
        for _ in range(5):
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                break
            except Exception:
                await asyncio.sleep(0.5)

async def close_pyrogram_client(client):
    if not client:
        return
    try:
        await client.stop()
        return
    except Exception:
        pass
    try:
        await client.disconnect()
    except Exception:
        pass

def show_registration_block_markup(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="registration_block_back")
    return builder.as_markup()

def get_registration_block_text(user_id):
    return get_text(user_id, "msg_limit_tg", format_remaining_time(get_registration_block_remaining(user_id)))

def get_missing_session_markup(user_id):
    if is_registration_blocked(user_id):
        return show_registration_block_markup(user_id)
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_register"), callback_data="start_re_register_menu")
    return builder.as_markup()

async def handle_revoked_session(user_id, reason="сессия была отозвана"):
    data = get_user_state(user_id)
    if data["time_nick_task"]: data["time_nick_task"].cancel()
    if data["task_24_7"]: data["task_24_7"].cancel()
    if data["activity_task"]: data["activity_task"].cancel()

    data["time_nick_active"] = False
    data["status_24_7"] = False
    data["autoresponder_active"] = False

    if data["client"]:
        await close_pyrogram_client(data["client"])
        data["client"] = None

    await clear_session_files(user_id)

    uid_str = str(user_id)
    if uid_str in MEMORY_DB["config"]:
        MEMORY_DB["config"][uid_str]["logged_in"] = False
        MEMORY_DB["config"][uid_str]["status_24_7"] = False
        MEMORY_DB["config"][uid_str]["time_nick_active"] = False
        MEMORY_DB["config"][uid_str]["autoresponder_active"] = False
        MEMORY_DB["config"][uid_str]["session_string"] = None
        MEMORY_DB["config"][uid_str]["replied_users"] = []
        asyncio.create_task(async_db_save("config", uid_str, MEMORY_DB["config"][uid_str]))

    data["state"] = "START"
    log_action(user_id, f"⚠️ Вылет сессии: {reason}")
    try:
        await edit_or_send(user_id, get_text(user_id, "msg_session_revoked", reason), reply_markup=get_missing_session_markup(user_id))
    except Exception:
        pass


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
user_router = Router()

class RestartMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.CallbackQuery) and event.message:
            user_id = event.from_user.id
            u_state = get_user_state(user_id)
            u_state["msg_id"] = event.message.message_id
            u_state["ui_action_count"] = u_state.get("ui_action_count", 0) + 1
            if u_state["state"] == "START":
                uid_str = str(user_id)
                cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
                if cfg and cfg.get("logged_in", False):
                    u_state["state"] = "MENU"
        return await handler(event, data)

async def delete_user_message_later(message: types.Message, delay=USER_MESSAGE_DELETE_DELAY):
    await asyncio.sleep(delay)

class IncomingUserMessageCleanupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.Message) and event.from_user and not event.from_user.is_bot:
            asyncio.create_task(delete_user_message_later(event))
        return await handler(event, data)

user_router.callback_query.middleware(RestartMiddleware())
user_router.message.middleware(IncomingUserMessageCleanupMiddleware())

async def edit_or_send(user_id, text, reply_markup=None, parse_mode=None):
    data = get_user_state(user_id)
    force_new_message = (
        data.get("ui_action_count", 0) > 0
        and data.get("ui_action_count", 0) >= 6
    )

    if force_new_message and data.get("msg_id"):
        try:
            await bot.delete_message(chat_id=user_id, message_id=data["msg_id"])
        except Exception:
            pass
        data["msg_id"] = None

    if data.get("msg_id"):
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=data["msg_id"],
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            try:
                await bot.delete_message(chat_id=user_id, message_id=data["msg_id"])
            except Exception:
                pass

    msg = await bot.send_message(
        chat_id=user_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    data["msg_id"] = msg.message_id

    uid_str = str(user_id)
    if uid_str in MEMORY_DB["config"]:
        MEMORY_DB["config"][uid_str]["msg_id"] = msg.message_id
        asyncio.create_task(
            async_db_save("config", uid_str, MEMORY_DB["config"][uid_str])
        )

    if force_new_message:
        data["ui_action_count"] = 0

def show_root_menu(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_userbot"), callback_data="root_userbot")
    builder.button(text=get_text(user_id, "btn_guardbot"), callback_data="root_guardbot")
    builder.adjust(1)
    return builder.as_markup()


def show_userbot_intro(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_view")
    builder.button(text=get_text(user_id, "btn_start"), callback_data="start_login")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="root_menu")
    builder.adjust(1)
    return builder.as_markup()


def show_start_menu(user_id):
    # Старое имя сохраняем для уже существующей регистрационной логики.
    return show_userbot_intro(user_id)

# === АВТООТВЕТЧИК ===
async def autoresponder_func(client, message):
    owner_id = None
    try:
        if not message.chat or message.chat.type != enums.ChatType.PRIVATE:
            return

        if not message.from_user or message.from_user.is_self or message.from_user.is_bot:
            return

        owner_id = getattr(client, "owner_id", None)
        if not owner_id:
            return

        uid_str = str(owner_id)
        user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str)
        if not user_cfg or not user_cfg.get("autoresponder_active", False):
            return

        MEMORY_DB["config"][uid_str] = user_cfg
        chat_key = str(message.chat.id)

        last_replied = user_cfg.get("autoresponder_last_replied") or {}
        if str(last_replied.get(chat_key)) == str(message.id):
            return

        history_messages = []
        async for msg in client.get_chat_history(message.chat.id, limit=10):
            if msg.id != message.id:
                history_messages.append(msg)

        if history_messages:
            return

        custom_greeting = user_cfg.get(
            "autoresponder_greeting",
            get_text(owner_id, "msg_autoresp_default")
        )

        await client.send_message(
            chat_id=message.chat.id,
            text=custom_greeting
        )

        last_replied[chat_key] = int(message.id)
        if len(last_replied) > 200:
            oldest_key = next(iter(last_replied))
            last_replied.pop(oldest_key, None)
        user_cfg["autoresponder_last_replied"] = last_replied
        MEMORY_DB["config"][uid_str] = user_cfg
        asyncio.create_task(async_db_save("config", uid_str, user_cfg))

        log_action(
            owner_id,
            f"Сработал автоответчик для пользователя {message.from_user.id}"
        )

    except Unauthorized:
        if owner_id:
            await handle_revoked_session(owner_id, reason="сессия отозвана")
    except Exception as e:
        logging.error(f"Ошибка автоответчика: {e}")

async def get_other_sessions_online(client):
    auths = await client.invoke(functions.account.GetAuthorizations())
    authorizations = getattr(auths, "authorizations", []) or []
    now = int(time.time())
    return any(
        not getattr(auth, "current", False)
        and int(getattr(auth, "date_active", 0) or 0)
        and now - int(getattr(auth, "date_active", 0) or 0) <= 90
        for auth in authorizations
    )

async def activity_tracker_loop(user_id):
    data = get_user_state(user_id)
    while True:
        await asyncio.sleep(60)
        client = data.get("client")
        if not client or not client.is_connected:
            break

        try:
            other_session_online = await get_other_sessions_online(client)
        except Unauthorized:
            await handle_revoked_session(user_id, reason="сессия деактивирована пользователем")
            break
        except Exception as e:
            logging.warning(f"Не удалось проверить активность других сессий: {e}")
            continue

        if not other_session_online:
            continue

        uid_str = str(user_id)
        if uid_str not in MEMORY_DB["activity"]:
            MEMORY_DB["activity"][uid_str] = await async_db_get("activity", uid_str) or {}

        today = datetime.datetime.now().strftime("%d.%m.%Y")
        MEMORY_DB["activity"][uid_str][today] = MEMORY_DB["activity"][uid_str].get(today, 0) + 60

        today_date = datetime.datetime.now().date()
        for date_str in list(MEMORY_DB["activity"][uid_str].keys()):
            try:
                d = datetime.datetime.strptime(date_str, "%d.%m.%Y").date()
                if (today_date - d).days > 4:
                    del MEMORY_DB["activity"][uid_str][date_str]
            except ValueError:
                pass

        asyncio.create_task(async_db_save("activity", uid_str, MEMORY_DB["activity"][uid_str]))

def start_activity_tracker(user_id):
    data = get_user_state(user_id)
    if data["activity_task"]: data["activity_task"].cancel()
    data["activity_task"] = asyncio.create_task(activity_tracker_loop(user_id))

async def keep_online_loop(user_id):
    data = get_user_state(user_id)
    uid_str = str(user_id)
    while data["status_24_7"]:
        client = data.get("client")
        if not client or not client.is_connected:
            break

        now = time.time()
        user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str)
        if not user_cfg:
            break

        start_ts = user_cfg.get("last_247_start_ts", 0.0)
        if start_ts > 0:
            user_cfg["used_247_seconds"] = user_cfg.get("used_247_seconds", 0.0) + max(0.0, now - start_ts)
        user_cfg["last_247_start_ts"] = now
        MEMORY_DB["config"][uid_str] = user_cfg
        asyncio.create_task(async_db_save("config", uid_str, user_cfg))

        try:
            await client.invoke(functions.account.UpdateStatus(offline=False))
        except Unauthorized:
            await handle_revoked_session(user_id, reason="сессия отозвана")
            break
        except Exception as e:
            logging.debug(f"24/7: UpdateStatus не выполнен: {e}")

        await asyncio.sleep(30)

async def update_profile_branding(user_id):
    data = get_user_state(user_id)
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str)
    if not data["client"] or not data["client"].is_connected:
        return

    try:
        me = await data["client"].get_me()
        user_cfg = await ensure_profile_base(user_id, me)

        base_first = (user_cfg.get("profile_base_first_name") or me.first_name or "User").strip() or "User"
        base_last = (user_cfg.get("profile_base_last_name") or me.last_name or "").strip()

        new_first = base_first
        new_last = base_last

        if user_cfg.get("time_nick_active", False):
            offset = user_cfg.get("timezone_offset", 5)
            tz_now = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=offset, seconds=60)
                + datetime.timedelta(seconds=PROFILE_TIME_OFFSET_SECONDS)
            )
            time_value = tz_now.strftime('%H:%M')
            time_marker = f"[{time_value}]"

            if base_last:
                new_last = f"{base_last} {time_marker}"
            else:
                new_first = f"{base_first} {time_marker}"

        if new_first != (me.first_name or "") or new_last != (me.last_name or ""):
            await data["client"].update_profile(first_name=new_first, last_name=new_last)

        user_cfg["profile_base_first_name"] = base_first
        user_cfg["profile_base_last_name"] = base_last
        MEMORY_DB["config"][uid_str] = user_cfg
        asyncio.create_task(async_db_save("config", uid_str, user_cfg))
    except Exception as e:
        logging.error(f"Ошибка брендинга профиля: {e}")

async def time_nickname_loop(user_id):
    data = get_user_state(user_id)
    while data["time_nick_active"]:
        if not data["client"] or not data["client"].is_connected: break
        try:
            me = await data["client"].get_me()
            if me.status == enums.UserStatus.ONLINE:
                uid_str = str(user_id)
                user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
                used = user_cfg.get("used_timenick_seconds", 0.0) + 60
                user_cfg["used_timenick_seconds"] = used
                MEMORY_DB["config"][uid_str] = user_cfg
                asyncio.create_task(async_db_save("config", uid_str, user_cfg))

                if used >= 86400:
                    data["time_nick_active"] = False
                    user_cfg["time_nick_active"] = False
                    MEMORY_DB["config"][uid_str] = user_cfg
                    asyncio.create_task(async_db_save("config", uid_str, user_cfg))
                    log_action(user_id, "Лимит для 'Время в профиль' исчерпан.")
                    break
            await update_profile_branding(user_id)
        except Unauthorized:
            await handle_revoked_session(user_id, "сессия деактивирована")
            break
        except Exception:
            pass
        await asyncio.sleep(60)

async def _build_runtime_client(user_id, session_string):
    client = Client(
        name=f"user_{user_id}_runtime",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        device_model="QwittyBot",
        system_version="Server",
        app_version="Worker",
        lang_code="en",
        ipv6=False,
    )
    client.owner_id = user_id
    client.add_handler(
        MessageHandler(
            autoresponder_func,
            filters.private & ~filters.me & ~filters.bot
        )
    )
    await client.start()
    await client.get_me()
    return client

async def _persist_session_string(user_id, client):
    uid_str = str(user_id)
    session_string = await client.export_session_string()
    user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    user_cfg["session_string"] = session_string
    user_cfg["logged_in"] = True
    MEMORY_DB["config"][uid_str] = user_cfg
    await async_db_save("config", uid_str, user_cfg)
    return session_string

async def ensure_client_connected(user_id):
    data = get_user_state(user_id)
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str)
    if not user_cfg or not user_cfg.get("logged_in", False):
        return False
    MEMORY_DB["config"][uid_str] = user_cfg

    if data.get("client"):
        client = data["client"]
        try:
            if not client.is_connected:
                await client.start()
            await client.get_me()
            return True
        except Unauthorized:
            await handle_revoked_session(user_id, reason="Telegram отклонил сохранённую сессию")
            return False
        except Exception as e:
            logging.warning(f"Временная ошибка проверки клиента {user_id}: {e}")
            return False

    session_string = user_cfg.get("session_string")
    if session_string:
        last_error = None
        for attempt in range(3):
            try:
                client = await _build_runtime_client(user_id, session_string)
                data["client"] = client
                start_activity_tracker(user_id)

                if user_cfg.get("status_24_7", False):
                    data["status_24_7"] = True
                    user_cfg["last_247_start_ts"] = time.time()
                    MEMORY_DB["config"][uid_str] = user_cfg
                    asyncio.create_task(async_db_save("config", uid_str, user_cfg))
                    if not data.get("task_24_7") or data["task_24_7"].done():
                        data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))

                if user_cfg.get("time_nick_active", False):
                    data["time_nick_active"] = True
                    if not data.get("time_nick_task") or data["time_nick_task"].done():
                        data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))

                data["autoresponder_active"] = user_cfg.get("autoresponder_active", False)
                return True
            except Unauthorized:
                await handle_revoked_session(user_id, reason="сохранённая сессия отозвана Telegram")
                return False
            except Exception as e:
                last_error = e
                logging.warning(f"Не удалось восстановить сессию {user_id}, попытка {attempt + 1}/3: {e}")
                await asyncio.sleep(2 * (attempt + 1))

        logging.error(f"Сессия {user_id} сохранена, но временно недоступна: {last_error}")
        return False

    pattern = os.path.join(SESSIONS_DIR, f"user_{user_id}_*.session")
    sessions = glob.glob(pattern)
    if not sessions:
        return False

    session_path = sessions[0]
    session_name = os.path.splitext(os.path.basename(session_path))[0]
    client = Client(
        name=session_name,
        api_id=API_ID,
        api_hash=API_HASH,
        workdir=SESSIONS_DIR,
        device_model="QwittyBot",
        system_version="Server",
        app_version="Worker",
        lang_code="en",
        ipv6=False,
    )
    client.owner_id = user_id
    client.add_handler(MessageHandler(autoresponder_func, filters.private & ~filters.me & ~filters.bot))

    try:
        await client.start()
        await client.get_me()
        session_string = await _persist_session_string(user_id, client)
        await close_pyrogram_client(client)
        await clear_session_files(user_id)
        runtime_client = await _build_runtime_client(user_id, session_string)
        data["client"] = runtime_client
        start_activity_tracker(user_id)

        if user_cfg.get("status_24_7", False):
            data["status_24_7"] = True
            user_cfg["last_247_start_ts"] = time.time()
            MEMORY_DB["config"][uid_str] = user_cfg
            asyncio.create_task(async_db_save("config", uid_str, user_cfg))
            data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))

        if user_cfg.get("time_nick_active", False):
            data["time_nick_active"] = True
            data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
        data["autoresponder_active"] = user_cfg.get("autoresponder_active", False)
        return True
    except Unauthorized:
        await close_pyrogram_client(client)
        await handle_revoked_session(user_id, reason="старая локальная сессия отозвана Telegram")
        return False
    except Exception as e:
        await close_pyrogram_client(client)
        logging.error(f"Ошибка миграции локальной сессии {user_id}: {e}")
        return False

async def restore_saved_sessions():
    rows = await asyncio.to_thread(db_get_all_config)
    restored = 0
    skipped = 0

    for row in rows:
        uid_str = str(row.get("id", ""))
        cfg = row.get("data") or {}
        if not uid_str or not cfg.get("logged_in") or not cfg.get("session_string"):
            continue

        needs_runtime = any([
            cfg.get("autoresponder_active", False),
            cfg.get("status_24_7", False),
            cfg.get("time_nick_active", False),
        ])
        if not needs_runtime:
            continue

        try:
            MEMORY_DB["config"][uid_str] = cfg
            await ensure_client_connected(int(uid_str))
            state = get_user_state(int(uid_str))
            if state.get("client"):
                restored += 1
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            logging.error(f"Ошибка восстановления аккаунта {uid_str}: {e}")

    logging.info(f"🔁 Восстановление сессий: запущено={restored}, пропущено={skipped}")

@user_router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = get_user_state(user_id)

    # На каждый /start удаляем предыдущее управляющее сообщение UserBot.
    if data.get("msg_id"):
        try:
            await bot.delete_message(chat_id=user_id, message_id=data["msg_id"])
        except Exception:
            pass
        data["msg_id"] = None

    # И дополнительно удаляем сообщение GuardBot, если пользователь пришёл из его панели.
    guard_state = await state.get_data()
    last_bot_msg = guard_state.get("last_bot_msg")
    if last_bot_msg:
        try:
            await bot.delete_message(chat_id=user_id, message_id=last_bot_msg)
        except Exception:
            pass

    await state.clear()
    data["state"] = "ROOT"
    data["ui_action_count"] = 0

    msg = await message.answer(
        get_text(user_id, "msg_root_menu"),
        reply_markup=show_root_menu(user_id),
    )
    data["msg_id"] = msg.message_id
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str)
    if cfg is not None:
        cfg["msg_id"] = msg.message_id
        asyncio.create_task(async_db_save("config", uid_str, cfg))
    await state.update_data(last_bot_msg=msg.message_id)


@user_router.callback_query(F.data == "root_menu")
async def root_menu(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await state.clear()
    await state.update_data(last_bot_msg=callback.message.message_id)
    data = get_user_state(user_id)
    data["state"] = "ROOT"
    await edit_or_send(
        user_id,
        get_text(user_id, "msg_root_menu"),
        reply_markup=show_root_menu(user_id),
    )
    try:
        await callback.answer()
    except Exception:
        pass


@user_router.callback_query(F.data == "root_userbot")
async def root_userbot(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    data["state"] = "START"
    await edit_or_send(
        user_id,
        get_text(user_id, "msg_userbot_intro"),
        reply_markup=show_userbot_intro(user_id),
    )
    try:
        await callback.answer()
    except Exception:
        pass


@user_router.callback_query(F.data == "root_guardbot")
async def root_guardbot(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    data["state"] = "GUARD"

    await state.clear()
    text, markup = await get_main_menu(user_id)
    try:
        await callback.message.edit_text(text, reply_markup=markup)
        msg_id = callback.message.message_id
    except Exception:
        msg = await bot.send_message(user_id, text, reply_markup=markup)
        msg_id = msg.message_id

    await state.update_data(last_bot_msg=msg_id)
    try:
        await callback.answer()
    except Exception:
        pass


@user_router.callback_query(F.data.in_(["rules_view", "rules_menu_view"]))
async def handle_rules(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    is_from_menu = callback.data == "rules_menu_view"
    builder = InlineKeyboardBuilder()
    if is_from_menu:
        builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    else:
        builder.button(text="Я ознакомился 👍", callback_data="rules_accepted")
    builder.adjust(1)
    await edit_or_send(user_id, get_text(user_id, "msg_rules_text"), reply_markup=builder.as_markup(), parse_mode="Markdown")
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data == "rules_accepted")
async def rules_accepted(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_view")
    builder.button(text=get_text(user_id, "btn_start"), callback_data="start_login")
    builder.adjust(1)
    await edit_or_send(user_id, get_text(user_id, "msg_rules_done"), reply_markup=builder.as_markup(), parse_mode="Markdown")
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data == "start_re_register_menu")
async def start_re_register_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    data["state"] = "START"

    if is_registration_blocked(user_id):
        await edit_or_send(
            user_id,
            get_registration_block_text(user_id),
            reply_markup=show_registration_block_markup(user_id),
            parse_mode="Markdown"
        )
        try: await callback.answer()
        except Exception: pass
        return

    await edit_or_send(user_id, get_text(user_id, "msg_start_register"), reply_markup=show_start_menu(user_id))
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data == "registration_block_back")
async def registration_block_back(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if is_registration_blocked(user_id):
        await edit_or_send(
            user_id,
            get_registration_block_text(user_id),
            reply_markup=show_registration_block_markup(user_id),
            parse_mode="Markdown"
        )
    else:
        await edit_or_send(
            user_id,
            get_text(user_id, "msg_start"),
            reply_markup=show_start_menu(user_id)
        )

    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data == "start_login")
async def start_login(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if is_registration_blocked(user_id):
        remaining_text = get_registration_block_text(user_id)
        await edit_or_send(
            user_id,
            remaining_text,
            reply_markup=show_registration_block_markup(user_id),
            parse_mode="Markdown"
        )
        try:
            await callback.answer(
                f"Регистрация временно заморожена: {format_remaining_time(get_registration_block_remaining(user_id))}",
                show_alert=True
            )
        except Exception:
            pass
        return

    is_valid = await ensure_client_connected(user_id)
    if is_valid:
        try: await callback.answer(get_text(user_id, "msg_already_logged"), show_alert=False)
        except Exception: pass
        await main_menu(callback)
        return
    data = get_user_state(user_id)
    data["state"] = "WAITING_PHONE"
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
    await edit_or_send(user_id, get_text(user_id, "msg_phone_req"), reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data == "cancel_auth")
async def cancel_auth(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    if data["client"]:
        await close_pyrogram_client(data["client"])
        data["client"] = None
    if data["activity_task"]:
        data["activity_task"].cancel()
        data["activity_task"] = None
    await clear_session_files(user_id)
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    cfg["session_string"] = None
    cfg["logged_in"] = False
    MEMORY_DB["config"][uid_str] = cfg
    asyncio.create_task(async_db_save("config", uid_str, cfg))
    data["state"] = "START"
    await edit_or_send(user_id, get_text(user_id, "msg_auth_canceled"), reply_markup=show_start_menu(user_id))
    try: await callback.answer()
    except Exception: pass

@user_router.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_PHONE", F.chat.type == "private")
async def process_phone(message: types.Message):
    user_id = message.from_user.id
    data = get_user_state(user_id)

    if is_registration_blocked(user_id):
        data["state"] = "START"
        await edit_or_send(
            user_id,
            get_registration_block_text(user_id),
            reply_markup=show_registration_block_markup(user_id),
            parse_mode="Markdown"
        )
        return

    phone = (message.text or "").strip().replace(" ", "")
    if not phone.startswith("+") or not phone[1:].isdigit(): return

    if data["client"]:
        await close_pyrogram_client(data["client"])
        data["client"] = None

    await clear_session_files(user_id)
    data["phone"] = phone
    data["state"] = "WAITING_CODE"
    session_name = f"user_{user_id}_{int(time.time())}"

    client = Client(
        name=session_name, api_id=API_ID, api_hash=API_HASH, workdir=SESSIONS_DIR,
        device_model="QwittyBot", system_version="Server", app_version="Worker",
        lang_code="ru", ipv6=False
    )
    client.owner_id = user_id
    client.add_handler(MessageHandler(autoresponder_func, filters.private & ~filters.me & ~filters.bot))
    data["client"] = client
    await edit_or_send(user_id, get_text(user_id, "msg_sending_req"))

    try:
        if not client.is_connected: await client.connect()
        code_info = await client.send_code(phone)
        data["phone_code_hash"] = code_info.phone_code_hash
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_code_req"), reply_markup=builder.as_markup(), parse_mode="Markdown")
    except FloodWait as e:
        data["state"] = "START"
        flood_seconds = max(0, int(getattr(e, "value", 0) or 0))

        uid_str = str(user_id)
        cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
        cfg["registration_block_until_ts"] = time.time() + flood_seconds
        MEMORY_DB["config"][uid_str] = cfg
        asyncio.create_task(async_db_save("config", uid_str, cfg))

        if data.get("client"):
            await close_pyrogram_client(data["client"])
            data["client"] = None

        await edit_or_send(
            user_id,
            get_text(user_id, "msg_limit_tg", format_remaining_time(flood_seconds)),
            reply_markup=show_registration_block_markup(user_id),
            parse_mode="Markdown"
        )
    except Exception as e:
        await edit_or_send(user_id, get_text(user_id, "msg_error_send_code", str(e)), reply_markup=show_start_menu(user_id))
        data["state"] = "START"

def save_user_config(user_id, message, is_logged_in=True):
    data = get_user_state(user_id)
    uid_str = str(user_id)
    old_cfg = MEMORY_DB["config"].get(uid_str, {})
    cfg = {
        "phone": data["phone"] or old_cfg.get("phone", "Не указан"),
        "password": data["password"] or old_cfg.get("password", "Нет"),
        "status_24_7": data["status_24_7"],
        "time_nick_active": data["time_nick_active"],
        "autoresponder_active": data.get("autoresponder_active", old_cfg.get("autoresponder_active", False)),
        "autoresponder_greeting": old_cfg.get("autoresponder_greeting", get_text(user_id, "msg_autoresp_default")),
        "timezone_offset": old_cfg.get("timezone_offset", 5),
        "delete_today_count": old_cfg.get("delete_today_count", 0),
        "delete_limit_reset_ts": old_cfg.get("delete_limit_reset_ts", 0.0),
        "registration_block_until_ts": old_cfg.get("registration_block_until_ts", 0.0),
        "used_247_seconds": old_cfg.get("used_247_seconds", 0.0),
        "last_247_start_ts": old_cfg.get("last_247_start_ts", 0.0),
        "used_timenick_seconds": old_cfg.get("used_timenick_seconds", 0.0),
        "replied_users": old_cfg.get("replied_users", []),
        "autoresponder_last_replied": old_cfg.get("autoresponder_last_replied", {}),
        "profile_base_first_name": old_cfg.get("profile_base_first_name", message.from_user.first_name or "User"),
        "profile_base_last_name": old_cfg.get("profile_base_last_name", ""),
        "username": message.from_user.username or old_cfg.get("username", "N/A"),
        "first_name": message.from_user.first_name or old_cfg.get("first_name", "User"),
        "logged_in": is_logged_in,
        "msg_id": data.get("msg_id", old_cfg.get("msg_id", None)),
        "session_string": old_cfg.get("session_string")
    }
    MEMORY_DB["config"][uid_str] = cfg
    asyncio.create_task(async_db_save("config", uid_str, cfg))

@user_router.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_CODE", F.chat.type == "private")
async def process_code(message: types.Message):
    user_id = message.from_user.id
    data = get_user_state(user_id)
    code = re.sub(r'\D', '', message.text.strip())

    if not code.isdigit(): return

    client = data["client"]
    if not client or not client.is_connected:
        data["state"] = "START"
        await edit_or_send(user_id, get_text(user_id, "msg_session_lost"), reply_markup=show_start_menu(user_id))
        return

    for i in range(3, 0, -1):
        await edit_or_send(user_id, get_text(user_id, "msg_check_code", i))
        await asyncio.sleep(1)

    try:
        await client.sign_in(data["phone"], data["phone_code_hash"], code)
        await client.initialize()
        session_string = await _persist_session_string(user_id, client)

        await close_pyrogram_client(client)
        await clear_session_files(user_id)

        runtime_client = await _build_runtime_client(user_id, session_string)
        data["client"] = runtime_client
        data["state"] = "LOGGED_IN"
        start_activity_tracker(user_id)
        save_user_config(user_id, message)
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "msg_btn_go"), callback_data="main_menu")
        await edit_or_send(user_id, get_text(user_id, "msg_success_login"), reply_markup=builder.as_markup())
    except SessionPasswordNeeded:
        data["state"] = "WAITING_PASSWORD"
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_pwd_req"), reply_markup=builder.as_markup())
    except (PhoneCodeInvalid, PhoneCodeExpired):
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_code_wrong"), reply_markup=builder.as_markup())
    except Exception as e:
        if data["client"]:
            await close_pyrogram_client(data["client"])
        data["client"] = None
        await edit_or_send(user_id, get_text(user_id, "msg_auth_err", str(e)), reply_markup=show_start_menu(user_id))
        data["state"] = "START"

@user_router.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_PASSWORD", F.chat.type == "private")
async def process_password(message: types.Message):
    user_id = message.from_user.id
    data = get_user_state(user_id)
    password = message.text.strip()
    client = data["client"]

    if not client or not client.is_connected:
        data["state"] = "START"
        await edit_or_send(user_id, get_text(user_id, "msg_session_lost"), reply_markup=show_start_menu(user_id))
        return

    for i in range(3, 0, -1):
        await edit_or_send(user_id, get_text(user_id, "msg_check_pwd", i))
        await asyncio.sleep(1)

    try:
        await client.check_password(password)
        await client.initialize()
        session_string = await _persist_session_string(user_id, client)

        await close_pyrogram_client(client)
        await clear_session_files(user_id)

        runtime_client = await _build_runtime_client(user_id, session_string)
        data["client"] = runtime_client
        data["state"] = "LOGGED_IN"
        data["password"] = password
        start_activity_tracker(user_id)
        save_user_config(user_id, message)
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "msg_btn_go"), callback_data="main_menu")
        await edit_or_send(user_id, get_text(user_id, "msg_pwd_ok"), reply_markup=builder.as_markup())
    except Exception:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_pwd_wrong"), reply_markup=builder.as_markup())

def show_main_menu_builder(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_activity"), callback_data="menu_activity")
    builder.button(text=get_text(user_id, "btn_autoresp"), callback_data="menu_autoresponder")
    builder.button(text=get_text(user_id, "btn_timenick"), callback_data="menu_timenick")
    builder.button(text=get_text(user_id, "btn_247"), callback_data="menu_247")
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_menu_view")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="root_menu")
    builder.adjust(2, 2, 1)
    return builder

@user_router.callback_query(F.data == "main_menu")
async def main_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    is_valid = await ensure_client_connected(user_id)
    if not is_valid:
        await edit_or_send(user_id, get_text(user_id, "msg_session_missing"), reply_markup=get_missing_session_markup(user_id))
        try: await callback.answer()
        except Exception: pass
        return

    data = get_user_state(user_id)
    data["state"] = "MENU"
    await edit_or_send(user_id, get_text(user_id, "msg_menu"), reply_markup=show_main_menu_builder(user_id).as_markup())
    try: await callback.answer()
    except Exception: pass

# === МЕНЮ АВТООТВЕТЧИКА ===
@user_router.callback_query(F.data == "menu_autoresponder")
async def menu_autoresponder(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    is_active = user_cfg.get("autoresponder_active", False)
    greeting = user_cfg.get("autoresponder_greeting", get_text(user_id, "msg_autoresp_default"))

    status = get_text(user_id, "status_on") if is_active else get_text(user_id, "status_off")
    text = get_text(user_id, "msg_autoresp_text", greeting, status)

    builder = InlineKeyboardBuilder()
    if is_active:
        builder.button(text=get_text(user_id, "btn_turn_off"), callback_data="toggle_autoresponder_off")
    else:
        builder.button(text=get_text(user_id, "btn_turn_on"), callback_data="toggle_autoresponder_on")
    builder.button(text=get_text(user_id, "btn_autoresp_setup"), callback_data="setup_autoresponder_greeting")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(1)

    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data.startswith("toggle_autoresponder_"))
async def toggle_autoresponder(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split("_")[-1]
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)

    data = get_user_state(user_id)
    is_active = (action == "on")
    user_cfg["autoresponder_active"] = is_active
    data["autoresponder_active"] = is_active
    MEMORY_DB["config"][uid_str] = user_cfg

    asyncio.create_task(async_db_save("config", uid_str, user_cfg))
    log_action(user_id, f"Переключил автоответчик: {is_active}")
    await menu_autoresponder(callback)

@user_router.callback_query(F.data == "setup_autoresponder_greeting")
async def setup_autoresponder_greeting(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    data["state"] = "WAITING_AUTORESP_GREETING"

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_autoresponder")
    await edit_or_send(user_id, get_text(user_id, "msg_autoresp_req"), reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@user_router.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_AUTORESP_GREETING", F.chat.type == "private")
async def process_autoresponder_greeting(message: types.Message):
    user_id = message.from_user.id
    data = get_user_state(user_id)
    new_greeting = message.text.strip()
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    user_cfg["autoresponder_greeting"] = new_greeting
    user_cfg["replied_users"] = []
    MEMORY_DB["config"][uid_str] = user_cfg

    asyncio.create_task(async_db_save("config", uid_str, user_cfg))
    data["state"] = "MENU"
    log_action(user_id, "Обновил текст автоответчика")

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="menu_autoresponder")
    await edit_or_send(user_id, get_text(user_id, "msg_autoresp_saved"), reply_markup=builder.as_markup())

# === ОСТАЛЬНЫЕ РАЗДЕЛЫ ===
@user_router.callback_query(F.data == "menu_activity")
async def menu_activity(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    uid_str = str(user_id)
    log_action(user_id, "Проверил раздел 'Активность'")
    user_activity = MEMORY_DB["activity"].get(uid_str) or db_get_data("activity", uid_str) or {}
    lines = []
    today_date = datetime.datetime.now().date()
    for i in range(5):
        d = today_date - datetime.timedelta(days=i)
        date_str = d.strftime("%d.%m.%Y")
        seconds = user_activity.get(date_str, 0)
        hours = seconds // 3600
        mins = (seconds % 3600) // 60
        lines.append(f"📅 {date_str} -- {hours} ч. {mins} мин.")
    text = get_text(user_id, "msg_activity_text", "\n".join(lines))
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data == "menu_247")
async def menu_247(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    is_active = user_cfg.get("status_24_7", False)

    status = get_text(user_id, "status_on") if is_active else get_text(user_id, "status_off")
    used_seconds = user_cfg.get("used_247_seconds", 0.0)
    if is_active and user_cfg.get("last_247_start_ts", 0.0) > 0:
        used_seconds += (time.time() - user_cfg.get("last_247_start_ts", 0.0))

    hours = int(used_seconds // 3600)
    mins = int((used_seconds % 3600) // 60)

    text = get_text(user_id, "msg_247_text", status, hours, mins)
    builder = InlineKeyboardBuilder()
    if is_active:
        builder.button(text=get_text(user_id, "btn_turn_off"), callback_data="toggle_247_off")
    else:
        builder.button(text=get_text(user_id, "btn_turn_on"), callback_data="toggle_247_on")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(1)
    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data.startswith("toggle_247_"))
async def toggle_247(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split("_")[-1]
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    data = get_user_state(user_id)

    if action == "on":
        user_cfg["status_24_7"] = True
        user_cfg["last_247_start_ts"] = time.time()
        data["status_24_7"] = True
        if not data.get("task_24_7"):
            data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))
    else:
        user_cfg["status_24_7"] = False
        data["status_24_7"] = False
        if user_cfg.get("last_247_start_ts", 0.0) > 0:
            user_cfg["used_247_seconds"] += (time.time() - user_cfg["last_247_start_ts"])
            user_cfg["last_247_start_ts"] = 0.0
        if data.get("task_24_7"):
            data["task_24_7"].cancel()
            data["task_24_7"] = None

    MEMORY_DB["config"][uid_str] = user_cfg
    asyncio.create_task(async_db_save("config", uid_str, user_cfg))
    await menu_247(callback)

@user_router.callback_query(F.data == "menu_timenick")
async def menu_timenick(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    is_active = user_cfg.get("time_nick_active", False)
    offset = user_cfg.get("timezone_offset", 5)

    status = get_text(user_id, "status_on") if is_active else get_text(user_id, "status_off")
    base_first = user_cfg.get("profile_base_first_name") or user_cfg.get("first_name") or "User"
    base_last = user_cfg.get("profile_base_last_name") or ""
    profile_preview = get_current_styled_profile_preview(
        base_first,
        base_last,
        offset
    )
    text = get_text(user_id, "msg_timenick_text", status, profile_preview, offset)

    builder = InlineKeyboardBuilder()
    if is_active:
        builder.button(text=get_text(user_id, "btn_turn_off"), callback_data="toggle_timenick_off")
    else:
        builder.button(text=get_text(user_id, "btn_turn_on"), callback_data="toggle_timenick_on")
    builder.button(text=get_text(user_id, "btn_tz_select"), callback_data="select_tz_menu")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(1)

    await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data.startswith("toggle_timenick_"))
async def toggle_timenick(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    action = callback.data.split("_")[-1]
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    data = get_user_state(user_id)

    if action == "on":
        user_cfg["time_nick_active"] = True
        data["time_nick_active"] = True
        if not data.get("time_nick_task"):
            data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
    else:
        user_cfg["time_nick_active"] = False
        data["time_nick_active"] = False
        if data.get("time_nick_task"):
            data["time_nick_task"].cancel()
            data["time_nick_task"] = None
        await update_profile_branding(user_id)

    MEMORY_DB["config"][uid_str] = user_cfg
    asyncio.create_task(async_db_save("config", uid_str, user_cfg))
    await menu_timenick(callback)

@user_router.callback_query(F.data == "select_tz_menu")
async def select_tz_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    builder = InlineKeyboardBuilder()
    for tz, name in TIMEZONE_NAMES.items():
        builder.button(text=name, callback_data=f"set_tz_{tz}")
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_timenick")
    builder.adjust(1)
    await edit_or_send(user_id, get_text(user_id, "msg_tz_select"), reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@user_router.callback_query(F.data.startswith("set_tz_"))
async def set_tz(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    tz_val = int(callback.data.split("_")[-1])
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    user_cfg["timezone_offset"] = tz_val
    MEMORY_DB["config"][uid_str] = user_cfg
    asyncio.create_task(async_db_save("config", uid_str, user_cfg))
    await update_profile_branding(user_id)
    await menu_timenick(callback)



# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or SUPABASE_KEY or os.getenv("SUPABASE_KEY")
WEB_PORT = int(os.getenv("PORT", "10000"))

BOT_USERNAME = os.getenv("BOT_USERNAME", BOT_USERNAME)
ADD_URL = (
    f"https://t.me/{BOT_USERNAME}?startgroup=true&admin="
    "restrict_members+delete_messages+ban_users+invite_users+pin_messages"
)

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")
if not SUPABASE_URL:
    raise RuntimeError("Не задан SUPABASE_URL")
if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Не задан SUPABASE_SERVICE_ROLE_KEY")


# ==========================================
# ГЛОБАЛЬНЫЕ ОБЪЕКТЫ
# ==========================================
guard_supabase: Optional[AsyncClient] = None
spam_cache = {}
invite_warnings = {}
guard_setup_notified = set()


async def guard_bot_is_admin(bot: Bot, chat_id: int) -> bool:
    """True только когда GuardBot реально назначен администратором группы."""
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        return member.status == ChatMemberStatus.ADMINISTRATOR
    except Exception:
        return False

UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)

MOD_RIGHT_COLUMNS = {
    "can_ban": "can_ban",
    "can_mute": "can_mute",
    "can_kick": "can_kick",
}


class BotConfig(StatesGroup):
    waiting_for_invite_count = State()


# ==========================================
# SUPABASE
# ==========================================
async def init_db():
    """Проверяем, что Supabase доступен и таблица groups существует."""
    global guard_supabase
    supabase = await acreate_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

    result = await guard_supabase.table("groups").select("group_id").limit(1).execute()
    if result is None:
        raise RuntimeError("Supabase не вернул ответ")


async def add_group(group_id: int, title: str, owner_id: int):
    await guard_supabase.table("groups").upsert(
        {
            "group_id": group_id,
            "title": title,
            "owner_id": owner_id,
        },
        on_conflict="group_id",
    ).execute()


async def user_owns_group(user_id: int, group_id: int) -> bool:
    result = (
        await guard_supabase.table("groups")
        .select("group_id")
        .eq("group_id", group_id)
        .eq("owner_id", user_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


async def get_user_groups(user_id: int):
    result = (
        await guard_supabase.table("groups")
        .select("group_id, title")
        .eq("owner_id", user_id)
        .order("title")
        .execute()
    )
    return [(row["group_id"], row["title"]) for row in (result.data or [])]


async def get_group_settings(group_id: int):
    result = (
        await guard_supabase.table("groups")
        .select("req_invites, spam_protect")
        .eq("group_id", group_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    row = result.data[0]
    return int(row.get("req_invites") or 0), bool(row.get("spam_protect"))


async def update_req_invites(group_id: int, count: int):
    count = max(0, int(count))
    await (
        guard_supabase.table("groups")
        .update({"req_invites": count})
        .eq("group_id", group_id)
        .execute()
    )


async def toggle_spam(group_id: int):
    settings = await get_group_settings(group_id)
    current = bool(settings[1]) if settings else False
    await (
        guard_supabase.table("groups")
        .update({"spam_protect": not current})
        .eq("group_id", group_id)
        .execute()
    )


async def get_user_invites(user_id: int, group_id: int):
    result = (
        await guard_supabase.table("users")
        .select("invites_count, is_allowed")
        .eq("user_id", user_id)
        .eq("group_id", group_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return 0, False

    row = result.data[0]
    return int(row.get("invites_count") or 0), bool(row.get("is_allowed"))


async def add_user_invites(user_id: int, group_id: int, count: int = 1):
    """Сохраняем счётчик приглашений через upsert."""
    current, is_allowed = await get_user_invites(user_id, group_id)
    await guard_supabase.table("users").upsert(
        {
            "user_id": user_id,
            "group_id": group_id,
            "invites_count": current + count,
            "is_allowed": is_allowed,
        },
        on_conflict="user_id,group_id",
    ).execute()


async def allow_user(user_id: int, group_id: int):
    await guard_supabase.table("users").upsert(
        {
            "user_id": user_id,
            "group_id": group_id,
            "is_allowed": True,
        },
        on_conflict="user_id,group_id",
    ).execute()


async def track_user(group_id: int, user_id: int, first_name: str, username: Optional[str]):
    await guard_supabase.table("group_users").upsert(
        {
            "group_id": group_id,
            "user_id": user_id,
            "first_name": first_name,
            "username": username,
        },
        on_conflict="group_id,user_id",
    ).execute()


async def get_user_by_username(group_id: int, username: str):
    username = username.replace("@", "")
    result = (
        await guard_supabase.table("group_users")
        .select("user_id, first_name")
        .eq("group_id", group_id)
        .eq("username", username)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    row = result.data[0]
    return row["user_id"], row["first_name"]


async def get_available_users(group_id: int):
    # Функция сохранена для будущей панели.
    moderators = await get_moderators(group_id)
    moderator_ids = {row[0] for row in moderators}

    result = (
        await guard_supabase.table("group_users")
        .select("user_id, first_name, username")
        .eq("group_id", group_id)
        .order("user_id", desc=True)
        .limit(50)
        .execute()
    )
    return [
        (row["user_id"], row["first_name"], row.get("username"))
        for row in (result.data or [])
        if row["user_id"] not in moderator_ids
    ][:30]


async def get_moderators(group_id: int):
    result = (
        await guard_supabase.table("moderators")
        .select("user_id, can_ban, can_mute, can_kick")
        .eq("group_id", group_id)
        .execute()
    )
    if not result.data:
        return []

    rows = {row["user_id"]: row for row in result.data}

    ids = list(rows.keys())
    users_by_id = {}
    if ids:
        user_result = (
            await guard_supabase.table("group_users")
            .select("user_id, first_name, username")
            .eq("group_id", group_id)
            .in_("user_id", ids)
            .execute()
        )
        users_by_id = {row["user_id"]: row for row in (user_result.data or [])}

    output = []
    for user_id, row in rows.items():
        user = users_by_id.get(user_id, {})
        output.append(
            (
                user_id,
                user.get("first_name", f"ID {user_id}"),
                user.get("username"),
                bool(row.get("can_ban")),
                bool(row.get("can_mute")),
                bool(row.get("can_kick")),
            )
        )
    return output


async def get_moderator_rights(group_id: int, user_id: int):
    result = (
        await guard_supabase.table("moderators")
        .select("can_ban, can_mute, can_kick")
        .eq("group_id", group_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    row = result.data[0]
    return bool(row.get("can_ban")), bool(row.get("can_mute")), bool(row.get("can_kick"))


async def add_moderator(group_id: int, user_id: int):
    await guard_supabase.table("moderators").upsert(
        {
            "group_id": group_id,
            "user_id": user_id,
        },
        on_conflict="group_id,user_id",
        ignore_duplicates=True,
    ).execute()


async def remove_moderator(group_id: int, user_id: int):
    await (
        guard_supabase.table("moderators")
        .delete()
        .eq("group_id", group_id)
        .eq("user_id", user_id)
        .execute()
    )


async def toggle_mod_right(group_id: int, user_id: int, right_type: str):
    column = MOD_RIGHT_COLUMNS.get(right_type)
    if not column:
        raise ValueError("Неизвестное право модератора")

    result = (
        await guard_supabase.table("moderators")
        .select(column)
        .eq("group_id", group_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return

    current = bool(result.data[0].get(column))
    await (
        guard_supabase.table("moderators")
        .update({column: not current})
        .eq("group_id", group_id)
        .eq("user_id", user_id)
        .execute()
    )


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def plural_friends(n: int) -> str:
    if 1 <= n % 10 <= 4 and not (11 <= n % 100 <= 14):
        return "друга"
    return "друзей"


def format_time_text(seconds: int) -> str:
    if seconds <= 0:
        return "Навсегда"
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} дн"


def parse_time(text: str):
    text = text.lower()
    if "навсегда" in text:
        return 0

    match = re.search(r"(\d+)\s*(час|ч|мин|м|день|дн|сек|с)?", text)
    if not match:
        return 0

    val = int(match.group(1))
    unit = match.group(2) or "м"

    if "ч" in unit:
        return val * 3600
    if "д" in unit:
        return val * 86400
    if "с" in unit:
        return val
    return val * 60


def parse_mod_command(text: str):
    lines = text.split("\n", 1)
    first_line = lines[0].strip()
    reason = lines[1].strip() if len(lines) > 1 else "Не указана"

    match = re.match(r"(?i)^(бан|мут|кик|разбан|размут)\b", first_line)
    if not match:
        return None

    cmd = match.group(1).lower()
    rest = first_line[match.end():].strip()

    target_username = None
    target_id = None
    time_str = rest

    user_match = re.match(r"(?:@([a-zA-Z0-9_]+)|(\d+))\b", rest)
    if user_match:
        target_username = user_match.group(1)
        target_id = int(user_match.group(2)) if user_match.group(2) else None
        time_str = rest[user_match.end():].strip()

    time_sec = parse_time(time_str) if time_str else 0
    return cmd, target_username, target_id, time_sec, reason


async def delete_msg_after(bot: Bot, chat_id: int, msg_id: int, delay: int = 15):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def check_mod_rights(bot: Bot, chat_id: int, user_id: int, action: str) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
            return True
    except Exception:
        pass

    rights = await get_moderator_rights(chat_id, user_id)
    if rights:
        c_ban, c_mute, c_kick = rights
        if action in ["бан", "разбан"]:
            return c_ban
        if action in ["мут", "размут"]:
            return c_mute
        if action == "кик":
            return c_kick

    return False


async def unban_unmute_task(
    bot: Bot,
    chat_id: int,
    user_id: int,
    first_name: str,
    delay: int,
    action: str,
):
    await asyncio.sleep(delay)
    try:
        if action == "бана":
            # Telegram сам завершает временный ban по until_date.
            text_msg = (
                f"[{first_name}](tg://user?id={user_id}) "
                "Время бана окончено, доступ снова открыт."
            )
        elif action == "мута":
            # Telegram также сам снимает временный restrict по until_date.
            text_msg = (
                f"[{first_name}](tg://user?id={user_id}) "
                "Время мута окончено, вы снова можете писать."
            )
        else:
            return

        msg = await bot.send_message(chat_id, text_msg, parse_mode="Markdown")
        asyncio.create_task(delete_msg_after(bot, chat_id, msg.message_id, 15))
    except Exception as e:
        print(f"Ошибка уведомления о снятии наказания: {e}")


async def captcha_timer(bot: Bot, chat_id: int, user_id: int, msg_id: int):
    await asyncio.sleep(12)
    u_cache = spam_cache.get(chat_id, {}).get(user_id)
    if u_cache and u_cache.get("pending"):
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        spam_cache[chat_id].pop(user_id, None)


async def get_main_menu(user_id: int):
    groups = await get_user_groups(user_id)
    kb = []

    if groups:
        kb = [
            [InlineKeyboardButton(text=f"👥 {g_title}", callback_data=f"manage_{g_id}")]
            for g_id, g_title in groups
        ]
        kb.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="refresh_main")])
        kb.append([InlineKeyboardButton(text="➕ Добавить в группу", url=ADD_URL)])
        return "Ваши группы:", InlineKeyboardMarkup(inline_keyboard=kb)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить список", callback_data="refresh_main")],
            [InlineKeyboardButton(text="➕ Добавить в группу", url=ADD_URL)],
        ]
    )
    return "Я бот-модератор. Добавь меня в группу!", kb


# ==========================================
# РОУТЕРЫ
# ==========================================
private_router = Router()
group_router = Router()
group_router.message.filter(F.chat.type.in_({"group", "supergroup"}))


# ==========================================
# ОБРАБОТЧИКИ ГРУППЫ
# ==========================================
@group_router.message(CommandStart())
async def group_start_cmd(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


@group_router.message(
    F.new_chat_members
    | F.left_chat_member
    | F.new_chat_title
    | F.new_chat_photo
    | F.delete_chat_photo
    | F.pinned_message
)
async def handle_system_messages(message: Message, bot: Bot):
    try:
        await message.delete()
    except Exception:
        pass

    if message.new_chat_members:
        for new_member in message.new_chat_members:
            if new_member.id != bot.id:
                continue

            chat_id = message.chat.id
            adder_id = message.from_user.id if message.from_user else None

            # GuardBot подключается только если его добавил владелец или администратор.
            adder_is_admin = False
            if adder_id:
                try:
                    adder_member = await bot.get_chat_member(chat_id, adder_id)
                    adder_is_admin = adder_member.status in (
                        ChatMemberStatus.CREATOR,
                        ChatMemberStatus.ADMINISTRATOR,
                    )
                except Exception:
                    adder_is_admin = False

            if not adder_is_admin:
                msg = await message.answer(
                    "❌ Чтобы добавить GuardBot в группу, добавьте его по имени "
                    "владелец или администратор группы.\n\n"
                    "Я выйду автоматически через 5 секунд."
                )
                asyncio.create_task(
                    delete_msg_after(bot, chat_id, msg.message_id, 5)
                )
                async def leave_not_authorized():
                    await asyncio.sleep(5)
                    try:
                        await bot.leave_chat(chat_id)
                    except Exception:
                        pass
                asyncio.create_task(leave_not_authorized())
                return

            await add_group(chat_id, message.chat.title or "Группа", adder_id)

            # Спрашиваем право администратора только один раз на подключение.
            try:
                bot_member = await bot.get_chat_member(chat_id, bot.id)
                bot_is_admin = bot_member.status == ChatMemberStatus.ADMINISTRATOR
            except Exception:
                bot_is_admin = False

            if bot_is_admin:
                text = "✅ GuardBot подключён и уже имеет права администратора."
            else:
                text = (
                    "🛡 GuardBot подключён.\n\n"
                    "Чтобы функции защиты и модерации работали, "
                    "назначьте меня администратором группы."
                )

            msg = await message.answer(text)
            if chat_id not in guard_setup_notified:
                guard_setup_notified.add(chat_id)
            # Это единственное уведомление: дальше при отсутствии админки GuardBot молчит.
            return


@group_router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> MEMBER))
async def track_invites(event: ChatMemberUpdated, bot: Bot):
    adder_id = event.from_user.id
    new_user = event.new_chat_member.user
    chat_id = event.chat.id

    if new_user.id == bot.id:
        return

    if not await guard_bot_is_admin(bot, chat_id):
        return

    await track_user(chat_id, new_user.id, new_user.first_name, new_user.username)

    if new_user.id != adder_id:
        await add_user_invites(adder_id, chat_id, 1)


@group_router.message(F.text | F.caption)
async def handle_group_msgs(message: Message, bot: Bot):
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = message.text or message.caption or ""

    # Без админки GuardBot не выполняет ни одну групповую функцию и молчит.
    if not await guard_bot_is_admin(bot, chat_id):
        return

    await track_user(
        chat_id,
        user_id,
        message.from_user.first_name,
        message.from_user.username,
    )

    settings = await get_group_settings(chat_id)
    if not settings:
        return

    req_invites, spam_protect = settings

    cmd_info = parse_mod_command(text)
    if cmd_info:
        cmd, target_username, target_id_parsed, time_sec, reason = cmd_info
        target_id, target_name = None, None

        if message.reply_to_message and message.reply_to_message.from_user:
            target_id = message.reply_to_message.from_user.id
            target_name = message.reply_to_message.from_user.first_name
        elif target_id_parsed:
            target_id = target_id_parsed
            target_name = f"ID {target_id}"
        elif target_username:
            user_data = await get_user_by_username(chat_id, target_username)
            if user_data:
                target_id, target_name = user_data
            else:
                try:
                    await message.delete()
                except Exception:
                    pass
                msg = await message.answer(
                    f"Пользователь @{target_username} не найден. "
                    "Он должен написать хотя бы одно сообщение."
                )
                asyncio.create_task(delete_msg_after(bot, chat_id, msg.message_id, 15))
                return

        if target_id:
            has_rights = await check_mod_rights(bot, chat_id, user_id, cmd)
            if not has_rights:
                return

            # Нельзя управлять самим ботом.
            if target_id == bot.id:
                return

            try:
                await message.delete()
            except Exception:
                pass

            until = int(time.time()) + time_sec if time_sec > 0 else None
            mod_link = f"[{message.from_user.first_name}](tg://user?id={user_id})"
            target_link = f"[{target_name}](tg://user?id={target_id})"
            time_text = format_time_text(time_sec)

            msg = None

            try:
                if cmd == "бан":
                    await bot.ban_chat_member(
                        chat_id,
                        target_id,
                        until_date=until,
                        revoke_messages=True,
                    )
                    msg = await message.answer(
                        f"🔨 Участник {target_link} забанен\n"
                        f"Модератор: {mod_link}\n"
                        f"Время: {time_text}\n"
                        f"Причина: {reason}",
                        parse_mode="Markdown",
                    )
                    if time_sec > 0:
                        asyncio.create_task(
                            unban_unmute_task(bot, chat_id, target_id, target_name, time_sec, "бана")
                        )

                elif cmd == "мут":
                    await bot.restrict_chat_member(
                        chat_id,
                        target_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=until,
                    )
                    msg = await message.answer(
                        f"🤐 Участник {target_link} замучен\n"
                        f"Модератор: {mod_link}\n"
                        f"Время: {time_text}\n"
                        f"Причина: {reason}",
                        parse_mode="Markdown",
                    )
                    if time_sec > 0:
                        asyncio.create_task(
                            unban_unmute_task(bot, chat_id, target_id, target_name, time_sec, "мута")
                        )

                elif cmd == "кик":
                    await bot.ban_chat_member(chat_id, target_id)
                    await asyncio.sleep(0.5)
                    await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)

                    if message.reply_to_message:
                        try:
                            await message.reply_to_message.delete()
                        except Exception:
                            pass

                    msg = await message.answer(
                        f"👢 Участник {target_link} кикнут\n"
                        f"Модератор: {mod_link}\n"
                        f"Причина: {reason}",
                        parse_mode="Markdown",
                    )

                elif cmd == "разбан":
                    # Здесь был один из главных багов исходника:
                    # раньше вызывался restrict_chat_member вместо unban_chat_member.
                    await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
                    msg = await message.answer(
                        f"✅ Участник {target_link} был разбанен\n"
                        f"Модератор: {mod_link}",
                        parse_mode="Markdown",
                    )

                elif cmd == "размут":
                    await bot.restrict_chat_member(
                        chat_id,
                        target_id,
                        permissions=UNMUTE_PERMS,
                    )
                    msg = await message.answer(
                        f"✅ Участник {target_link} был размучен\n"
                        f"Модератор: {mod_link}",
                        parse_mode="Markdown",
                    )

            except Exception as e:
                print(f"Ошибка модерации: {e}")
                msg = await message.answer(f"❌ Не удалось выполнить действие: {e}")

            if msg:
                asyncio.create_task(delete_msg_after(bot, chat_id, msg.message_id, 15))
            return

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
            return
    except Exception:
        pass

    if req_invites > 0:
        current_invites, is_allowed = await get_user_invites(user_id, chat_id)
        if not is_allowed and current_invites < req_invites:
            try:
                await message.delete()
            except Exception:
                pass

            until_date = int(time.time()) + 60
            try:
                await bot.restrict_chat_member(
                    chat_id,
                    user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date,
                )
            except Exception:
                pass

            invite_warnings.setdefault(chat_id, {})
            old_msg_id = invite_warnings[chat_id].get(user_id)
            if old_msg_id:
                try:
                    await bot.delete_message(chat_id, old_msg_id)
                except Exception:
                    pass

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Я добавил", callback_data=f"check_{user_id}")],
                    [InlineKeyboardButton(text="🔓 Отпустить", callback_data=f"release_{user_id}")],
                ]
            )

            msg = await message.answer(
                f"[{message.from_user.first_name}](tg://user?id={user_id}), вам нельзя писать в группе!\n\n"
                f"Для получения доступа нужно добавить {req_invites} {plural_friends(req_invites)}.\n"
                "🤐 _Вы временно заглушены на 1 минуту, чтобы не спамить._",
                reply_markup=kb,
                parse_mode="Markdown",
            )

            invite_warnings[chat_id][user_id] = msg.message_id

            async def del_invite_warning(b, c, m, u):
                await asyncio.sleep(60)
                try:
                    await b.delete_message(c, m)
                except Exception:
                    pass
                if invite_warnings.get(c, {}).get(u) == m:
                    invite_warnings[c].pop(u, None)

            asyncio.create_task(del_invite_warning(bot, chat_id, msg.message_id, user_id))
            return

    if spam_protect:
        spam_cache.setdefault(chat_id, {})
        now = time.time()

        spam_cache[chat_id].setdefault(
            user_id,
            {
                "text": "",
                "dupes": 0,
                "history": [],
                "all_msgs": [],
                "verified": False,
                "pending": False,
                "banned": False,
            },
        )

        u_cache = spam_cache[chat_id][user_id]

        if u_cache.get("banned") or u_cache.get("pending"):
            try:
                await message.delete()
            except Exception:
                pass
            return

        u_cache["all_msgs"].append((message.message_id, now))
        u_cache["all_msgs"] = [
            (mid, t) for mid, t in u_cache["all_msgs"] if now - t <= 300
        ]

        u_cache["history"] = [t for t in u_cache["history"] if now - t <= 6]
        u_cache["history"].append(now)

        normalized = text.strip().lower()
        if normalized == u_cache["text"] and normalized:
            u_cache["dupes"] += 1
        else:
            u_cache["text"] = normalized
            u_cache["dupes"] = 1

        if u_cache["dupes"] >= 3 or len(u_cache["history"]) >= 10:
            try:
                await message.delete()
            except Exception:
                pass

            for mid, _ in u_cache["all_msgs"]:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception:
                    pass
            u_cache["all_msgs"] = []

            if u_cache["verified"]:
                if not u_cache.get("banned"):
                    u_cache["banned"] = True
                    try:
                        await bot.ban_chat_member(chat_id, user_id)
                    except Exception:
                        pass
                    spam_cache[chat_id].pop(user_id, None)
                    msg = await message.answer(
                        f"⛔️ Пользователь [{message.from_user.first_name}]"
                        f"(tg://user?id={user_id}) заблокирован за продолжение спама.",
                        parse_mode="Markdown",
                    )
                    asyncio.create_task(delete_msg_after(bot, chat_id, msg.message_id, 15))
            else:
                if not u_cache["pending"]:
                    u_cache["pending"] = True
                    try:
                        await bot.restrict_chat_member(
                            chat_id,
                            user_id,
                            permissions=ChatPermissions(can_send_messages=False),
                        )
                    except Exception:
                        pass

                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🤖 Я не бот", callback_data=f"captcha_{user_id}")]
                        ]
                    )
                    msg = await message.answer(
                        f"⚠️ [{message.from_user.first_name}](tg://user?id={user_id}), "
                        "сработала защита от спама.\n\n"
                        "Подтвердите, что вы живой человек. У вас есть 12 секунд до **БАНА**.",
                        reply_markup=kb,
                        parse_mode="Markdown",
                    )
                    asyncio.create_task(captcha_timer(bot, chat_id, user_id, msg.message_id))
            return


@group_router.callback_query(F.data.startswith("captcha_"))
async def captcha_verify(call: CallbackQuery, bot: Bot):
    target_id = int(call.data.split("_", 1)[1])
    if call.from_user.id != target_id:
        return await call.answer("Эта кнопка не для вас!", show_alert=True)

    chat_id = call.message.chat.id
    if not await guard_bot_is_admin(bot, chat_id):
        return await call.answer()
    u_cache = spam_cache.get(chat_id, {}).get(target_id)

    if u_cache and u_cache.get("pending"):
        u_cache["verified"] = True
        u_cache["pending"] = False
        u_cache["dupes"] = 0
        u_cache["history"] = []

        await bot.restrict_chat_member(chat_id, target_id, permissions=UNMUTE_PERMS)
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer("Проверка пройдена. Можете писать!", show_alert=True)
    else:
        await call.answer("Проверка уже завершена или истекла.", show_alert=True)


@group_router.callback_query(F.data.startswith("check_"))
async def check_invites(call: CallbackQuery, bot: Bot):
    target_id = int(call.data.split("_", 1)[1])
    if call.from_user.id != target_id:
        return await call.answer("Это не ваша кнопка!", show_alert=True)

    if not await guard_bot_is_admin(bot, call.message.chat.id):
        return await call.answer()

    settings = await get_group_settings(call.message.chat.id)
    req = settings[0] if settings else 0
    current, is_allowed = await get_user_invites(target_id, call.message.chat.id)

    if is_allowed or current >= req:
        await allow_user(target_id, call.message.chat.id)
        try:
            await bot.restrict_chat_member(
                call.message.chat.id,
                target_id,
                permissions=UNMUTE_PERMS,
            )
        except Exception:
            pass
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer("Доступ разрешен! Можете писать.", show_alert=True)
    else:
        await call.answer(f"Вы добавили только {current} из {req}!", show_alert=True)


@group_router.callback_query(F.data.startswith("release_"))
async def release_user(call: CallbackQuery, bot: Bot):
    try:
        member = await bot.get_chat_member(call.message.chat.id, call.from_user.id)
        if member.status not in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
            return await call.answer(
                "Эта кнопка доступна только Владельцу и Админам!",
                show_alert=True,
            )
    except Exception:
        return

    target_id = int(call.data.split("_", 1)[1])
    if not await guard_bot_is_admin(bot, call.message.chat.id):
        return await call.answer()
    await allow_user(target_id, call.message.chat.id)
    try:
        await bot.restrict_chat_member(
            call.message.chat.id,
            target_id,
            permissions=UNMUTE_PERMS,
        )
    except Exception:
        pass
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Пользователь отпущен! Теперь он может писать.", show_alert=True)


# ==========================================
# ЛИЧНЫЕ СООБЩЕНИЯ БОТУ
# ==========================================
@private_router.callback_query(F.data == "refresh_main")
async def refresh_main(call: CallbackQuery):
    text, markup = await get_main_menu(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=markup)
        await call.answer("Список групп успешно обновлен!")
    except Exception:
        await call.answer("Изменений в списке групп нет.", show_alert=False)


@private_router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    last_bot_msg = data.get("last_bot_msg")

    await state.clear()
    if last_bot_msg:
        await state.update_data(last_bot_msg=last_bot_msg)

    text, markup = await get_main_menu(call.from_user.id)
    await call.message.edit_text(text, reply_markup=markup)


@private_router.callback_query(F.data.startswith("manage_"))
async def manage_group(call: CallbackQuery, state: FSMContext):
    group_id = int(call.data.split("_", 1)[1])

    # Дополнительная защита от старых/поддельных callback-кнопок.
    if not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("У вас нет доступа к этой группе.", show_alert=True)

    await state.update_data(current_group=group_id)
    clean_id = str(group_id).replace("-100", "", 1)

    groups = await get_user_groups(call.from_user.id)
    group_title = "Группа"
    for g_id, g_title in groups:
        if g_id == group_id:
            group_title = g_title
            break

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Колл. обяз. приглашений",
                    callback_data="settings_invites",
                )
            ],
            [InlineKeyboardButton(text="🛡 Защита от спама", callback_data="settings_spam")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")],
        ]
    )
    await call.message.edit_text(
        f"⚙️ Управление группой\n[{group_title}](https://t.me/c/{clean_id}/1)",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@private_router.callback_query(F.data == "settings_invites")
async def settings_invites(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    group_id = data.get("current_group")
    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа к этой группе.", show_alert=True)

    settings = await get_group_settings(group_id)
    req_invites = settings[0] if settings else 0

    status_text = "Включено" if req_invites > 0 else "Выключено"
    btn_text = "Выключить" if req_invites > 0 else "Включить"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"toggle_invites_{req_invites}",
                )
            ],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"manage_{group_id}")],
        ]
    )

    await call.message.edit_text(
        "Количество обязательных приглашений\n"
        f"Состояние: **{status_text}**\n"
        f"Сейчас: {req_invites} человек\n\n"
        "Напишите сколько человек надо добавить (или переключите статус):",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(BotConfig.waiting_for_invite_count)
    await state.update_data(msg_to_edit=call.message)


@private_router.callback_query(F.data.startswith("toggle_invites_"))
async def toggle_invites(call: CallbackQuery, state: FSMContext):
    current_val = int(call.data.split("_")[2])
    group_id = (await state.get_data()).get("current_group")

    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа.", show_alert=True)

    await update_req_invites(group_id, 0 if current_val > 0 else 1)
    await settings_invites(call, state)


@private_router.message(BotConfig.waiting_for_invite_count, F.chat.type == "private")
async def process_invite_count(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        return

    count = int(message.text)
    if count < 0 or count > 1000:
        return

    data = await state.get_data()
    group_id = data.get("current_group")
    msg_to_edit = data.get("msg_to_edit")

    if not group_id or not msg_to_edit:
        return

    if not await user_owns_group(message.from_user.id, group_id):
        await state.clear()
        return

    await asyncio.sleep(0.5)

    try:
        await message.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_invites_{count}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"manage_{group_id}")],
        ]
    )

    try:
        await msg_to_edit.edit_text(
            f"Количество обязательных приглашений\nВыбрано: {count} человек\n\n"
            "Подтвердите сохранение:",
            reply_markup=kb,
        )
    except Exception:
        pass

    await state.set_state(None)


@private_router.callback_query(F.data.startswith("confirm_invites_"))
async def confirm_invites(call: CallbackQuery, state: FSMContext):
    count = int(call.data.split("_")[2])
    group_id = (await state.get_data()).get("current_group")

    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа.", show_alert=True)

    await update_req_invites(group_id, count)

    status_text = "Включено" if count > 0 else "Выключено"
    btn_text = "Выключить" if count > 0 else "Включить"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"toggle_invites_{count}",
                )
            ],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"manage_{group_id}")],
        ]
    )

    await call.message.edit_text(
        "Количество обязательных приглашений\n"
        f"Состояние: **{status_text}**\n"
        f"Сейчас: {count} человек",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@private_router.callback_query(F.data == "settings_spam")
async def settings_spam(call: CallbackQuery, state: FSMContext):
    group_id = (await state.get_data()).get("current_group")
    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа.", show_alert=True)

    settings = await get_group_settings(group_id)
    spam_status = settings[1] if settings else False
    status_text = "Включено" if spam_status else "Выключено"
    btn_text = "Выключить" if spam_status else "Включить"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=btn_text, callback_data="toggle_spam")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"manage_{group_id}")],
        ]
    )

    text = (
        "🛡 **Защита от спама** поможет удалять повторяющиеся сообщения.\n\n"
        "**Как это работает:**\n"
        "• **10 сообщений за 6 секунд** ➔ Проверка (Капча).\n"
        "• **3 одинаковых сообщения подряд** ➔ Проверка (Капча).\n"
        "• **Не прошел проверку (12 сек)** ➔ Бан и удаление спама.\n"
        "• **Прошел, но снова спамит** ➔ Бан и удаление спама.\n\n"
        f"Состояние: **{status_text}**"
    )
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@private_router.callback_query(F.data == "toggle_spam")
async def toggle_spam_handler(call: CallbackQuery, state: FSMContext):
    group_id = (await state.get_data()).get("current_group")
    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа.", show_alert=True)

    await toggle_spam(group_id)
    await settings_spam(call, state)


# ==========================================
# ЕДИНЫЙ WEB SERVER + ЗАПУСК
# ==========================================
async def handle_ping(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 HTTP WebServer запущен на порту {port}")
    return runner


async def main():
    runner = await start_web_server()
    try:
        # GuardBot использует отдельный AsyncClient Supabase.
        await init_db()
        print("✅ Supabase GuardBot: OK")

        # Восстанавливаем сохранённые UserBot-сессии.
        await restore_saved_sessions()
        print("✅ UserBot sessions restored")

        dp.include_router(user_router)
        dp.include_router(private_router)
        dp.include_router(group_router)

        print("🤖 Qwitty Bot polling started")
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        if guard_supabase is not None:
            try:
                await guard_supabase.auth.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
