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
import math
import psutil
import ntplib
from aiohttp import web

if sys.platform != "win32":
    try:
        import uvloop
        uvloop.install()
        print("⚡ [Движок]: uvloop успешно активирован (Linux/macOS)")
    except ImportError:
        print("⚙️ [Движок]: uvloop не установлен, используется стандартный asyncio.")
else:
    print("🚀 Запуск Скрипта")

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

from pyrogram import Client, enums, filters
from pyrogram.handlers import MessageHandler
from pyrogram.raw import functions
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, Unauthorized, FloodWait

from supabase import create_client, Client as SupabaseClient

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

                       
ADMIN_ID = 8845929618
ADMIN_USERNAME = "Qwitty_Cc"

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

                                           
                                                              
                                                                       
NTP_OFFSET_SECONDS = 0.0
NTP_LAST_SYNC_MONOTONIC = 0.0
NTP_SYNC_INTERVAL_SECONDS = 900.0                                       
NTP_SYNC_LOCK = asyncio.Lock()

def _get_ntp_offset_sync():
    client = ntplib.NTPClient()
    servers = ["pool.ntp.org", "time.google.com", "time.cloudflare.com"]
    local_before = time.time()
    for server in servers:
        try:
            response = client.request(server, version=3, timeout=2)
            local_after = time.time()
                                                                            
            local_mid = (local_before + local_after) / 2.0
            return float(response.tx_time) - local_mid
        except Exception:
            local_before = time.time()
            continue
    return None

async def sync_world_clock(force=False):
    global NTP_OFFSET_SECONDS, NTP_LAST_SYNC_MONOTONIC

    now_mono = time.monotonic()
    if not force and now_mono - NTP_LAST_SYNC_MONOTONIC < NTP_SYNC_INTERVAL_SECONDS:
        return NTP_OFFSET_SECONDS

    async with NTP_SYNC_LOCK:
        now_mono = time.monotonic()
        if not force and now_mono - NTP_LAST_SYNC_MONOTONIC < NTP_SYNC_INTERVAL_SECONDS:
            return NTP_OFFSET_SECONDS

        offset = await asyncio.to_thread(_get_ntp_offset_sync)
        if offset is not None:
            NTP_OFFSET_SECONDS = offset
            NTP_LAST_SYNC_MONOTONIC = time.monotonic()
            logging.info(f"🌐 Мировое время синхронизировано, поправка: {offset:+.3f} сек.")
        else:
                                                                     
                                                                   
            NTP_LAST_SYNC_MONOTONIC = time.monotonic()
            logging.warning("⚠️ NTP недоступен, используется системное UTC-время.")

    return NTP_OFFSET_SECONDS


def get_world_utc_datetime():
    return datetime.datetime.fromtimestamp(
        time.time() + NTP_OFFSET_SECONDS,
        datetime.timezone.utc,
    )


def get_world_utc_timestamp():
    return time.time() + NTP_OFFSET_SECONDS


async def ntp_sync_loop():
    while True:
        try:
            await sync_world_clock(force=False)
        except Exception as e:
            logging.warning(f"Ошибка фоновой синхронизации NTP: {e}")
        await asyncio.sleep(NTP_SYNC_INTERVAL_SECONDS)


async def sleep_until_next_world_minute():
                                                                  
                                                         
    now_ts = get_world_utc_timestamp()
    delay = 60.0 - (now_ts % 60.0)
    if delay < 0.01:
        delay = 0.01
    await asyncio.sleep(delay)

                        
supabase: SupabaseClient = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("✅ Supabase успешно подключен")
    except Exception as e:
        logging.error(f"❌ Ошибка подключения к Supabase: {e}")

        
SESSIONS_DIR = "sessions"
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

RU_MONTHS = {
    1: "Января", 2: "Февраля", 3: "Марта", 4: "Апреля",
    5: "Мая", 6: "Июня", 7: "Июля", 8: "Августа",
    9: "Сентября", 10: "Октября", 11: "Ноября", 12: "Декабря"
}

def format_date_ru(dt):
    return f"{dt.day} {RU_MONTHS.get(dt.month, '')} {dt.year} года"

                                                                         
BOLD_DIGITS = {
    '0': '𝟬', '1': '𝟭', '2': '𝟮', '3': '𝟯', '4': '𝟰',
    '5': '𝟱', '6': '𝟲', '7': '𝟳', '8': '𝟴', '9': '𝟵'
}

def format_bold_time(time_str):
    return "".join(BOLD_DIGITS.get(ch, ch) for ch in time_str)

def is_admin(user: types.User):
    if user.id != ADMIN_ID:
        return False
    if user.username and user.username.lower() == ADMIN_USERNAME.lower():
        return True
    return False

TIMEZONE_NAMES = {
    -8: "Лос-Анджелес UTC-8",
    -5: "Нью-Йорк UTC-5",
    -3: "Бразилия UTC-3",
    0: "Лондон UTC+0",
    1: "Париж/Берлин UTC+1",
    2: "Афины/Каир UTC+2",
    3: "Москва/Стамбул UTC+3",
    4: "Баку/Тбилиси UTC+4",
    5: "Ташкент/Шымкент UTC+5",
    6: "Астана/Дакка UTC+6",
    8: "Пекин/Сингапур UTC+8",
    9: "Токио/Сеул UTC+9",
}

REGISTRATION_FLOOD_SECONDS_DEFAULT = 0
USER_MESSAGE_DELETE_DELAY = 3

                                                             

def format_remaining_time(seconds):
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        parts = [f"{days} дн."]
        if hours: parts.append(f"{hours} ч.")
        if minutes: parts.append(f"{minutes} мин.")
        if secs: parts.append(f"{secs} сек.")
        return " ".join(parts)
    if hours:
        parts = [f"{hours} ч."]
        if minutes: parts.append(f"{minutes} мин.")
        if secs: parts.append(f"{secs} сек.")
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
    "btn_back": "Назад ⬅️", 
    "btn_back_menu": "Назад в меню 🏠", 
    "btn_confirm": "Подтвердить ✅", 
    "btn_activity": "Активность 📊",
    "btn_autoresp": "Автоответчик 🤖", 
    "btn_timenick": "Время в профиль ⏰", 
    "btn_turn_on": "Включить 🟢",
    "btn_turn_off": "Выключить 🔴", 
    "btn_tz_select": "Выбрать часовой пояс 🌐", 
    "btn_refresh": "Обновить 🔄",
    "btn_autoresp_setup": "Изменить текст ✏️",
    "btn_im_sure": "Я уверен 👍", 
    "btn_register": "Регистрироваться 📝",
    "msg_start": "Здравствуйте!\nДобро пожаловать в бота автоматизированного управления аккаунтом.\nОзнакомьтесь с правилами.",
    "msg_start_register": "Чтобы зарегистрироваться заново, нажмите кнопку ниже 👇",
    "msg_menu": "Доступные нам функции управления вашим аккаунтом:",
    "msg_rules_text": (
        "**🛡 Правила бота**\n\n"
        "**1. Бот работает через юзербота на основе Telegram MTProto, для работы необходимо подключение аккаунта.**\n"
        "**2. Авторизация выполняется через номер телефона, код Telegram и, при необходимости, облачный пароль.**\n"
        "**3. Используйте только свой аккаунт и соблюдайте правила Telegram.**\n\n"
        "**⚠️ Запрещено:**\n\n"
        "**1. Использовать чужие аккаунты без разрешения владельца.**\n"
        "**2. Использовать бота для спама, флуда или других нарушений правил Telegram.**"
    ),
    "msg_rules_done": "Всё, правила прочитаны! 🎉\n\nЖмите кнопку начала ниже, чтобы привязать аккаунт.",
    "msg_phone_req": "Пожалуйста, отправьте ваш номер телефона в международном формате.\nПример: +12345678",
    "msg_code_req": "Код авторизации отправлен в Telegram.\n💬 Напишите код через дефис.\nПример: 12-45-6",
    "msg_pwd_req": "Аккаунт защищен облачным паролем.\nВведите его в чат:",
    "msg_success_login": "Бот успешно зашел в аккаунт!\nНажмите кнопку ниже для продолжения.",
    "msg_btn_go": "Поехали 🚀",
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
    "msg_check_code": "⏳ Проверка кода...\n⏱ Осталось: {0} сек.",
    "msg_code_wrong": "Неправильный код.\nНапишите код заново:",
    "msg_check_pwd": "⏳ Проверка 2FA...\n⏱ Осталось: {0} сек.",
    "msg_pwd_wrong": "❌ Неверный пароль!\nВведите заново:",
    "msg_pwd_ok": "Пароль принят!\nЮзербот успешно запущен.",
    "msg_activity_text": "Ваша история активности (за 5 дней):\n\n{0}",
    "msg_timenick_text": "Вывод текущего времени в имя профиля.\n\nТекущий статус: {0}\nПрофиль: {1}\nСмещение часового пояса: UTC{2}",
    "msg_tz_select": "Выберите ваш часовой пояс🌐", 
    "msg_tz_saved": "Часовой пояс изменен на UTC{0}!",
    "msg_autoresp_text": "🤖 **Автоответчик**\n\nСтатус: {1}\nТекст приветствия:\n💬 \"{0}\"",
    "msg_autoresp_req": "Напишите новый текст приветствия в чат ✏️", 
    "msg_autoresp_saved": "Приветствие успешно сохранено! 🎉",
    "msg_autoresp_default": "👋 Здравствуйте! Сейчас я не в сети, отвечу позже.",
}

PROFILE_TIME_OFFSET_SECONDS = 0

def get_current_styled_profile_preview(base_first, base_last, offset, include_nick=True, include_time=True):
    clean_first = (base_first or "User").strip() or "User"
    clean_last = (base_last or "").strip()
    first = clean_first
    last = clean_last
    if include_time:
        utc_now = get_world_utc_datetime()
        tz_now = (
            utc_now
            + datetime.timedelta(hours=offset)
            + datetime.timedelta(seconds=PROFILE_TIME_OFFSET_SECONDS)
        )
        raw_time = tz_now.strftime("%H:%M")
        bold_time = format_bold_time(raw_time)
        time_marker = f"[{bold_time}]"
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
            "time_nick_active": False, "time_nick_task": None,
            "autoresponder_active": False, "activity_task": None, "delete_count": 100,
            "registration_block_until_ts": 0.0,
            "ui_action_count": 0,
            "temp_greeting": None,
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
    if data["activity_task"]: data["activity_task"].cancel()

    data["time_nick_active"] = False
    data["autoresponder_active"] = False

    if data["client"]:
        await close_pyrogram_client(data["client"])
        data["client"] = None

    await clear_session_files(user_id)

    uid_str = str(user_id)
    if uid_str in MEMORY_DB["config"]:
        MEMORY_DB["config"][uid_str]["logged_in"] = False
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
    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        pass

class IncomingUserMessageCleanupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.Message) and event.from_user and not event.from_user.is_bot:
            asyncio.create_task(delete_user_message_later(event))
        return await handler(event, data)

dp.callback_query.middleware(RestartMiddleware())
dp.message.middleware(IncomingUserMessageCleanupMiddleware())

async def edit_or_send(user_id, text, reply_markup=None, parse_mode=None):
    data = get_user_state(user_id)
    force_new_message = (
        data.get("ui_action_count", 0) > 0
        and data["ui_action_count"] % 5 == 0
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

def show_start_menu(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_view")
    builder.button(text=get_text(user_id, "btn_start"), callback_data="start_login")
    builder.adjust(1)
    return builder.as_markup()


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

async def update_profile_branding(user_id):
    data = get_user_state(user_id)
    uid_str = str(user_id)

    if not data.get("client") or not data["client"].is_connected:
        return

    try:
                                                                                  
        user_cfg = MEMORY_DB["config"].get(uid_str)
        if not user_cfg:
            user_cfg = await async_db_get("config", uid_str) or {}
            MEMORY_DB["config"][uid_str] = user_cfg

        base_first = (user_cfg.get("profile_base_first_name") or "User").strip() or "User"
        base_last = (user_cfg.get("profile_base_last_name") or "").strip()

                                                                       
        if "profile_base_first_name" not in user_cfg or "profile_base_last_name" not in user_cfg:
            me = await data["client"].get_me()
            user_cfg = await ensure_profile_base(user_id, me)
            base_first = (user_cfg.get("profile_base_first_name") or me.first_name or "User").strip() or "User"
            base_last = (user_cfg.get("profile_base_last_name") or me.last_name or "").strip()

        if not user_cfg.get("time_nick_active", False):
            return

        offset = int(user_cfg.get("timezone_offset", 5))
        utc_now = get_world_utc_datetime()
        tz_now = (
            utc_now
            + datetime.timedelta(hours=offset)
            + datetime.timedelta(seconds=PROFILE_TIME_OFFSET_SECONDS)
        )
        time_value = tz_now.strftime('%H:%M')
        bold_time = format_bold_time(time_value)
        time_marker = f"[{bold_time}]"

        new_first = base_first
        new_last = f"{base_last} {time_marker}" if base_last else base_last
        if not base_last:
            new_first = f"{base_first} {time_marker}"

                                                              
                                                                             
        profile_key = (new_first, new_last)
        if data.get("last_profile_key") == profile_key:
            return

        await data["client"].update_profile(first_name=new_first, last_name=new_last)
        data["last_profile_key"] = profile_key

                                                                       
                                           
    except Exception as e:
        logging.error(f"Ошибка брендинга профиля: {e}")

async def time_nickname_loop(user_id):
    data = get_user_state(user_id)

    while data.get("time_nick_active", False):
        try:
            await sleep_until_next_world_minute()
        except asyncio.CancelledError:
            raise

        if not data.get("time_nick_active", False):
            break
        if not data.get("client") or not data["client"].is_connected:
            break

        try:
            await update_profile_branding(user_id)
        except Unauthorized:
            await handle_revoked_session(user_id, "сессия деактивирована")
            break
        except Exception as e:
            logging.error(f"Ошибка обновления времени в профиле: {e}")

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

                if user_cfg.get("time_nick_active", False):
                    data["time_nick_active"] = True
                    if not data.get("time_nick_task") or data["time_nick_task"].done():
                        data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
                                                                              
                                                                            
                    asyncio.create_task(update_profile_branding(user_id))

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

        if user_cfg.get("time_nick_active", False):
            data["time_nick_active"] = True
            data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
                                                                              
            asyncio.create_task(update_profile_branding(user_id))
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
    loaded = 0

    for row in rows:
        uid_str = str(row.get("id", "")).strip()
        cfg = row.get("data") or {}

        if not uid_str or not isinstance(cfg, dict):
            continue

        MEMORY_DB["config"][uid_str] = cfg
        loaded += 1

        if not cfg.get("logged_in") or not cfg.get("session_string"):
            continue

        needs_runtime = any([
            cfg.get("autoresponder_active", False),
            cfg.get("time_nick_active", False),
        ])
        if not needs_runtime:
            continue

        try:
            await ensure_client_connected(int(uid_str))
            state = get_user_state(int(uid_str))
            if state.get("client"):
                restored += 1
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            logging.error(f"Ошибка восстановления аккаунта {uid_str}: {e}")

    logging.info(
        f"🔄 Восстановление данных: загружено={loaded}, "
        f"сессий запущено={restored}, сессий пропущено={skipped}"
    )

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    data = get_user_state(user_id)
    uid_str = str(user_id)
    if data.get("msg_id"):
        try:
            await bot.delete_message(chat_id=user_id, message_id=data["msg_id"])
        except Exception:
            pass
        data["msg_id"] = None
        data["ui_action_count"] = 0

    if uid_str not in MEMORY_DB["config"]:
        MEMORY_DB["config"][uid_str] = db_get_data("config", uid_str) or {
            "phone": "Не указан", "password": "Нет",
            "time_nick_active": False, "autoresponder_active": False,
            "autoresponder_greeting": get_text(user_id, "msg_autoresp_default"),
            "timezone_offset": 5,
            "used_timenick_seconds": 0.0,
            "registration_block_until_ts": 0.0,
            "replied_users": [], "autoresponder_last_replied": {},
            "profile_base_first_name": message.from_user.first_name or "User",
            "profile_base_last_name": "",
            "username": message.from_user.username or "N/A",
            "first_name": message.from_user.first_name or "User", "logged_in": False,
            "ever_registered": False,
            "last_entry_at": None,
            "entry_first_name": message.from_user.first_name or "User",
            "entry_username": message.from_user.username or "N/A",
            "entry_phone": "Не виден",
            "msg_id": None, "session_string": None
        }
        asyncio.create_task(async_db_save("config", uid_str, MEMORY_DB["config"][uid_str]))

    cfg = MEMORY_DB["config"][uid_str]
    if not cfg.get("logged_in", False) and not cfg.get("ever_registered", False):
        cfg["last_entry_at"] = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        cfg["entry_first_name"] = message.from_user.first_name or "User"
        cfg["entry_username"] = message.from_user.username or "N/A"
        cfg["entry_phone"] = cfg.get("phone") if cfg.get("phone") not in (None, "", "Не указан") else "Не виден"
        MEMORY_DB["config"][uid_str] = cfg
        asyncio.create_task(async_db_save("config", uid_str, cfg))

    is_valid = await ensure_client_connected(user_id)
    if is_valid:
        log_action(user_id, "Ввёл команду /start")
        data["state"] = "MENU"
        await edit_or_send(user_id, get_text(user_id, "msg_menu"),
                           reply_markup=show_main_menu_builder(user_id, message.from_user).as_markup())
    else:
        data["state"] = "START"
        log_action(user_id, "Ввёл команду /start")
        if is_registration_blocked(user_id):
            await edit_or_send(
                user_id,
                get_registration_block_text(user_id),
                reply_markup=show_registration_block_markup(user_id),
                parse_mode="Markdown"
            )
        else:
            await edit_or_send(user_id, get_text(user_id, "msg_start"), reply_markup=show_start_menu(user_id))

@dp.callback_query(F.data.in_(["rules_view", "rules_menu_view"]))
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

@dp.callback_query(F.data == "rules_accepted")
async def rules_accepted(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_view")
    builder.button(text=get_text(user_id, "btn_start"), callback_data="start_login")
    builder.adjust(1)
    await edit_or_send(user_id, get_text(user_id, "msg_rules_done"), reply_markup=builder.as_markup(), parse_mode="Markdown")
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data == "start_re_register_menu")
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

@dp.callback_query(F.data == "registration_block_back")
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

@dp.callback_query(F.data == "start_login")
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

@dp.callback_query(F.data == "cancel_auth")
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

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_PHONE")
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
        "time_nick_active": data["time_nick_active"],
        "autoresponder_active": data.get("autoresponder_active", old_cfg.get("autoresponder_active", False)),
        "autoresponder_greeting": old_cfg.get("autoresponder_greeting", get_text(user_id, "msg_autoresp_default")),
        "timezone_offset": old_cfg.get("timezone_offset", 5),
        "delete_today_count": old_cfg.get("delete_today_count", 0),
        "delete_limit_reset_ts": old_cfg.get("delete_limit_reset_ts", 0.0),
        "registration_block_until_ts": old_cfg.get("registration_block_until_ts", 0.0),
        "used_timenick_seconds": old_cfg.get("used_timenick_seconds", 0.0),
        "replied_users": old_cfg.get("replied_users", []),
        "autoresponder_last_replied": old_cfg.get("autoresponder_last_replied", {}),
        "profile_base_first_name": old_cfg.get("profile_base_first_name", message.from_user.first_name or "User"),
        "profile_base_last_name": old_cfg.get("profile_base_last_name", ""),
        "username": message.from_user.username or old_cfg.get("username", "N/A"),
        "first_name": message.from_user.first_name or old_cfg.get("first_name", "User"),
        "logged_in": is_logged_in,
        "ever_registered": True if is_logged_in else old_cfg.get("ever_registered", False),
        "last_entry_at": old_cfg.get("last_entry_at"),
        "entry_first_name": old_cfg.get("entry_first_name", message.from_user.first_name or "User"),
        "entry_username": old_cfg.get("entry_username", message.from_user.username or "N/A"),
        "entry_phone": old_cfg.get("entry_phone", "Не виден"),
        "msg_id": data.get("msg_id", old_cfg.get("msg_id", None)),
        "session_string": old_cfg.get("session_string")
    }
    MEMORY_DB["config"][uid_str] = cfg
    asyncio.create_task(async_db_save("config", uid_str, cfg))

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_CODE")
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
        data["state"] = "MENU"
        await edit_or_send(user_id, get_text(user_id, "msg_menu"), reply_markup=show_main_menu_builder(user_id, message.from_user).as_markup())
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

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_PASSWORD")
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
        data["state"] = "MENU"
        await edit_or_send(user_id, get_text(user_id, "msg_menu"), reply_markup=show_main_menu_builder(user_id, message.from_user).as_markup())
    except Exception:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_pwd_wrong"), reply_markup=builder.as_markup())

def show_main_menu_builder(user_id, user_obj: types.User = None):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_autoresp"), callback_data="menu_autoresponder")
    builder.button(text=get_text(user_id, "btn_timenick"), callback_data="menu_timenick")
    builder.adjust(2)
    return builder

@dp.callback_query(F.data == "main_menu")
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
    await edit_or_send(user_id, get_text(user_id, "msg_menu"), reply_markup=show_main_menu_builder(user_id, callback.from_user).as_markup())
    try: await callback.answer()
    except Exception: pass

                                                                 

@dp.callback_query(F.data == "menu_activity")
async def menu_activity(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    is_valid = await ensure_client_connected(user_id)
    if not is_valid:
        await edit_or_send(user_id, get_text(user_id, "msg_session_missing"), reply_markup=get_missing_session_markup(user_id))
        try: await callback.answer()
        except Exception: pass
        return

    uid_str = str(user_id)
    activity_data = MEMORY_DB["activity"].get(uid_str) or await async_db_get("activity", uid_str) or {}
    
    lines = []
    today_date = datetime.datetime.now().date()
    for i in range(4, -1, -1):
        d = today_date - datetime.timedelta(days=i)
        date_str = d.strftime("%d.%m.%Y")
        seconds = activity_data.get(date_str, 0)
        formatted_time = format_remaining_time(seconds) if seconds > 0 else "0 сек."
        lines.append(f"📅 {date_str}: {formatted_time}")

    text = get_text(user_id, "msg_activity_text", "\n".join(lines))
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data == "menu_autoresponder")
async def menu_autoresponder(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    is_valid = await ensure_client_connected(user_id)
    if not is_valid:
        await edit_or_send(user_id, get_text(user_id, "msg_session_missing"), reply_markup=get_missing_session_markup(user_id))
        try: await callback.answer()
        except Exception: pass
        return

    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    is_active = cfg.get("autoresponder_active", False)
    status_str = get_text(user_id, "status_on") if is_active else get_text(user_id, "status_off")
    greeting = cfg.get("autoresponder_greeting", get_text(user_id, "msg_autoresp_default"))

    text = get_text(user_id, "msg_autoresp_text", greeting, status_str)

    builder = InlineKeyboardBuilder()
    btn_toggle_text = get_text(user_id, "btn_turn_off") if is_active else get_text(user_id, "btn_turn_on")
    builder.button(text=btn_toggle_text, callback_data="toggle_autoresponder")
    builder.button(text=get_text(user_id, "btn_autoresp_setup"), callback_data="autoresp_setup")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(2, 1)

    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data == "toggle_autoresponder")
async def toggle_autoresponder(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    
    new_status = not cfg.get("autoresponder_active", False)
    cfg["autoresponder_active"] = new_status
    data["autoresponder_active"] = new_status
    MEMORY_DB["config"][uid_str] = cfg
    asyncio.create_task(async_db_save("config", uid_str, cfg))

    log_action(user_id, f"Автоответчик: {'Включен' if new_status else 'Выключен'}")
    await menu_autoresponder(callback)

@dp.callback_query(F.data == "autoresp_setup")
async def autoresp_setup(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    data["state"] = "WAITING_AUTORESP_TEXT"

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_autoresponder")
    await edit_or_send(user_id, get_text(user_id, "msg_autoresp_req"), reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_AUTORESP_TEXT")
async def process_autoresp_text(message: types.Message):
    user_id = message.from_user.id
    data = get_user_state(user_id)
    new_text = message.text.strip() if message.text else ""

    if new_text:
        uid_str = str(user_id)
        cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
        cfg["autoresponder_greeting"] = new_text
        MEMORY_DB["config"][uid_str] = cfg
        asyncio.create_task(async_db_save("config", uid_str, cfg))
        log_action(user_id, "Изменён текст автоответчика")

    data["state"] = "MENU"
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_autoresponder")
    await edit_or_send(user_id, get_text(user_id, "msg_autoresp_saved"), reply_markup=builder.as_markup())

@dp.callback_query(F.data == "menu_timenick")
async def menu_timenick(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    is_valid = await ensure_client_connected(user_id)
    if not is_valid:
        await edit_or_send(user_id, get_text(user_id, "msg_session_missing"), reply_markup=get_missing_session_markup(user_id))
        try: await callback.answer()
        except Exception: pass
        return

    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    is_active = cfg.get("time_nick_active", False)
    status_str = get_text(user_id, "status_on") if is_active else get_text(user_id, "status_off")
    offset = cfg.get("timezone_offset", 5)

    base_first = cfg.get("profile_base_first_name", "User")
    base_last = cfg.get("profile_base_last_name", "")

    profile_preview = get_current_styled_profile_preview(base_first, base_last, offset, include_time=is_active)
    sign_str = f"+{offset}" if offset >= 0 else str(offset)

    text = get_text(user_id, "msg_timenick_text", status_str, profile_preview, sign_str)

    builder = InlineKeyboardBuilder()
    btn_toggle_text = get_text(user_id, "btn_turn_off") if is_active else get_text(user_id, "btn_turn_on")
    builder.button(text=btn_toggle_text, callback_data="toggle_timenick")
    builder.button(text=get_text(user_id, "btn_tz_select"), callback_data="tz_select")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(2, 1)

    await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data == "toggle_timenick")
async def toggle_timenick(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}

    new_status = not cfg.get("time_nick_active", False)
    cfg["time_nick_active"] = new_status
    data["time_nick_active"] = new_status
    MEMORY_DB["config"][uid_str] = cfg
    asyncio.create_task(async_db_save("config", uid_str, cfg))

    if new_status:
        if not data.get("time_nick_task") or data["time_nick_task"].done():
            data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
        asyncio.create_task(update_profile_branding(user_id))
    else:
        if data.get("time_nick_task"):
            data["time_nick_task"].cancel()
            data["time_nick_task"] = None
        if data.get("client") and data["client"].is_connected:
            try:
                base_first = cfg.get("profile_base_first_name", "User")
                base_last = cfg.get("profile_base_last_name", "")
                await data["client"].update_profile(first_name=base_first, last_name=base_last)
            except Exception as e:
                logging.error(f"Ошибка сброса имени профиля: {e}")

    log_action(user_id, f"Время в профиле: {'Включено' if new_status else 'Выключено'}")
    await menu_timenick(callback)

@dp.callback_query(F.data == "tz_select")
async def tz_select(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    builder = InlineKeyboardBuilder()
    for tz_val, tz_name in TIMEZONE_NAMES.items():
        builder.button(text=tz_name, callback_data=f"set_tz_{tz_val}")
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_timenick")
    builder.adjust(2)

    await edit_or_send(user_id, get_text(user_id, "msg_tz_select"), reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("set_tz_"))
async def set_timezone(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    tz_val = int(callback.data.split("_")[-1])
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    cfg["timezone_offset"] = tz_val
    MEMORY_DB["config"][uid_str] = cfg
    asyncio.create_task(async_db_save("config", uid_str, cfg))

    sign_str = f"+{tz_val}" if tz_val >= 0 else str(tz_val)
    log_action(user_id, f"Изменён часовой пояс: UTC{sign_str}")

    if cfg.get("time_nick_active", False):
        asyncio.create_task(update_profile_branding(user_id))

    await menu_timenick(callback)

                                                      

@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: types.CallbackQuery):
    try: await callback.answer()
    except Exception: pass

def build_admin_menu_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="Активнные🟢", callback_data="admin_users_1")
    builder.button(text="Не-входящие🔴", callback_data="admin_entries_1")
    builder.button(text="Назад в меню 🏠", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

@dp.message(F.text.casefold() == "admin")
async def admin_command(message: types.Message):
                                                                                        
    if not is_admin(message.from_user):
        return

    data = get_user_state(message.from_user.id)
    data["state"] = "ADMIN"
    await edit_or_send(
        message.from_user.id,
        "Админ меню:",
        reply_markup=build_admin_menu_markup(),
    )

@dp.callback_query(F.data == "admin_users_back")
async def admin_users_back(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        return
    await edit_or_send(
        callback.from_user.id,
        "Админ меню:",
        reply_markup=build_admin_menu_markup(),
    )
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_entries_"))
async def admin_entries_list(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return

    page = int(callback.data.split("_")[-1])
    entries = []
    for uid, cfg in MEMORY_DB["config"].items():
        if cfg.get("ever_registered", cfg.get("logged_in", False)):
            continue
        if not cfg.get("last_entry_at"):
            continue
        entries.append((uid, cfg))

    entries.sort(key=lambda item: item[1].get("last_entry_at", ""), reverse=True)

    per_page = 8
    total_entries = len(entries)
    total_pages = max(1, (total_entries + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    current_page = entries[(page - 1) * per_page:page * per_page]

    builder = InlineKeyboardBuilder()
    for uid, cfg in current_page:
        first_name = cfg.get("entry_first_name") or cfg.get("first_name") or "User"
        builder.button(text=f"👤 {first_name}", callback_data=f"admin_entry_{uid}")
    builder.adjust(1)

    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_entries_{page-1}"))
    nav_buttons.append(types.InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        nav_buttons.append(types.InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_entries_{page+1}"))
    builder.row(*nav_buttons)
    builder.button(text="⬅️ В админ меню", callback_data="admin_users_back")

    await edit_or_send(callback.from_user.id, f"Список не-входящих ({total_entries}):", reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_entry_"))
async def admin_entry_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    target_uid = callback.data.split("_")[-1]
    cfg = MEMORY_DB["config"].get(target_uid) or db_get_data("config", target_uid) or {}
    if cfg.get("ever_registered", cfg.get("logged_in", False)):
        await admin_entries_list(callback)
        return

    first_name = cfg.get("entry_first_name") or cfg.get("first_name") or "User"
    username = cfg.get("entry_username") or cfg.get("username") or "N/A"
    username_str = f"@{username}" if username != "N/A" else "Не указан"
    phone = cfg.get("entry_phone") or "Не виден"
    entry_time = cfg.get("last_entry_at") or "Неизвестно"

    text = (
        f"Никнейм: {first_name}\n"
        f"Юзернейм: {username_str}\n"
        f"Номер: {phone}\n"
        f"Последний вход: {entry_time}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_entries_1")
    builder.adjust(1)
    await edit_or_send(callback.from_user.id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

async def admin_validate_session(user_id, cfg, semaphore=None):
    """Быстрая проверка сессии для админского списка.
    Не делает повторные попытки: один сломанный/отозванный аккаунт
    не должен тормозить весь список активных пользователей.
    """
    if semaphore is not None:
        async with semaphore:
            return await _admin_validate_session(user_id, cfg)
    return await _admin_validate_session(user_id, cfg)

async def _admin_validate_session(user_id, cfg):
    if not cfg.get("logged_in", False):
        return False

    data = get_user_state(user_id)
    client = data.get("client")

    if client and client.is_connected:
        try:
            await client.get_me()
            return True
        except Unauthorized:
            try:
                await handle_revoked_session(user_id, reason="сессия деактивирована пользователем")
            except Exception as e:
                logging.warning(f"Не удалось обработать отозванную сессию {user_id}: {e}")
            return False
        except Exception as e:
            # Клиент подключён, а ошибка может быть временной.
            # Не удаляем такого пользователя из активных только из-за transient-ошибки.
            logging.warning(f"Временная проверка активности {user_id}: {e}")
            return True

    session_string = cfg.get("session_string")
    if not session_string:
        return False

    try:
        client = await _build_runtime_client(user_id, session_string)
        data["client"] = client
        start_activity_tracker(user_id)

        if cfg.get("time_nick_active", False):
            data["time_nick_active"] = True
            if not data.get("time_nick_task") or data["time_nick_task"].done():
                data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
            asyncio.create_task(update_profile_branding(user_id))

        data["autoresponder_active"] = cfg.get("autoresponder_active", False)
        return True
    except Unauthorized:
        try:
            await handle_revoked_session(user_id, reason="сохранённая сессия отозвана Telegram")
        except Exception as e:
            logging.warning(f"Не удалось очистить отозванную сессию {user_id}: {e}")
        return False
    except Exception as e:
        logging.warning(f"Недоступна сессия пользователя {user_id}: {e}")
        return False

@dp.callback_query(F.data.startswith("admin_users_"))
async def admin_users_list(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return

    try:
        page = int(callback.data.split("_")[-1])
    except (ValueError, TypeError):
        page = 1

    all_configs = list(MEMORY_DB["config"].items())

    # Проверяем аккаунты независимо друг от друга.
    # Если один юзер заблокировал бота, удалил юзербота или его сессия отозвана,
    # это больше не ломает построение всего списка.
    validation_semaphore = asyncio.Semaphore(5)
    validation_tasks = [
        admin_validate_session(int(uid), cfg, validation_semaphore)
        for uid, cfg in all_configs
        if cfg.get("logged_in", False)
    ]
    validation_results = await asyncio.gather(*validation_tasks, return_exceptions=True)

    active_configs = []
    result_index = 0
    for uid, cfg in all_configs:
        if not cfg.get("logged_in", False):
            continue

        result = validation_results[result_index]
        result_index += 1

        if result is True:
            active_configs.append((uid, cfg))
        elif isinstance(result, Exception):
            logging.warning(f"Ошибка проверки активности {uid}: {result}")

    def get_user_score(item):
        uid, cfg = item
        activity = MEMORY_DB["activity"].get(uid, {})
        return sum(activity.values()) if activity else (1 if cfg.get("logged_in") else 0)

    active_configs.sort(key=get_user_score, reverse=True)

    per_page = 5
    total_users = len(active_configs)
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    current_page_users = active_configs[start_idx:end_idx]

    builder = InlineKeyboardBuilder()
    for uid, cfg in current_page_users:
        first_name = cfg.get("first_name") or cfg.get("profile_base_first_name") or "User"
        builder.button(text=f"👤 {first_name} ({uid})", callback_data=f"admin_user_{uid}")

    builder.adjust(1)

    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_users_{page-1}"))

    nav_buttons.append(types.InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data="ignore"))

    if page < total_pages:
        nav_buttons.append(types.InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_users_{page+1}"))

    builder.row(*nav_buttons)
    builder.button(text="⬅️ В админ меню", callback_data="admin_users_back")

    await edit_or_send(callback.from_user.id, "Активные пользователи:", reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_user_"))
async def admin_user_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    target_uid = callback.data.split("_")[-1]

    cfg = MEMORY_DB["config"].get(target_uid) or db_get_data("config", target_uid) or {}
    first_name = cfg.get("first_name") or cfg.get("profile_base_first_name") or "Qwitty"
    username = cfg.get("username", "N/A")
    username_str = f"@{username}" if username != "N/A" else "Отсутствует"
    phone = cfg.get("phone", "Не указан")

    devices_str = "Неизвестно"
    target_state = get_user_state(int(target_uid))
    client = target_state.get("client")
    if client and client.is_connected:
        try:
            auths = await client.invoke(functions.account.GetAuthorizations())
            authorizations = getattr(auths, "authorizations", []) or []
            device_names = []
            for auth in authorizations:
                dev = getattr(auth, "device_model", "") or getattr(auth, "model", "")
                if dev and dev not in device_names:
                    device_names.append(dev)
            if device_names:
                devices_str = ", ".join(device_names)
            else:
                devices_str = "Не найдено"
        except Exception as e:
            logging.error(f"Ошибка получения устройств: {e}")
            devices_str = "Ошибка получения"

    timezone_offset = int(cfg.get("timezone_offset", 5) or 5)
    timezone_name = TIMEZONE_NAMES.get(timezone_offset, f"UTC{timezone_offset:+d}")
    time_status = get_text(callback.from_user.id, "status_on") if cfg.get("time_nick_active", False) else get_text(callback.from_user.id, "status_off")
    autoresponder_status = get_text(callback.from_user.id, "status_on") if cfg.get("autoresponder_active", False) else get_text(callback.from_user.id, "status_off")
    autoresponder_greeting = cfg.get("autoresponder_greeting", get_text(int(target_uid), "msg_autoresp_default"))

    text = (
        f"Никнейм: {first_name}\n"
        f"Юзернейм: {username_str}\n"
        f"Номер: {phone}\n"
        f"Устройства: {devices_str}\n\n"
        f"Время в профиль: {time_status}\n"
        f"{timezone_name}\n\n"
        f"Автоответчик: {autoresponder_status}\n"
        f"{autoresponder_greeting}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data="admin_users_1")
    builder.adjust(1)

    await edit_or_send(callback.from_user.id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

                                                              

                                                                     

async def handle_ping(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 HTTP сервер запущен на порту {port}")

async def main():
    await start_web_server()

                                                                 
                                                   
    await sync_world_clock(force=True)
    asyncio.create_task(ntp_sync_loop())

    await restore_saved_sessions()
    logging.info("🚀 Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    loop.run_until_complete(main())
