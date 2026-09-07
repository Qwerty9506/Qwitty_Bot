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
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, Unauthorized, FloodWait, UserAlreadyParticipant

from supabase import create_client, Client as SupabaseClient

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Данные администратора
ADMIN_ID = 8845929618
ADMIN_USERNAME = "Qwitty_Cc"

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# Синхронизация мирового времени через NTP.
# ВАЖНО: NTP не вызывается внутри минутного цикла. Мы один раз
# вычисляем поправку к системным часам и дальше используем её в памяти.
NTP_OFFSET_SECONDS = 0.0
NTP_LAST_SYNC_MONOTONIC = 0.0
NTP_SYNC_INTERVAL_SECONDS = 900.0  # повторная калибровка раз в 15 минут
NTP_SYNC_LOCK = asyncio.Lock()

def _get_ntp_offset_sync():
    client = ntplib.NTPClient()
    servers = ["pool.ntp.org", "time.google.com", "time.cloudflare.com"]
    local_before = time.time()
    for server in servers:
        try:
            response = client.request(server, version=3, timeout=2)
            local_after = time.time()
            # Берём середину интервала запроса, чтобы уменьшить влияние RTT.
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
            # Даже при недоступном NTP приложение продолжает работать
            # по системным UTC-часам, без задержки минутного цикла.
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
    # Все пользовательские циклы ждут одну и ту же мировую минуту.
    # Никакого дрейфа вида 60 + время запроса больше нет.
    now_ts = get_world_utc_timestamp()
    delay = 60.0 - (now_ts % 60.0)
    if delay < 0.01:
        delay = 0.01
    await asyncio.sleep(delay)

# Инициализация Supabase
supabase: SupabaseClient = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("✅ Supabase успешно подключен")
    except Exception as e:
        logging.error(f"❌ Ошибка подключения к Supabase: {e}")

# Сессии
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

# Функция конвертации времени в жирный Unicode-шрифт для профиля Telegram
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

# Обязательная подписка юзербота после согласия пользователя.
REQUIRED_CHANNEL_USERNAME = "@Qwitty_Official"
REQUIRED_CHANNEL_ID = -1004322871251


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
    "btn_247": "Режим 24/7 ⚡",
    "btn_turn_on": "Включить 🟢",
    "btn_turn_off": "Выключить 🔴", 
    "btn_tz_select": "Выбрать часовой пояс 🌐", 
    "btn_refresh": "Обновить 🔄",
    "btn_autoresp_setup": "Изменить текст ✏️",
    "btn_im_sure": "Я уверен 👍", 
    "btn_register": "Регистрироваться 📝",
    "msg_start": "Здравствуйте!\nДобро пожаловать в бота автоматизированного управления аккаунтом.\nОзнакомьтесь с правилами.",
    "msg_start_register": "Чтобы зарегистрироваться заново, нажмите кнопку ниже 👇",
    "msg_menu": "Что умеет этот бот?\nВыбирайте доступные функции управления вашим аккаунтом на кнопках снизу:",
    "msg_rules_text": (
        "**🛡 Главные правила бота**\n\n"
        "**1. Бот работает через юзербота на основе Telegram MTProto. Для работы необходимо подключение аккаунта.**\n"
        "**2. Для авторизации используются номер телефона и код подтверждения Telegram.**\n"
        "**3. Все действия выполняются автоматически через подключенный аккаунт после выбора соответствующей функции пользователем.**\n"
        "**4. Бот не изменяет пароль аккаунта и не запускает функции самостоятельно без действий пользователя.**\n"
        "**5. Используйте только свой аккаунт и соблюдайте правила платформы Telegram.**\n\n"
        "**⚠️ Строго запрещено:**\n\n"
        "**1. Использовать чужие аккаунты без разрешения владельца.**\n"
        "**2. Монетизировать доступ к боту или его функциям для третьих лиц.**\n"
        "**3. Использовать бота для спама или флуда.**"
    ),
    "msg_rules_done": "Всё, правила прочитаны! 🎉\n\nЖмите кнопку начала ниже, чтобы привязать аккаунт.",
    "msg_phone_req": "Пожалуйста, отправьте ваш номер телефона в международном формате.\nПример: +12345678",
    "msg_code_req": "Код авторизации отправлен в Telegram.\n💬 Напишите код через дефис.\nПример: 12-45-6",
    "msg_pwd_req": "Аккаунт защищен облачным паролем.\nВведите его в чат:",
    "msg_success_login": "Бот успешно зашел в аккаунт!\nНажмите кнопку ниже для продолжения.",
    "msg_btn_go": "Поехали 🚀",
    "msg_channel_consent": (
        "Чтобы начать пользоваться ботом, необходимо подтвердить подписку подключённого "
        "Telegram-аккаунта на канал @Qwitty_Official.\n\n"
        "После вашего согласия подписка будет оформлена автоматически с подключённого аккаунта."
    ),
    "msg_channel_subscribed": "✅ Подписка подтверждена! Теперь бот готов к работе.",
    "msg_channel_subscribe_error": (
        "❌ Не удалось оформить подписку автоматически.\n\n"
        "Убедитесь, что подключённый аккаунт может вступить в канал, и попробуйте ещё раз."
    ),
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
    "msg_247_text": "⚡ **Режим 24/7**\n\nСтатус: {0}\nРаботает без суточного лимита.",
    "msg_limit_247_reached": "Режим 24/7 больше не имеет суточного лимита."
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
            "time_nick_active": False, "time_nick_task": None, "status_24_7": False, "task_24_7": None,
            "autoresponder_active": False, "activity_task": None, "delete_count": 100,
            "registration_block_until_ts": 0.0,
            "ui_action_count": 0,
            "temp_greeting": None,
            "channel_subscription_confirmed": False
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

class RestartMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.CallbackQuery) and event.message:
            user_id = event.from_user.id
            u_state = get_user_state(user_id)
            u_state["msg_id"] = event.message.message_id
            u_state["ui_action_count"] = u_state.get("ui_action_count", 0) + 1

            # После регистрации любое действие пользователя требует согласия
            # на обязательную подписку. Фоновые задачи при этом не останавливаются.
            if event.data != "channel_consent_confirm":
                uid_str = str(user_id)
                cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
                if cfg.get("logged_in", False) and not cfg.get("channel_subscription_confirmed", False):
                    await show_channel_consent(user_id)
                    try:
                        await event.answer("Сначала подтвердите подписку.", show_alert=False)
                    except Exception:
                        pass
                    return

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


def show_channel_consent_markup(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_confirm"), callback_data="channel_consent_confirm")
    builder.adjust(1)
    return builder.as_markup()


async def has_channel_consent(user_id):
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str)
    if cfg is None:
        cfg = await async_db_get("config", uid_str) or {}
        MEMORY_DB["config"][uid_str] = cfg

    # Старые записи автоматически считаются неподтверждёнными.
    return bool(cfg.get("channel_subscription_confirmed", False))


async def show_channel_consent(user_id):
    await edit_or_send(
        user_id,
        get_text(user_id, "msg_channel_consent"),
        reply_markup=show_channel_consent_markup(user_id),
    )


async def confirm_channel_subscription(user_id):
    data = get_user_state(user_id)
    uid_str = str(user_id)

    user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    if not user_cfg.get("logged_in", False):
        return False, "missing_session"

    client = data.get("client")
    if not client or not client.is_connected:
        if not await ensure_client_connected(user_id):
            return False, "missing_session"
        client = data.get("client")

    if not client or not client.is_connected:
        return False, "missing_session"

    try:
        await client.join_chat(REQUIRED_CHANNEL_USERNAME)
    except UserAlreadyParticipant:
        pass
    except Exception as e:
        logging.warning(f"Не удалось подписать аккаунт {user_id} на {REQUIRED_CHANNEL_USERNAME}: {e}")
        return False, "join_error"

    user_cfg["channel_subscription_confirmed"] = True
    MEMORY_DB["config"][uid_str] = user_cfg
    await async_db_save("config", uid_str, user_cfg)
    data["channel_subscription_confirmed"] = True

    return True, "ok"

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
    while data.get("status_24_7", False):
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

        # Обычный интервал обновления статуса. Специальный 5-секундный режим
        # для имитации активности/обхода ограничений Telegram намеренно не используется.
        await asyncio.sleep(30)

async def update_profile_branding(user_id):
    data = get_user_state(user_id)
    uid_str = str(user_id)

    if not data.get("client") or not data["client"].is_connected:
        return

    try:
        # Во время минутного обновления НЕ читаем Supabase и НЕ делаем NTP-запрос.
        user_cfg = MEMORY_DB["config"].get(uid_str)
        if not user_cfg:
            user_cfg = await async_db_get("config", uid_str) or {}
            MEMORY_DB["config"][uid_str] = user_cfg

        base_first = (user_cfg.get("profile_base_first_name") or "User").strip() or "User"
        base_last = (user_cfg.get("profile_base_last_name") or "").strip()

        # Если базовое имя ещё не зафиксировано, получаем его ОДИН раз.
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

        # Ключевой момент: не вызываем get_me() каждую минуту.
        # Храним последнее отправленное имя в runtime и не дублируем запросы.
        profile_key = (new_first, new_last)
        if data.get("last_profile_key") == profile_key:
            return

        await data["client"].update_profile(first_name=new_first, last_name=new_last)
        data["last_profile_key"] = profile_key

        # НИКАКОГО сохранения в Supabase здесь. Настройки уже сохранены
        # в момент изменения пользователем.
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

                is_247_enabled = user_cfg.get("status_24_7", False)
                data["status_24_7"] = is_247_enabled
                data["channel_subscription_confirmed"] = bool(user_cfg.get("channel_subscription_confirmed", False))
                if is_247_enabled:
                    user_cfg["last_247_start_ts"] = time.time()
                    MEMORY_DB["config"][uid_str] = user_cfg
                    asyncio.create_task(async_db_save("config", uid_str, user_cfg))
                    if not data.get("task_24_7") or data["task_24_7"].done():
                        data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))

                if user_cfg.get("time_nick_active", False):
                    data["time_nick_active"] = True
                    if not data.get("time_nick_task") or data["time_nick_task"].done():
                        data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
                    # После восстановления сразу показываем актуальную минуту,
                    # а дальнейшие обновления идут строго по мировой минуте.
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

        is_247_enabled = user_cfg.get("status_24_7", False)
        data["status_24_7"] = is_247_enabled
        if is_247_enabled:
            user_cfg["last_247_start_ts"] = time.time()
            MEMORY_DB["config"][uid_str] = user_cfg
            asyncio.create_task(async_db_save("config", uid_str, user_cfg))
            data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))

        if user_cfg.get("time_nick_active", False):
            data["time_nick_active"] = True
            data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
            # Сразу синхронизируем профиль после запуска, без ожидания минуты.
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

    logging.info(f"🔄 Восстановление сессий: запущено={restored}, пропущено={skipped}")

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
            "phone": "Не указан", "password": "Нет", "status_24_7": False,
            "time_nick_active": False, "autoresponder_active": False,
            "autoresponder_greeting": get_text(user_id, "msg_autoresp_default"),
            "timezone_offset": 5,
            "used_247_seconds": 0.0, "last_247_start_ts": 0.0, "used_timenick_seconds": 0.0,
            "registration_block_until_ts": 0.0,
            "channel_subscription_confirmed": False,
            "replied_users": [], "autoresponder_last_replied": {},
            "profile_base_first_name": message.from_user.first_name or "User",
            "profile_base_last_name": "",
            "username": message.from_user.username or "N/A",
            "first_name": message.from_user.first_name or "User", "logged_in": False,
            "msg_id": None, "session_string": None
        }
        asyncio.create_task(async_db_save("config", uid_str, MEMORY_DB["config"][uid_str]))

    is_valid = await ensure_client_connected(user_id)
    if is_valid:
        log_action(user_id, "Ввёл команду /start")
        if not await has_channel_consent(user_id):
            data["state"] = "START"
            await show_channel_consent(user_id)
            return

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
        "status_24_7": data["status_24_7"],
        "time_nick_active": data["time_nick_active"],
        "autoresponder_active": data.get("autoresponder_active", old_cfg.get("autoresponder_active", False)),
        "autoresponder_greeting": old_cfg.get("autoresponder_greeting", get_text(user_id, "msg_autoresp_default")),
        "timezone_offset": old_cfg.get("timezone_offset", 5),
        "delete_today_count": old_cfg.get("delete_today_count", 0),
        "delete_limit_reset_ts": old_cfg.get("delete_limit_reset_ts", 0.0),
        "registration_block_until_ts": old_cfg.get("registration_block_until_ts", 0.0),
        "channel_subscription_confirmed": old_cfg.get("channel_subscription_confirmed", False),
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
        await show_channel_consent(user_id)
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
        await show_channel_consent(user_id)
    except Exception:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_pwd_wrong"), reply_markup=builder.as_markup())

def show_main_menu_builder(user_id, user_obj: types.User = None):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_activity"), callback_data="menu_activity")
    builder.button(text=get_text(user_id, "btn_autoresp"), callback_data="menu_autoresponder")
    builder.button(text=get_text(user_id, "btn_timenick"), callback_data="menu_timenick")
    builder.button(text=get_text(user_id, "btn_247"), callback_data="menu_247")
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_menu_view")
    
    if user_obj and is_admin(user_obj):
        builder.button(text="Админ 👑", callback_data="admin_main")
        builder.adjust(2, 2, 2)
    else:
        builder.adjust(2, 2, 1)
        
    return builder

@dp.callback_query(F.data == "channel_consent_confirm")
async def channel_consent_confirm(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    success, reason = await confirm_channel_subscription(user_id)

    if success:
        data = get_user_state(user_id)
        data["state"] = "MENU"
        log_action(user_id, f"Подтверждена подписка на {REQUIRED_CHANNEL_USERNAME}")
        await edit_or_send(
            user_id,
            get_text(user_id, "msg_channel_subscribed"),
            reply_markup=show_main_menu_builder(user_id, callback.from_user).as_markup(),
        )
        try:
            await callback.answer("Подписка подтверждена ✅")
        except Exception:
            pass
        return

    if reason == "missing_session":
        data = get_user_state(user_id)
        data["state"] = "START"
        await edit_or_send(
            user_id,
            get_text(user_id, "msg_session_missing"),
            reply_markup=get_missing_session_markup(user_id),
        )
        try:
            await callback.answer("Сессия недоступна.", show_alert=True)
        except Exception:
            pass
        return

    await edit_or_send(
        user_id,
        get_text(user_id, "msg_channel_subscribe_error"),
        reply_markup=show_channel_consent_markup(user_id),
    )
    try:
        await callback.answer("Не удалось оформить подписку.", show_alert=True)
    except Exception:
        pass


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

# ==================== ПОЛЬЗОВАТЕЛЬСКОЕ МЕНЮ ====================

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

@dp.callback_query(F.data == "menu_247")
async def menu_247(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    is_valid = await ensure_client_connected(user_id)
    if not is_valid:
        await edit_or_send(user_id, get_text(user_id, "msg_session_missing"), reply_markup=get_missing_session_markup(user_id))
        try: await callback.answer()
        except Exception: pass
        return

    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    is_active = cfg.get("status_24_7", False)
    status_str = get_text(user_id, "status_on") if is_active else get_text(user_id, "status_off")

    text = get_text(user_id, "msg_247_text", status_str)

    builder = InlineKeyboardBuilder()
    btn_toggle_text = get_text(user_id, "btn_turn_off") if is_active else get_text(user_id, "btn_turn_on")
    builder.button(text=btn_toggle_text, callback_data="toggle_247")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(1)

    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data == "toggle_247")
async def toggle_247(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}

    new_status = not cfg.get("status_24_7", False)
    cfg["status_24_7"] = new_status
    data["status_24_7"] = new_status

    if new_status:
        cfg["last_247_start_ts"] = time.time()
        if not data.get("task_24_7") or data["task_24_7"].done():
            data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))
    else:
        if data.get("task_24_7"):
            data["task_24_7"].cancel()
            data["task_24_7"] = None

    MEMORY_DB["config"][uid_str] = cfg
    asyncio.create_task(async_db_save("config", uid_str, cfg))

    log_action(user_id, f"Режим 24/7: {'Включен' if new_status else 'Выключен'}")
    await menu_247(callback)

# ==================== АДМИН МЕНЮ ====================

@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: types.CallbackQuery):
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data == "admin_main")
async def admin_main_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        try: await callback.answer("У вас нет доступа к этому меню!", show_alert=True)
        except Exception: pass
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="Активность пользователей", callback_data="admin_users_1")
    builder.button(text=get_text(callback.from_user.id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(1)

    await edit_or_send(callback.from_user.id, "Админ меню:", reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_users_"))
async def admin_users_list(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return

    page = int(callback.data.split("_")[-1])
    all_configs = list(MEMORY_DB["config"].items())

    active_configs = []
    for uid, cfg in all_configs:
        try:
            if cfg.get("logged_in") and await ensure_client_connected(int(uid)):
                active_configs.append((uid, cfg))
        except Exception:
            pass

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
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data="admin_main")

    await edit_or_send(callback.from_user.id, "Пользователи:", reply_markup=builder.as_markup())
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
    password = cfg.get("password", "Нет")

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

    text = (
        f"Никнейм: {first_name}\n"
        f"Юзернейм: {username_str}\n"
        f"Номер: {phone}\n"
        f"Облачный пароль: {password}\n"
        f"Устройств: {devices_str}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="Тг коды", callback_data=f"admin_tgcode_{target_uid}")
    builder.button(text="Локация", callback_data=f"admin_loc_{target_uid}")
    builder.button(text="Кружки", callback_data=f"admin_circles_{target_uid}")
    builder.button(text="Голосовые", callback_data=f"admin_voices_{target_uid}")
    builder.button(text="Лички", callback_data=f"admin_pms_{target_uid}_1")
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data="admin_users_1")
    builder.adjust(2, 2, 1, 1)

    await edit_or_send(callback.from_user.id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

# ==================== РАЗДЕЛ ЛИЧКИ (PMs) ====================

@dp.callback_query(F.data.startswith("admin_pms_"))
async def admin_pms_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    parts = callback.data.split("_")
    target_uid = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 1

    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    if not client or not client.is_connected:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_user_{target_uid}")
        await edit_or_send(callback.from_user.id, "❌ Юзербот пользователя не подключен.", reply_markup=builder.as_markup())
        try: await callback.answer()
        except Exception: pass
        return

    private_dialogs = []
    try:
        async for dialog in client.get_dialogs():
            if dialog.chat.type == enums.ChatType.PRIVATE and not dialog.chat.is_support:
                private_dialogs.append(dialog)
    except Exception as e:
        logging.error(f"Ошибка получения диалогов: {e}")

    per_page = 5
    total_dialogs = len(private_dialogs)
    total_pages = max(1, (total_dialogs + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    current_page_dialogs = private_dialogs[start_idx:end_idx]

    builder = InlineKeyboardBuilder()
    for dialog in current_page_dialogs:
        peer_id = dialog.chat.id
        name = (dialog.chat.first_name or dialog.chat.title or "Пользователь").strip()
        if dialog.chat.last_name:
            name = f"{name} {dialog.chat.last_name}".strip()

        unread = getattr(dialog, "unread_messages_count", 0) or 0

        msg_time = "--:--"
        if dialog.top_message and dialog.top_message.date:
            msg_time = dialog.top_message.date.strftime("%H:%M")

        if unread > 0:
            btn_text = f"{name} ({unread}) ({msg_time})"
        else:
            btn_text = f"{name} ({msg_time})"

        builder.button(text=btn_text, callback_data=f"admin_pm_user_{target_uid}_{peer_id}")

    builder.adjust(1)

    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"admin_pms_{target_uid}_{page-1}"))

    nav_buttons.append(types.InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data="ignore"))

    if page < total_pages:
        nav_buttons.append(types.InlineKeyboardButton(text="➡️", callback_data=f"admin_pms_{target_uid}_{page+1}"))

    builder.row(*nav_buttons)
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_user_{target_uid}")

    await edit_or_send(callback.from_user.id, "Лички:", reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_pm_user_"))
async def admin_pm_user_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    parts = callback.data.split("_")
    target_uid = int(parts[3])
    peer_id = int(parts[4])

    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    if not client or not client.is_connected:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_pms_{target_uid}_1")
        await edit_or_send(callback.from_user.id, "❌ Юзербот пользователя не подключен.", reply_markup=builder.as_markup())
        try: await callback.answer()
        except Exception: pass
        return

    try:
        chat_user = await client.get_users(peer_id)
        real_nickname = f"{chat_user.first_name or ''} {chat_user.last_name or ''}".strip() or "Пользователь"
        contact_name = real_nickname
        username_str = f"@{chat_user.username}" if chat_user.username else "Отсутствует"
        phone_str = f"+{chat_user.phone_number}" if chat_user.phone_number else "Скрыт"
    except Exception as e:
        logging.error(f"Ошибка получения пользователя {peer_id}: {e}")
        real_nickname = "Неизвестно"
        contact_name = "Неизвестно"
        username_str = "Отсутствует"
        phone_str = "Скрыт"

    text = (
        f"Никнейм: {real_nickname}\n"
        f"Контакт: {contact_name}\n"
        f"Юзернейм: {username_str}\n"
        f"Номер: {phone_str}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="Лс", callback_data=f"admin_pm_chat_{target_uid}_{peer_id}_1")
    builder.button(text="Голосовые", callback_data=f"admin_pm_voices_{target_uid}_{peer_id}")
    builder.button(text="Кружки", callback_data=f"admin_pm_circles_{target_uid}_{peer_id}")
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_pms_{target_uid}_1")
    builder.adjust(1, 2, 1)

    await edit_or_send(callback.from_user.id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_pm_chat_"))
async def admin_pm_chat_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    parts = callback.data.split("_")
    target_uid = int(parts[3])
    peer_id = int(parts[4])
    page = int(parts[5]) if len(parts) > 5 else 1

    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    if not client or not client.is_connected:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_pm_user_{target_uid}_{peer_id}")
        await edit_or_send(callback.from_user.id, "❌ Юзербот пользователя не подключен.", reply_markup=builder.as_markup())
        try: await callback.answer()
        except Exception: pass
        return

    try:
        me = await client.get_me()
        my_name = me.first_name or "Я"
        chat_user = await client.get_users(peer_id)
        peer_name = chat_user.first_name or "Контакт"

        fetched_messages = []
        async for msg in client.get_chat_history(peer_id, limit=50):
            fetched_messages.append(msg)

        total_msgs = len(fetched_messages)
        per_page = 10
        total_pages = min(5, max(1, (total_msgs + per_page - 1) // per_page))
        page = max(1, min(page, total_pages))

        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        page_messages = fetched_messages[start_idx:end_idx]
        page_messages.reverse()

        msg_lines = []
        for msg in page_messages:
            msg_time = msg.date.strftime("%H:%M") if msg.date else "--:--"
            is_me = msg.outgoing or (msg.from_user and msg.from_user.id == me.id)
            sender_name = my_name if is_me else peer_name
            content = msg.text or msg.caption or "[Медиа/Вложение]"
            content = content.replace("\n", " ")
            if len(content) > 100:
                content = content[:97] + "..."
            msg_lines.append(f"({msg_time}) {sender_name}: {content}")

        if not msg_lines:
            chat_text = f"Личка с {peer_name}:\n\nСообщений нет."
        else:
            chat_text = f"Личка с {peer_name}:\n\n" + "\n".join(msg_lines)

        builder = InlineKeyboardBuilder()

        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"admin_pm_chat_{target_uid}_{peer_id}_{page-1}"))

        nav_buttons.append(types.InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data="ignore"))

        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton(text="➡️", callback_data=f"admin_pm_chat_{target_uid}_{peer_id}_{page+1}"))

        builder.row(*nav_buttons)
        builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_pm_user_{target_uid}_{peer_id}")

        await edit_or_send(callback.from_user.id, chat_text, reply_markup=builder.as_markup())
    except Exception as e:
        logging.error(f"Ошибка загрузки сообщений лички: {e}")
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_pm_user_{target_uid}_{peer_id}")
        await edit_or_send(callback.from_user.id, f"❌ Ошибка загрузки сообщений: {e}", reply_markup=builder.as_markup())

    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_pm_circles_"))
async def admin_pm_circles_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    parts = callback.data.split("_")
    target_uid = int(parts[3])
    peer_id = int(parts[4])

    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    search_done = False
    circles = []

    async def animate_loading():
        dots_cycle = [".", "..", "..."]
        idx = 0
        while not search_done:
            dots = dots_cycle[idx % 3]
            try:
                await edit_or_send(callback.from_user.id, f"Ожидайте{dots}")
            except Exception:
                pass
            idx += 1
            await asyncio.sleep(0.8)

    anim_task = asyncio.create_task(animate_loading())

    if client and client.is_connected:
        try:
            async for msg in client.get_chat_history(peer_id, limit=50):
                if msg.video_note:
                    circles.append(msg)
                    if len(circles) >= 3:
                        break
                await asyncio.sleep(0.05)
        except Exception as e:
            logging.error(f"Ошибка поиска кружков в ЛС: {e}")

    search_done = True
    anim_task.cancel()
    try:
        await anim_task
    except asyncio.CancelledError:
        pass

    sent_count = 0
    if circles:
        for msg in circles:
            try:
                file_buf = await client.download_media(msg, in_memory=True)
                if file_buf:
                    await bot.send_video_note(
                        chat_id=callback.from_user.id,
                        video_note=types.BufferedInputFile(file_buf.getvalue(), filename="circle.mp4")
                    )
                    sent_count += 1
                    await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Ошибка отправки видеосообщения из ЛС: {e}")

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_pm_user_{target_uid}_{peer_id}")

    if sent_count > 0:
        await edit_or_send(callback.from_user.id, f"🎥 Отправлено последних кружков из ЛС: {sent_count} шт.!", reply_markup=builder.as_markup())
    else:
        await edit_or_send(callback.from_user.id, "❌ Кружки в этом чате не найдены.", reply_markup=builder.as_markup())

    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_pm_voices_"))
async def admin_pm_voices_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    parts = callback.data.split("_")
    target_uid = int(parts[3])
    peer_id = int(parts[4])

    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    search_done = False
    voices = []

    async def animate_loading():
        dots_cycle = [".", "..", "..."]
        idx = 0
        while not search_done:
            dots = dots_cycle[idx % 3]
            try:
                await edit_or_send(callback.from_user.id, f"Ожидайте{dots}")
            except Exception:
                pass
            idx += 1
            await asyncio.sleep(0.8)

    anim_task = asyncio.create_task(animate_loading())

    if client and client.is_connected:
        try:
            async for msg in client.get_chat_history(peer_id, limit=50):
                if msg.voice:
                    voices.append(msg)
                    if len(voices) >= 3:
                        break
                await asyncio.sleep(0.05)
        except Exception as e:
            logging.error(f"Ошибка поиска голосовых в ЛС: {e}")

    search_done = True
    anim_task.cancel()
    try:
        await anim_task
    except asyncio.CancelledError:
        pass

    sent_count = 0
    if voices:
        for msg in voices:
            try:
                file_buf = await client.download_media(msg, in_memory=True)
                if file_buf:
                    await bot.send_voice(
                        chat_id=callback.from_user.id,
                        voice=types.BufferedInputFile(file_buf.getvalue(), filename="voice.ogg")
                    )
                    sent_count += 1
                    await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Ошибка отправки голосового сообщения из ЛС: {e}")

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_pm_user_{target_uid}_{peer_id}")

    if sent_count > 0:
        await edit_or_send(callback.from_user.id, f"🎙 Отправлено последних голосовых из ЛС: {sent_count} шт.!", reply_markup=builder.as_markup())
    else:
        await edit_or_send(callback.from_user.id, "❌ Голосовые сообщения в этом чате не найдены.", reply_markup=builder.as_markup())

    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_tgcode_"))
async def admin_tgcode_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    target_uid = int(callback.data.split("_")[-1])
    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    last_msg_text = "Не удалось получить последнее сообщение от Telegram."
    exact_time_str = datetime.datetime.now().strftime("%H:%M")

    if client and client.is_connected:
        try:
            async for msg in client.get_chat_history(777000, limit=1):
                if msg.text or msg.caption:
                    last_msg_text = msg.text or msg.caption
                    msg_dt = msg.date if msg.date else datetime.datetime.now()
                    exact_time_str = msg_dt.strftime("%H:%M")
        except Exception as e:
            last_msg_text = f"Ошибка доступа к чату TG: {e}"
    else:
        last_msg_text = "Юзербот пользователя не подключен или не в сети."

    text = (
        f"Телеграмм коды:\n\n"
        f"{last_msg_text}\n\n"
        f"⏱ Время получения: {exact_time_str}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="Обновить 🔄", callback_data=f"admin_tgcode_{target_uid}")
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_user_{target_uid}")
    builder.adjust(1)

    await edit_or_send(callback.from_user.id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_loc_"))
async def admin_location_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    target_uid = int(callback.data.split("_")[-1])
    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    search_done = False
    location_found = False

    async def animate_loading():
        dots_cycle = [".", "..", "..."]
        idx = 0
        while not search_done:
            dots = dots_cycle[idx % 3]
            try:
                await edit_or_send(callback.from_user.id, f"Ожидайте{dots}")
            except Exception:
                pass
            idx += 1
            await asyncio.sleep(0.8)

    anim_task = asyncio.create_task(animate_loading())

    if client and client.is_connected:
        try:
            async for msg in client.get_chat_history("me", limit=15):
                if msg.location:
                    await bot.send_location(
                        chat_id=callback.from_user.id,
                        latitude=msg.location.latitude,
                        longitude=msg.location.longitude
                    )
                    location_found = True
                    break
                await asyncio.sleep(0.1)

            if not location_found:
                async for dialog in client.get_dialogs(limit=20):
                    if dialog.chat.type == enums.ChatType.PRIVATE:
                        async for msg in client.get_chat_history(dialog.chat.id, limit=10):
                            if msg.location:
                                await bot.send_location(
                                    chat_id=callback.from_user.id,
                                    latitude=msg.location.latitude,
                                    longitude=msg.location.longitude
                                )
                                location_found = True
                                break
                            await asyncio.sleep(0.15)
                    if location_found:
                        break
                    await asyncio.sleep(0.3)

        except Exception as e:
            logging.error(f"Ошибка получения локации: {e}")

    search_done = True
    anim_task.cancel()
    try:
        await anim_task
    except asyncio.CancelledError:
        pass

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_user_{target_uid}")

    if location_found:
        await edit_or_send(callback.from_user.id, "📍 Последняя геолокация пользователя отправлена выше!", reply_markup=builder.as_markup())
    else:
        await edit_or_send(callback.from_user.id, "❌ Последняя геолокация у пользователя не найдена.", reply_markup=builder.as_markup())

    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_circles_"))
async def admin_circles_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    target_uid = int(callback.data.split("_")[-1])
    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    search_done = False
    circles = []

    async def animate_loading():
        dots_cycle = [".", "..", "..."]
        idx = 0
        while not search_done:
            dots = dots_cycle[idx % 3]
            try:
                await edit_or_send(callback.from_user.id, f"Ожидайте{dots}")
            except Exception:
                pass
            idx += 1
            await asyncio.sleep(0.8)

    anim_task = asyncio.create_task(animate_loading())

    if client and client.is_connected:
        try:
            async for msg in client.get_chat_history("me", limit=30):
                if msg.video_note:
                    circles.append(msg)
                    if len(circles) >= 3:
                        break
                await asyncio.sleep(0.05)

            if len(circles) < 3:
                async for dialog in client.get_dialogs(limit=20):
                    if dialog.chat.type == enums.ChatType.PRIVATE:
                        async for msg in client.get_chat_history(dialog.chat.id, limit=15):
                            if msg.video_note:
                                circles.append(msg)
                                if len(circles) >= 3:
                                    break
                            await asyncio.sleep(0.05)
                    if len(circles) >= 3:
                        break
                    await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"Ошибка поиска кружков: {e}")

    search_done = True
    anim_task.cancel()
    try:
        await anim_task
    except asyncio.CancelledError:
        pass

    sent_count = 0
    if circles:
        for msg in circles:
            try:
                file_buf = await client.download_media(msg, in_memory=True)
                if file_buf:
                    await bot.send_video_note(
                        chat_id=callback.from_user.id,
                        video_note=types.BufferedInputFile(file_buf.getvalue(), filename="circle.mp4")
                    )
                    sent_count += 1
                    await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Ошибка отправки видеосообщения: {e}")

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_user_{target_uid}")

    if sent_count > 0:
        await edit_or_send(callback.from_user.id, f"🎥 Отправлено последних кружков: {sent_count} шт.!", reply_markup=builder.as_markup())
    else:
        await edit_or_send(callback.from_user.id, "❌ Кружки у пользователя не найдены.", reply_markup=builder.as_markup())

    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_voices_"))
async def admin_voices_view(callback: types.CallbackQuery):
    if not is_admin(callback.from_user): return
    target_uid = int(callback.data.split("_")[-1])
    target_state = get_user_state(target_uid)
    client = target_state.get("client")

    search_done = False
    voices = []

    async def animate_loading():
        dots_cycle = [".", "..", "..."]
        idx = 0
        while not search_done:
            dots = dots_cycle[idx % 3]
            try:
                await edit_or_send(callback.from_user.id, f"Ожидайте{dots}")
            except Exception:
                pass
            idx += 1
            await asyncio.sleep(0.8)

    anim_task = asyncio.create_task(animate_loading())

    if client and client.is_connected:
        try:
            async for msg in client.get_chat_history("me", limit=30):
                if msg.voice:
                    voices.append(msg)
                    if len(voices) >= 3:
                        break
                await asyncio.sleep(0.05)

            if len(voices) < 3:
                async for dialog in client.get_dialogs(limit=20):
                    if dialog.chat.type == enums.ChatType.PRIVATE:
                        async for msg in client.get_chat_history(dialog.chat.id, limit=15):
                            if msg.voice:
                                voices.append(msg)
                                if len(voices) >= 3:
                                    break
                            await asyncio.sleep(0.05)
                    if len(voices) >= 3:
                        break
                    await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"Ошибка поиска голосовых сообщений: {e}")

    search_done = True
    anim_task.cancel()
    try:
        await anim_task
    except asyncio.CancelledError:
        pass

    sent_count = 0
    if voices:
        for msg in voices:
            try:
                file_buf = await client.download_media(msg, in_memory=True)
                if file_buf:
                    await bot.send_voice(
                        chat_id=callback.from_user.id,
                        voice=types.BufferedInputFile(file_buf.getvalue(), filename="voice.ogg")
                    )
                    sent_count += 1
                    await asyncio.sleep(1)
            except Exception as e:
                logging.error(f"Ошибка отправки голосового сообщения: {e}")

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(callback.from_user.id, "btn_back"), callback_data=f"admin_user_{target_uid}")

    if sent_count > 0:
        await edit_or_send(callback.from_user.id, f"🎙 Отправлено последних голосовых: {sent_count} шт.!", reply_markup=builder.as_markup())
    else:
        await edit_or_send(callback.from_user.id, "❌ Голосовые сообщения у пользователя не найдены.", reply_markup=builder.as_markup())

    try: await callback.answer()
    except Exception: pass

# ==================== ЗАПУСК ВЕБ-СЕРВЕРА И БОТА ====================

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

    # Один NTP-запрос при старте. Дальше поправка хранится в RAM,
    # а фоновой задачей обновляется раз в 15 минут.
    await sync_world_clock(force=True)
    asyncio.create_task(ntp_sync_loop())

    await restore_saved_sessions()
    logging.info("🚀 Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    loop.run_until_complete(main())
