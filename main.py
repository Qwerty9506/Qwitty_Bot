# Qwitty: aiogram 3.x / Pyrogram 2.x / Supabase.
# Required env: BOT_TOKEN, API_ID, API_HASH, SUPABASE_URL, SUPABASE_KEY.
# Existing Supabase tables: config, activity, logs (id unique, data JSON/JSONB).
# New switches are JSON keys; no additional SQL columns are required.
# Presence is Telegram account status, not screen time. Unobserved server
# downtime is not backfilled. Telegram FloodWait/RetryAfter delays are respected.
import copy
import asyncio
import sys
import os
import datetime
import time
import glob
import logging
import re
import random
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
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from pyrogram import Client, enums, filters
from pyrogram.handlers import MessageHandler, RawUpdateHandler
from pyrogram.raw import functions, types as raw_types
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, Unauthorized, FloodWait

from supabase import create_client, Client as SupabaseClient

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

SUPABASE_DB_SIZE_RPC = os.getenv("SUPABASE_DB_SIZE_RPC", "get_database_size_bytes")
SUPABASE_DB_LIMIT_MB = float(os.getenv("SUPABASE_DB_LIMIT_MB", "500") or 500)


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
    # Числовой ID устойчив к смене/удалению username.
    return user is not None and user.id == ADMIN_ID


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
    "btn_server_stats": "Статистика сервера 🖥",
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
    "msg_timenick_text": "Вывод текущего времени в имя профиля.\n\nТекущий статус: {0}\nПредпросмотр: {1}\nСмещение часового пояса: UTC{2}",
    "msg_tz_select": "Выберите ваш часовой пояс🌐",
    "msg_tz_saved": "Часовой пояс изменен на UTC{0}!",
    "msg_autoresp_text": "🤖 **Автоответчик**\n\nСтатус: {1}\nТекст приветствия:\n💬 \"{0}\"",
    "msg_autoresp_req": "Напишите новый текст приветствия в чат ✏️",
    "msg_autoresp_saved": "Приветствие успешно сохранено! 🎉",
    "msg_autoresp_default": "👋 Здравствуйте! Сейчас я не в сети, отвечу позже.",
}

# Имя Telegram — plain text: используются Unicode-цифры, не HTML-курсив.
TIME_STYLES = (
    ("0123456789", "[", "]", ":"),
    ("𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵", "[", "]", ":"),
    ("𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗", "『", "』", ":"),
    ("𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡", "⟦", "⟧", ":"),
    ("𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫", "⌜", "⌟", ":"),
    ("𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿", "〈", "〉", ":"),
    ("０１２３４５６７８９", "", "", "："),
    ("⁰¹²³⁴⁵⁶⁷⁸⁹", "✦ ", " ✦", ":"),
    ("₀₁₂₃₄₅₆₇₈₉", "⌁ ", " ⌁", ":"),
    ("⓪①②③④⑤⑥⑦⑧⑨", "", " ♡", ":"),
    ("𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵", "✧ ", " ✧", ":"),
    ("𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗", "⟨", "⟩", "∶"),
    ("𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡", "⌞", "⌝", ":"),
    ("𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿", "「", "」", ":"),
    ("𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫", "⋆ ", " ⋆", ":"),
    ("0123456789", "⏾ ", "", "∶"),
    ("𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵", "【", "】", ":"),
    ("𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡", "◇ ", " ◇", ":"),
    ("𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗", "꧁", "꧂", ":"),
    ("𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿", "〈 ", " 〉", "∶"),
    ("⁰¹²³⁴⁵⁶⁷⁸⁹", "˚₊‧ ", " ‧₊˚", ":"),
    ('𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵', '༺', '༻', ':'),
    ('𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗', '⫷', '⫸', '∶'),
    ('𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡', '⟪', '⟫', ':'),
    ('𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿', '〔', '〕', '∶'),
    ('𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫', '⦗', '⦘', ':'),
    ('0123456789', '❮', '❯', '∶'),
    ('𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵', '⌜ ', ' ⌝', ':'),
    ('𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗', '⌞ ', ' ⌟', '∶'),
    ('𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡', '✦ ┊', '┊ ✦', ':'),
    ('𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿', '⊹ ', ' ⊹', '∶'),
    ('𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫', '⟡ ', ' ⟡', ':'),
    ('0123456789', '⋄ ⋆ ', ' ⋆ ⋄', '∶'),
    ('𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵', '☾ ', ' ☽', ':'),
    ('𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗', '☽ ⋆ ', ' ⋆ ☾', '∶'),
    ('𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡', '✩ ', ' ✩', ':'),
    ('𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿', '⭒ ', ' ⭒', '∶'),
    ('𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫', '♛ ', ' ♛', ':'),
    ('0123456789', '♠ ', ' ♠', '∶'),
    ('𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵', '❖ ', ' ❖', ':'),
    ('𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗', '⚜ ', ' ⚜', '∶'),
    ('𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡', '∞ ', ' ∞', ':'),
    ('𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿', '⸙ ', ' ⸙', '∶'),
    ('𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫', '༄ ', ' ༄', ':'),
    ('0123456789', 'ミ★ ', ' ★彡', '∶'),
)


def format_profile_time(raw_time, style=1):
    try:
        index = int(style)
        if not 0 <= index < len(TIME_STYLES):
            index = 1
    except (TypeError, ValueError):
        index = 1
    digits, left, right, colon = TIME_STYLES[index]
    value = raw_time.translate(str.maketrans("0123456789:", digits + colon))
    return left + value + right


def profile_names(base_first, base_last, marker):
    first = ((base_first or "User").strip() or "User")[:64]
    last = (base_last or "").strip()
    # Telegram ограничивает каждую часть имени 64 символами.
    if last:
        last = last[:max(0, 63 - len(marker))].rstrip() + " " + marker
    else:
        first = first[:max(0, 63 - len(marker))].rstrip() + " " + marker
    return first, last

PROFILE_TIME_OFFSET_SECONDS = 0

def get_current_styled_profile_preview(base_first, base_last, offset, include_nick=True, include_time=True, style=1):
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
        first, last = profile_names(clean_first, clean_last, format_profile_time(raw_time, style))

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
        if "profile_base_last_name" not in cfg:
            clean_last = re.sub(r"\s*\[[^\]]+\]", "", me.last_name or "").strip()
            cfg["profile_base_last_name"] = clean_last
        MEMORY_DB["config"][uid_str] = cfg
        queue_db_save("config", uid_str, cfg)
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
        logging.error("Supabase read failed (%s): %s", table, type(e).__name__)
        raise RuntimeError("Не удалось загрузить настройки из Supabase; повторите позже.") from e
    return {}

def db_get_all_config():
    if not supabase:
        return []
    try:
        rows = []
        offset = 0
        while True:
            batch = supabase.table("config").select("id, data").order("id").range(offset, offset + 499).execute().data or []
            rows.extend(batch)
            if len(batch) < 500:
                return rows
            offset += 500
    except Exception as e:
        logging.error(f"Error fetching all Supabase configs: {e}")
        return []

def db_save_data(table: str, user_id: str, data: dict):
    if not supabase:
        return False
    try:
        supabase.table(table).upsert({"id": str(user_id), "data": data}).execute()
        return True
    except Exception as e:
        logging.error("Supabase write failed (%s): %s", table, type(e).__name__)
        return False

async def async_db_get(table: str, user_id: str):
    return await asyncio.to_thread(db_get_data, table, str(user_id))

DB_WRITE_LOCKS = {}
DB_DIRTY = set()
DB_TASKS = set()

def queue_db_save(table, uid, data):
    DB_DIRTY.add((table, str(uid)))
    task = asyncio.create_task(async_db_save(table, str(uid), data))
    DB_TASKS.add(task)
    task.add_done_callback(DB_TASKS.discard)

def _record_db_save_result(table: str, user_id: str, ok: bool):
    """Keep config/activity persistence health separate and suppress transient UI flashes."""
    try:
        numeric_uid = int(user_id)
    except (TypeError, ValueError):
        return

    state = USER_DATA.get(numeric_uid)
    if state is None:
        return

    if table == "config":
        if ok:
            state["config_save_failures"] = 0
            state["config_save_failed_since"] = 0.0
            state["save_error"] = False
            state.pop("config_save_error", None)
            return

        failures = int(state.get("config_save_failures", 0) or 0) + 1
        state["config_save_failures"] = failures
        if not state.get("config_save_failed_since"):
            state["config_save_failed_since"] = time.monotonic()

        # A single failed request is often followed immediately by a successful
        # coalesced/retry write. Do not flash a scary warning for that transient case.
        failed_for = time.monotonic() - float(state.get("config_save_failed_since", 0.0) or 0.0)
        persistent = failures >= 3 or failed_for >= 15.0
        state["save_error"] = persistent
        if persistent:
            state["config_save_error"] = True

    elif table == "activity":
        # Activity is intentionally flushed only every ~5 minutes. Its persistence
        # state must never masquerade as a settings/config save error in the main UI.
        state["activity_save_error"] = not ok


async def async_db_save(table: str, user_id: str, data: dict):
    key = (table, str(user_id))
    DB_DIRTY.add(key)
    lock = DB_WRITE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        # Take the latest state AFTER acquiring the lock: queued old snapshots
        # must never overwrite a newer style, switch or session string.
        current = MEMORY_DB.get(table, {}).get(str(user_id), data)
        snapshot = copy.deepcopy(current)
        ok = await asyncio.to_thread(db_save_data, table, str(user_id), snapshot)
        if ok and snapshot == MEMORY_DB.get(table, {}).get(str(user_id), data):
            DB_DIRTY.discard(key)
        _record_db_save_result(table, str(user_id), ok)
        return ok

async def db_retry_loop():
    while True:
        await asyncio.sleep(5)
        for table, uid in list(DB_DIRTY):
            # activity сохраняется своим разнесённым по времени циклом примерно раз в 5 минут.
            # Иначе этот общий retry-loop снова превращал бы редкие записи в запросы каждые 5 секунд.
            if table == "activity":
                continue
            await async_db_save(table, uid, MEMORY_DB[table].get(uid, {}))

async def persist_user_config_now(user_id: int, cfg: dict):
    uid = str(user_id)
    MEMORY_DB["config"][uid] = cfg
    # async_db_save() centrally tracks persistent config failures. Do not overwrite
    # that debounce here with a one-request transient error.
    return await async_db_save("config", uid, cfg)

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
    queue_db_save("logs", uid_str, MEMORY_DB["logs"][uid_str])

def get_user_state(user_id):
    """Возвращает runtime-состояние, гидратируя сохранённые настройки из Supabase."""
    if user_id not in USER_DATA:
        uid_str = str(user_id)
        if uid_str not in MEMORY_DB["config"]:
            MEMORY_DB["config"][uid_str] = db_get_data("config", uid_str) or {}

        cfg = MEMORY_DB["config"].get(uid_str) or {}
        USER_DATA[user_id] = {
            "msg_id": cfg.get("msg_id"),
            "phone": cfg.get("phone"),
            "password": cfg.get("password"),
            "phone_code_hash": None,
            "client": None,
            "state": "MENU" if cfg.get("logged_in", False) else "START",
            "time_nick_active": bool(cfg.get("time_nick_active", False)),
            "time_nick_task": None,
            "autoresponder_active": bool(cfg.get("autoresponder_active", False)),
            "activity_task": None,
            "delete_count": int(cfg.get("delete_today_count", 0) or 0),
            "registration_block_until_ts": float(cfg.get("registration_block_until_ts", 0.0) or 0.0),
            "ui_action_count": 0,
            "ui_refresh_task": None,
            "last_ui_text": None,
            "last_ui_reply_markup": None,
            "last_ui_parse_mode": None,
            "admin_stats_active": False,
            "admin_stats_task": None,
            "temp_greeting": cfg.get("autoresponder_greeting"),
        }
    else:
        # Если конфиг был обновлён из Supabase после создания runtime-состояния,
        # синхронизируем только постоянные пользовательские настройки.
        uid_str = str(user_id)
        cfg = MEMORY_DB["config"].get(uid_str) or {}
        state = USER_DATA[user_id]
        if state.get("client") is None:
            state["phone"] = cfg.get("phone", state.get("phone"))
            state["password"] = cfg.get("password", state.get("password"))
            state["time_nick_active"] = bool(cfg.get("time_nick_active", state.get("time_nick_active", False)))
            state["autoresponder_active"] = bool(cfg.get("autoresponder_active", state.get("autoresponder_active", False)))
            state["registration_block_until_ts"] = float(cfg.get("registration_block_until_ts", state.get("registration_block_until_ts", 0.0)) or 0.0)
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
    set_presence(user_id, False)
    data = get_user_state(user_id)
    for task_key in ("time_nick_task", "activity_task", "online_task", "auto_read_offline_task", "activity_ui_task"):
        task = data.get(task_key)
        if task and task is not asyncio.current_task():
            task.cancel()
        data[task_key] = None

    data["time_nick_active"] = False
    data["autoresponder_active"] = False

    if data["client"]:
        await close_pyrogram_client(data["client"])
        data["client"] = None

    await clear_session_files(user_id)

    uid_str = str(user_id)
    if uid_str in MEMORY_DB["config"]:
        MEMORY_DB["config"][uid_str]["online_247"] = False
        MEMORY_DB["config"][uid_str]["auto_read"] = False
        MEMORY_DB["config"][uid_str]["logged_in"] = False
        MEMORY_DB["config"][uid_str]["time_nick_active"] = False
        MEMORY_DB["config"][uid_str]["autoresponder_active"] = False
        MEMORY_DB["config"][uid_str]["session_string"] = None
        MEMORY_DB["config"][uid_str]["replied_users"] = []
        queue_db_save("config", uid_str, MEMORY_DB["config"][uid_str])

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
        lock = get_user_state(event.from_user.id).setdefault("ui_lock", asyncio.Lock())
        async with lock:
            return await self.dispatch(handler, event, data)

    async def dispatch(self, handler, event, data):
        if isinstance(event, types.CallbackQuery) and event.message:
            user_id = event.from_user.id
            u_state = get_user_state(user_id)
            stats_task = u_state.get("admin_stats_task")
            if stats_task and not stats_task.done():
                stop_admin_server_stats_loop(user_id)
                await asyncio.gather(stats_task, return_exceptions=True)
            u_state["msg_id"] = event.message.message_id
            await stop_activity_ui(user_id)
            if event.data not in ("guard", "ignore"):
                u_state["ui_action_count"] = u_state.get("ui_action_count", 0) + 1
                u_state["recreate_pending"] = u_state["ui_action_count"] % 5 == 0

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
        if event.from_user:
            lock = get_user_state(event.from_user.id).setdefault("ui_lock", asyncio.Lock())
            async with lock:
                return await handler(event, data)
        return await handler(event, data)

dp.callback_query.middleware(RestartMiddleware())
dp.message.middleware(IncomingUserMessageCleanupMiddleware())

def start_ui_refresh_task(user_id):
    # Сообщения меняются только по действию пользователя.
    task = get_user_state(user_id).get("ui_refresh_task")
    if task and not task.done():
        task.cancel()


async def edit_or_send(user_id, text, reply_markup=None, parse_mode=None):
    data = get_user_state(user_id)
    if data.get("save_error"):
        text += "\n\n⚠️ Настройки пока не сохранены в базе. Повторная попытка выполняется автоматически."
    data["last_ui_text"] = text
    data["last_ui_reply_markup"] = reply_markup
    data["last_ui_parse_mode"] = parse_mode
    start_ui_refresh_task(user_id)

    await stop_activity_ui(user_id)
    force_new_message = data.pop("recreate_pending", False)
    if force_new_message and data.get("msg_id"):
        try:
            await bot.delete_message(chat_id=user_id, message_id=data["msg_id"])
        except TelegramBadRequest:
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
            error_text = str(e).lower()
            if "message is not modified" in error_text:
                return
            # Новое сообщение создаём только когда старого сообщения Telegram
            # уже действительно не существует. Для остальных ошибок не плодим UI.
            if "message to edit not found" not in error_text and "message identifier is not specified" not in error_text:
                logging.warning(f"Не удалось изменить UI-сообщение {user_id}: {e}")
                return
        except Exception as e:
            logging.warning(f"Не удалось изменить UI-сообщение {user_id}: {e}")
            return
        data["msg_id"] = None

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
        queue_db_save("config", uid_str, MEMORY_DB["config"][uid_str])

    if force_new_message:
        data["ui_action_count"] = 0


async def maybe_recreate_ui(callback):
    # Centralized in edit_or_send; legacy call sites remain compatible.
    return

def show_start_menu(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_view")
    builder.button(text=get_text(user_id, "btn_start"), callback_data="start_login")
    builder.button(text="Назад в главное меню 🏠", callback_data="root_menu")
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
        queue_db_save("config", uid_str, user_cfg)

        log_action(
            owner_id,
            f"Сработал автоответчик для пользователя {message.from_user.id}"
        )

    except Unauthorized:
        if owner_id:
            await handle_revoked_session(owner_id, reason="сессия отозвана")
    except Exception as e:
        logging.error(f"Ошибка автоответчика: {e}")

ACTIVITY_DAYS = 5

# Активность считается локально каждую секунду, но в Supabase сбрасывается редко.
# 5 минут ±25 секунд = окно 4:35..5:25. Для разных сессий секунды резервируются
# отдельно, чтобы при массовом запуске они не стреляли в Supabase одновременно.
ACTIVITY_SAVE_MIN_SECONDS = 275
ACTIVITY_SAVE_MAX_SECONDS = 325
ACTIVITY_SAVE_RESERVATIONS = {}
PROFILE_ACTIVITY_SUPPRESS_SECONDS = 6.0


def schedule_next_activity_save(user_id, data):
    now = time.monotonic()
    uid = str(user_id)

    old_slot = data.pop("activity_save_slot", None)
    if old_slot is not None and ACTIVITY_SAVE_RESERVATIONS.get(old_slot) == uid:
        ACTIVITY_SAVE_RESERVATIONS.pop(old_slot, None)

    # Чистим уже прошедшие слоты.
    for slot in list(ACTIVITY_SAVE_RESERVATIONS):
        if slot <= int(now):
            ACTIVITY_SAVE_RESERVATIONS.pop(slot, None)

    # Сначала пытаемся выбрать полностью случайную секунду в 50-секундном окне.
    candidates = list(range(ACTIVITY_SAVE_MIN_SECONDS, ACTIVITY_SAVE_MAX_SECONDS + 1))
    random.shuffle(candidates)
    chosen_delay = None
    chosen_slot = None
    for delay in candidates:
        slot = int(now + delay)
        if slot not in ACTIVITY_SAVE_RESERVATIONS:
            chosen_delay = delay + random.random()
            chosen_slot = slot
            break

    # Если сессий больше, чем свободных секунд окна, всё равно даём отдельный
    # субсекундный момент, чтобы запросы не стартовали одним asyncio-тактом.
    if chosen_delay is None:
        base = random.randint(ACTIVITY_SAVE_MIN_SECONDS, ACTIVITY_SAVE_MAX_SECONDS)
        chosen_delay = base + random.random()
        chosen_slot = int(now + base)

    ACTIVITY_SAVE_RESERVATIONS[chosen_slot] = uid
    data["activity_save_slot"] = chosen_slot
    data["activity_save_due"] = now + chosen_delay
    return chosen_delay


def profile_activity_suppressed(user_id):
    data = get_user_state(user_id)
    return time.monotonic() < data.get("profile_activity_suppress_until", 0.0)


def begin_profile_activity_suppression(user_id):
    data = get_user_state(user_id)
    # Перед служебным изменением имени фиксируем уже набранную реальную активность.
    # ВАЖНО: здесь больше НЕ переводим аккаунт в offline. Иначе UpdateStatus(False)
    # может погасить реальный presence аккаунта, даже если пользователь сейчас
    # сидит в Telegram с телефона/ПК. На короткое время лишь игнорируем технический
    # Online, который способен прилететь от самого update_profile().
    accrue_activity(user_id)
    data["profile_activity_suppress_until"] = time.monotonic() + PROFILE_ACTIVITY_SUPPRESS_SECONDS
    data["presence_poll_at"] = max(
        data.get("presence_poll_at", 0),
        data["profile_activity_suppress_until"],
    )

def add_activity_interval(activity, start_ts, end_ts, offset):
    tz = datetime.timezone(datetime.timedelta(hours=offset))
    while start_ts < end_ts:
        local = datetime.datetime.fromtimestamp(start_ts, tz)
        midnight = datetime.datetime.combine(local.date() + datetime.timedelta(days=1),
                                             datetime.time.min, tzinfo=tz).timestamp()
        stop = min(end_ts, midnight)
        key = local.strftime("%d.%m.%Y")
        activity[key] = float(activity.get(key, 0)) + stop - start_ts
        start_ts = stop


def accrue_activity(user_id, now=None):
    data = get_user_state(user_id)
    now = time.time() if now is None else now
    previous = data.get("activity_cursor", now)
    end = min(now, data.get("presence_until", 0))
    if data.get("presence_online") and end > previous:
        activity = MEMORY_DB["activity"].setdefault(str(user_id), {})
        offset = int(MEMORY_DB["config"].get(str(user_id), {}).get("timezone_offset", 5))
        add_activity_interval(activity, previous, end, offset)
        DB_DIRTY.add(("activity", str(user_id)))
    data["activity_cursor"] = now
    if now >= data.get("presence_until", 0):
        data["presence_online"] = False


def set_presence(user_id, online, expires=0):
    now = time.time()
    accrue_activity(user_id, now)
    data = get_user_state(user_id)
    data["presence_online"] = bool(online and expires > now)
    data["presence_until"] = float(expires) if online else 0
    data["presence_known"] = True
    data["presence_timestamp"] = now


def apply_status(user_id, status):
    if isinstance(status, raw_types.UserStatusOnline):
        # update_profile() может кратко подсветить именно эту MTProto-сессию как Online.
        # Такой технический Online от функции «Время в профиль» не считаем активностью.
        if profile_activity_suppressed(user_id):
            set_presence(user_id, False)
            return
        set_presence(user_id, True, status.expires)
    elif isinstance(status, raw_types.UserStatusOffline):
        set_presence(user_id, False)
    else:
        set_presence(user_id, False)
        get_user_state(user_id)["presence_known"] = False


async def presence_update(client, update, users, chats):
    if isinstance(update, raw_types.UpdateUserStatus) and update.user_id == getattr(client, "account_id", None):
        apply_status(client.owner_id, update.status)


async def sync_presence(user_id, client):
    try:
        rows = await client.invoke(functions.users.GetUsers(id=[raw_types.InputUserSelf()]))
        if rows:
            apply_status(user_id, getattr(rows[0], "status", None))
        get_user_state(user_id).pop("activity_error", None)
    except FloodWait as e:
        get_user_state(user_id)["presence_poll_at"] = time.monotonic() + e.value + 1
    except Unauthorized:
        await handle_revoked_session(user_id, "сессия отозвана")
    except Exception:
        get_user_state(user_id)["activity_error"] = "Не удалось уточнить статус Telegram."


async def activity_tracker_loop(user_id):
    data = get_user_state(user_id)
    uid = str(user_id)
    data["activity_cursor"] = time.time()
    if not data.get("activity_save_due"):
        schedule_next_activity_save(user_id, data)

    while True:
        client = data.get("client")
        if not client:
            return
        if not client.is_connected:
            set_presence(user_id, False)
            data["presence_known"] = False
            data["presence_poll_at"] = 0
        else:
            if time.monotonic() >= data.get("presence_poll_at", 0):
                data["presence_poll_at"] = time.monotonic() + 30
                await sync_presence(user_id, client)
            accrue_activity(user_id)

        activity = MEMORY_DB["activity"].get(uid, {})
        offset = int(MEMORY_DB["config"].get(uid, {}).get("timezone_offset", 5))
        today = (get_world_utc_datetime() + datetime.timedelta(hours=offset)).date()
        for key in list(activity):
            try:
                expired = (today - datetime.datetime.strptime(key, "%d.%m.%Y").date()).days >= ACTIVITY_DAYS
            except ValueError:
                expired = True
            if expired:
                del activity[key]
                DB_DIRTY.add(("activity", uid))

        # Сам таймер остаётся секундным и точным, но Supabase получает только один
        # накопленный снимок примерно каждые 4:35..5:25. У каждой сессии своё время.
        pending = data.get("activity_save_task")
        save_due = data.get("activity_save_due", 0.0)
        if (
            ("activity", uid) in DB_DIRTY
            and time.monotonic() >= save_due
            and (not pending or pending.done())
        ):
            pending = asyncio.create_task(async_db_save("activity", uid, activity))
            data["activity_save_task"] = pending
            DB_TASKS.add(pending)
            pending.add_done_callback(DB_TASKS.discard)
            schedule_next_activity_save(user_id, data)

        await asyncio.sleep(1)


async def auto_read_message(client, message):
    uid = client.owner_id
    cfg = MEMORY_DB["config"].get(str(uid), {})
    if not cfg.get("auto_read", False):
        return
    data = get_user_state(uid)
    try:
        await client.read_chat_history(message.chat.id, max_id=message.id)
        # Each new incoming message resets the two-second idle deadline.
        data["auto_read_deadline"] = time.monotonic() + 2
        task = data.get("auto_read_offline_task")
        if not task or task.done():
            data["auto_read_offline_task"] = asyncio.create_task(auto_read_offline(uid, client))
    except FloodWait as e:
        data["auto_read_error"] = f"Telegram ограничил чтение на {e.value} сек."
    except Unauthorized:
        await handle_revoked_session(uid, "сессия отозвана")
    except Exception as e:
        logging.warning("Автопрочтение %s: %s", uid, type(e).__name__)


async def auto_read_offline(uid, client):
    data = get_user_state(uid)
    while True:
        await asyncio.sleep(max(0, data.get("auto_read_deadline", 0) - time.monotonic()))
        async with data.setdefault("online_lock", asyncio.Lock()):
            if time.monotonic() < data.get("auto_read_deadline", 0):
                continue
            cfg = MEMORY_DB["config"].get(str(uid), {})
            if not cfg.get("auto_read") or cfg.get("online_247"):
                return
            try:
                await client.invoke(functions.account.UpdateStatus(offline=True))
                # Other devices may keep the account online; resync server status.
                data["presence_poll_at"] = 0
            except FloodWait as e:
                data["auto_read_deadline"] = time.monotonic() + e.value + 1
                continue
            except Unauthorized:
                await handle_revoked_session(uid, "сессия отозвана")
            except Exception as e:
                logging.warning("Выход из сети %s: %s", uid, type(e).__name__)
            return


async def send_online_status(user_id):
    """Один запрос сразу; последующие не чаще 45 секунд. FloodWait сохраняется."""
    data = get_user_state(user_id)
    lock = data.setdefault("online_lock", asyncio.Lock())
    async with lock:
        if not MEMORY_DB["config"].get(str(user_id), {}).get("online_247", False):
            return
        if time.monotonic() < data.get("online_next_at", 0):
            return
        client = data.get("client")
        if not client or not client.is_connected:
            data["online_error"] = "Нет соединения с Telegram; ожидаем восстановления."
            data["online_next_at"] = time.monotonic() + 45
            return
        try:
            await client.invoke(functions.account.UpdateStatus(offline=False))
            data["presence_poll_at"] = 0
            data.pop("online_error", None)
            data["online_next_at"] = time.monotonic() + 45
        except FloodWait as e:
            data["online_next_at"] = time.monotonic() + max(1, e.value) + 1
            data["online_error"] = f"Пауза Telegram: {e.value} сек."
        except Unauthorized:
            await handle_revoked_session(user_id, "сессия отозвана")
        except Exception as e:
            data["online_error"] = "Не удалось обновить онлайн; повторим через 45 секунд."
            data["online_next_at"] = time.monotonic() + 45
            logging.warning("Режим 24/7 %s: %s", user_id, e)


async def online_mode_loop(user_id):
    data = get_user_state(user_id)
    while MEMORY_DB["config"].get(str(user_id), {}).get("online_247", False):
        await send_online_status(user_id)
        await asyncio.sleep(max(1, data.get("online_next_at", 0) - time.monotonic()))


def start_online_mode(user_id):
    data = get_user_state(user_id)
    task = data.get("online_task")
    if MEMORY_DB["config"].get(str(user_id), {}).get("online_247", False):
        if not task or task.done():
            data["online_task"] = asyncio.create_task(online_mode_loop(user_id))


def start_activity_tracker(user_id):
    data = get_user_state(user_id)
    task = data.get("activity_task")
    if not task or task.done():
        data["activity_task"] = asyncio.create_task(activity_tracker_loop(user_id))
    start_online_mode(user_id)


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
        time_marker = format_profile_time(time_value, user_cfg.get("time_style", 1))
        new_first, new_last = profile_names(base_first, base_last, time_marker)
        if time.monotonic() < data.get("profile_retry_after", 0):
            return


        profile_key = (new_first, new_last)
        if data.get("last_profile_key") == profile_key:
            return

        online_247 = bool(user_cfg.get("online_247", False))
        if not online_247:
            begin_profile_activity_suppression(user_id)

        await data["client"].update_profile(first_name=new_first, last_name=new_last)
        data["last_profile_key"] = profile_key

        # Никакого принудительного UpdateStatus(offline=True) после смены ника.
        # Технический Online от update_profile() отсекается suppression-окном выше,
        # а реальный онлайн с других устройств остаётся нетронутым.


    except FloodWait as e:
        data["profile_retry_after"] = time.monotonic() + e.value
        logging.warning("Обновление имени %s: FloodWait %s", user_id, e.value)
    except Unauthorized:
        await handle_revoked_session(user_id, "сессия отозвана")
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
    uid = str(user_id)
    if uid not in MEMORY_DB["activity"]:
        MEMORY_DB["activity"][uid] = await async_db_get("activity", uid) or {}
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
    client.add_handler(RawUpdateHandler(presence_update), group=-2)
    client.add_handler(MessageHandler(auto_read_message, filters.private & filters.incoming & ~filters.me), group=-1)
    try:
        await client.start()
        client.account_id = (await client.get_me()).id
        return client
    except BaseException:
        await close_pyrogram_client(client)
        raise

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
    lock = get_user_state(user_id).setdefault("connect_lock", asyncio.Lock())
    async with lock:
        return await _ensure_client_connected(user_id)


async def _ensure_client_connected(user_id):
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
            start_activity_tracker(user_id)
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

        # Every saved account needs a connection to receive presence events.
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

async def session_recovery_loop():
    while True:
        await asyncio.sleep(60)
        rows = await asyncio.to_thread(db_get_all_config)
        for row in rows:
            uid = str(row.get("id", ""))
            if uid.isdigit() and isinstance(row.get("data"), dict):
                MEMORY_DB["config"].setdefault(uid, row["data"])
        for uid, cfg in list(MEMORY_DB["config"].items()):
            if cfg.get("logged_in") and cfg.get("session_string"):
                data = get_user_state(int(uid))
                client = data.get("client")
                if not client:
                    await ensure_client_connected(int(uid))
                elif client.is_connected:
                    start_activity_tracker(int(uid))


@dp.message(F.text.casefold() == "admin")
async def admin_command(message: types.Message):

    if not is_admin(message.from_user):
        return

    stop_admin_server_stats_loop(message.from_user.id)
    data = get_user_state(message.from_user.id)
    data["state"] = "ADMIN"
    await edit_or_send(
        message.from_user.id,
        "Админ меню:",
        reply_markup=build_admin_menu_markup(),
    )

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    await stop_activity_ui(user_id)
    stop_admin_server_stats_loop(user_id)
    data = get_user_state(user_id)
    uid_str = str(user_id)
    if data.get("msg_id"):
        try:
            await bot.delete_message(chat_id=user_id, message_id=data["msg_id"])
        except Exception:
            pass
        data["msg_id"] = None

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
        queue_db_save("config", uid_str, MEMORY_DB["config"][uid_str])

    cfg = MEMORY_DB["config"][uid_str]
    if not cfg.get("logged_in", False) and not cfg.get("ever_registered", False):
        cfg["last_entry_at"] = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        cfg["entry_first_name"] = message.from_user.first_name or "User"
        cfg["entry_username"] = message.from_user.username or "N/A"
        cfg["entry_phone"] = cfg.get("phone") if cfg.get("phone") not in (None, "", "Не указан") else "Не виден"
        MEMORY_DB["config"][uid_str] = cfg
        queue_db_save("config", uid_str, cfg)

    data["state"] = "ROOT"
    log_action(user_id, "Ввёл команду /start")
    await edit_or_send(user_id, "Главное меню:", reply_markup=root_menu_markup())


def root_menu_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="♨️UserBot", callback_data="userbot")
    builder.button(text="🔰Guard", callback_data="guard")
    builder.adjust(2)
    return builder.as_markup()


@dp.callback_query(F.data == "root_menu")
async def root_menu(callback: types.CallbackQuery):
    uid = callback.from_user.id
    get_user_state(uid)["state"] = "ROOT"
    await edit_or_send(uid, "Главное меню:", reply_markup=root_menu_markup())
    await callback.answer()


@dp.callback_query(F.data == "guard")
async def guard_placeholder(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "userbot")
async def open_userbot(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if await ensure_client_connected(uid):
        await main_menu(callback)
    elif MEMORY_DB["config"].get(str(uid), {}).get("logged_in"):
        await callback.answer("Не удалось подключиться. Попробуйте ещё раз чуть позже.", show_alert=True)
    elif is_registration_blocked(uid):
        await edit_or_send(uid, get_registration_block_text(uid), reply_markup=show_registration_block_markup(uid))
        await callback.answer()
    else:
        get_user_state(uid)["state"] = "START"
        await edit_or_send(uid, "♨️UserBot\n\nДля подключения аккаунта ознакомьтесь с правилами и начните регистрацию.", reply_markup=show_start_menu(uid))
        await callback.answer()

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
    builder.button(text="Назад в главное меню 🏠", callback_data="root_menu")
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
    if MEMORY_DB["config"].get(str(user_id), {}).get("logged_in"):
        await main_menu(callback)
        return
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
    queue_db_save("config", uid_str, cfg)
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
        queue_db_save("config", uid_str, cfg)

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
        **old_cfg,
        "phone": data["phone"] or old_cfg.get("phone", "Не указан"),
        # Пароль 2FA не сохраняем в Supabase: после авторизации для работы
        # используется session_string, а сам пароль больше не нужен.
        "password": "Нет",
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
    queue_db_save("config", uid_str, cfg)

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
        await edit_or_send(user_id, "♨️UserBot — управление аккаунтом:", reply_markup=show_main_menu_builder(user_id, user_obj=message.from_user).as_markup())
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
        await edit_or_send(user_id, "♨️UserBot — управление аккаунтом:", reply_markup=show_main_menu_builder(user_id, user_obj=message.from_user).as_markup())
    except Exception:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_pwd_wrong"), reply_markup=builder.as_markup())

def show_main_menu_builder(user_id, user_obj: types.User = None):
    """Главное меню с аккуратной сеткой кнопок."""
    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text="Статистика 📊", callback_data="menu_activity"),
        types.InlineKeyboardButton(text="Режимы 24/7♻️", callback_data="menu_247"),
    )
    builder.row(
        types.InlineKeyboardButton(text=get_text(user_id, "btn_autoresp"), callback_data="menu_autoresponder"),
        types.InlineKeyboardButton(text=get_text(user_id, "btn_timenick"), callback_data="menu_timenick"),
    )
    builder.row(types.InlineKeyboardButton(text="Назад в главное меню 🏠", callback_data="root_menu"))
    return builder

@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    stop_admin_server_stats_loop(user_id)
    is_valid = await ensure_client_connected(user_id)
    if not is_valid:
        await edit_or_send(user_id, get_text(user_id, "msg_session_missing"), reply_markup=get_missing_session_markup(user_id))
        try: await callback.answer()
        except Exception: pass
        return

    data = get_user_state(user_id)
    data["state"] = "MENU"
    await edit_or_send(user_id, "♨️UserBot — управление аккаунтом:", reply_markup=show_main_menu_builder(user_id, user_obj=callback.from_user).as_markup())
    try: await callback.answer()
    except Exception: pass


def ru_plural(value, one, few, many):
    if 11 <= value % 100 <= 14:
        return many
    return one if value % 10 == 1 else few if 2 <= value % 10 <= 4 else many


@dp.callback_query(F.data == "menu_247")
async def menu_247(callback: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="Вечный онлайн 📛", callback_data="menu_online")
    builder.button(text="Авто-Прочтение 📌", callback_data="menu_auto_read")
    builder.button(text="Назад в меню 🏠", callback_data="main_menu")
    builder.adjust(1)
    await edit_or_send(callback.from_user.id, "Режимы 24/7:", reply_markup=builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "menu_online")
async def menu_online(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not await ensure_client_connected(uid):
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    cfg = MEMORY_DB["config"].get(str(uid), {})
    active = cfg.get("online_247", False)
    text = "Вечный онлайн 📛:\n\nСтатус: " + ("🟢 Включен" if active else "🔴 Выключен")
    text += "\nПоддерживает статус вечного «в сети»."
    if get_user_state(uid).get("online_error") and active:
        text += "\n⚠️ " + get_user_state(uid)["online_error"]

    builder = InlineKeyboardBuilder()
    builder.button(text="🔴 Выключить" if active else "🟢 Включить", callback_data="toggle_247")
    builder.button(text="Назад в меню 🏠", callback_data="menu_247")
    builder.adjust(1)
    await edit_or_send(uid, text, reply_markup=builder.as_markup())
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "toggle_247")
async def toggle_247(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not await ensure_client_connected(uid):
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return

    data = get_user_state(uid)
    cfg = MEMORY_DB["config"].setdefault(str(uid), {})
    cfg["online_247"] = not cfg.get("online_247", False)
    await persist_user_config_now(uid, cfg)

    if cfg["online_247"]:
        data["online_next_at"] = 0
        await callback.answer()
        await send_online_status(uid)
        start_online_mode(uid)
    else:
        task = data.get("online_task")
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        data["online_task"] = None
        data.pop("online_error", None)
        await callback.answer()

    await maybe_recreate_ui(callback)
    await menu_online(callback)


@dp.callback_query(F.data == "menu_auto_read")
async def menu_auto_read(callback: types.CallbackQuery):
    uid = callback.from_user.id
    cfg = MEMORY_DB["config"].get(str(uid), {})
    active = cfg.get("auto_read", False)
    text = "Авто-Прочтение 📌:\n\nСтатус: " + ("🟢 Включен" if active else "🔴 Выключен")
    text += "\nАвтоматически прочитает новые сообщения в ЛС.\nЧерез 2 секунды отправляет выход из сети."
    if cfg.get("online_247"):
        text += "\nВечный онлайн включён: сообщения читаются, выход из сети отключён."
    text += "\nДругие открытые устройства могут сохранять статус «в сети»."
    builder = InlineKeyboardBuilder()
    builder.button(text="🔴 Выключить" if active else "🟢 Включить", callback_data="toggle_auto_read")
    builder.button(text="Назад в меню 🏠", callback_data="menu_247")
    builder.adjust(1)
    await edit_or_send(uid, text, reply_markup=builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "toggle_auto_read")
async def toggle_auto_read(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not await ensure_client_connected(uid):
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    cfg = MEMORY_DB["config"].setdefault(str(uid), {})
    cfg["auto_read"] = not cfg.get("auto_read", False)
    await persist_user_config_now(uid, cfg)
    if not cfg["auto_read"]:
        task = get_user_state(uid).get("auto_read_offline_task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await menu_auto_read(callback)


def activity_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="Назад в меню 🏠", callback_data="main_menu")
    return builder.as_markup()


def activity_text(user_id):
    accrue_activity(user_id)
    uid = str(user_id)
    data = get_user_state(user_id)
    offset = int(MEMORY_DB["config"].get(uid, {}).get("timezone_offset", 5))
    now = get_world_utc_datetime() + datetime.timedelta(hours=offset)
    lines = ["🗂Статистика активности:", ""]
    for i in range(ACTIVITY_DAYS):
        key = (now.date() - datetime.timedelta(days=i)).strftime("%d.%m.%Y")
        total = max(0, int(MEMORY_DB["activity"].get(uid, {}).get(key, 0)))
        hours, rem = divmod(total, 3600)
        minutes, seconds = divmod(rem, 60)
        lines.append(f"{key} — {hours} ч. {minutes:02d} мин. {seconds:02d} сек.")
    status = ("🟢 В сети" if data.get("presence_online") else "🔴 Не в сети") if data.get("presence_known") else "⚪ Статус уточняется"
    lines.extend(["", status, "Учитывается статус аккаунта, включая вечный онлайн.", f"Обновлено: {now:%H:%M:%S}"])
    if data.get("activity_error"):
        lines.append(data["activity_error"])
    if ("activity", uid) in DB_DIRTY and data.get("activity_save_error"):
        lines.append("Сохранение статистики ожидает подключения к базе.")
    return "\n".join(lines)


async def stop_activity_ui(user_id):
    data = get_user_state(user_id)
    task = data.pop("activity_ui_task", None)
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def activity_ui_loop(user_id, message_id):
    while True:
        await asyncio.sleep(1)
        try:
            await bot.edit_message_text(chat_id=user_id, message_id=message_id,
                                        text=activity_text(user_id), reply_markup=activity_markup())
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                return
        except Exception as e:
            logging.warning("Обновление статистики %s: %s", user_id, type(e).__name__)
            await asyncio.sleep(3)


@dp.callback_query(F.data == "menu_activity")
async def menu_activity(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not await ensure_client_connected(uid):
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    await edit_or_send(uid, activity_text(uid), reply_markup=activity_markup())
    data = get_user_state(uid)
    data["activity_ui_task"] = asyncio.create_task(activity_ui_loop(uid, data["msg_id"]))
    await callback.answer()

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
    builder.adjust(1)

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
    await persist_user_config_now(user_id, cfg)

    log_action(user_id, f"Автоответчик: {'Включен' if new_status else 'Выключен'}")
    await maybe_recreate_ui(callback)
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
        await persist_user_config_now(user_id, cfg)
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

    profile_preview = get_current_styled_profile_preview(base_first, base_last, offset, include_time=True, style=cfg.get("time_style", 1))
    sign_str = f"+{offset}" if offset >= 0 else str(offset)

    text = get_text(user_id, "msg_timenick_text", status_str, profile_preview, sign_str)

    builder = InlineKeyboardBuilder()
    btn_toggle_text = get_text(user_id, "btn_turn_off") if is_active else get_text(user_id, "btn_turn_on")
    builder.button(text=btn_toggle_text, callback_data="toggle_timenick")
    builder.button(text=get_text(user_id, "btn_tz_select"), callback_data="tz_select")
    builder.button(text="Стили 🎨", callback_data="time_styles")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(1)

    await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

TIME_STYLES_PER_PAGE = 15


def build_time_styles_markup(raw_time, page=0):
    pages = (len(TIME_STYLES) + TIME_STYLES_PER_PAGE - 1) // TIME_STYLES_PER_PAGE
    page = max(0, min(page, pages - 1))
    builder = InlineKeyboardBuilder()
    first = page * TIME_STYLES_PER_PAGE
    for index in range(first, min(first + TIME_STYLES_PER_PAGE, len(TIME_STYLES))):
        builder.button(text=format_profile_time(raw_time, index), callback_data=f"time_style_{index}")
    builder.adjust(3)
    # Циклическая навигация: обе стрелки работают на каждой странице.
    builder.row(
        types.InlineKeyboardButton(text="⬅️", callback_data=f"time_styles_page_{(page - 1) % pages}"),
        types.InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="ignore"),
        types.InlineKeyboardButton(text="➡️", callback_data=f"time_styles_page_{(page + 1) % pages}"),
    )
    builder.row(types.InlineKeyboardButton(text="Назад ⬅️", callback_data="menu_timenick"))
    return builder.as_markup()


@dp.callback_query(F.data == "time_styles")
@dp.callback_query(F.data.startswith("time_styles_page_"))
async def time_styles(callback: types.CallbackQuery):
    uid = callback.from_user.id
    page = 0
    if callback.data and callback.data.startswith("time_styles_page_"):
        try:
            page = int(callback.data.rsplit("_", 1)[1])
            if not 0 <= page < (len(TIME_STYLES) + TIME_STYLES_PER_PAGE - 1) // TIME_STYLES_PER_PAGE:
                raise ValueError
        except (ValueError, IndexError):
            await callback.answer("Страница не найдена.")
            return
    cfg = MEMORY_DB["config"].get(str(uid), {})
    now = get_world_utc_datetime() + datetime.timedelta(hours=int(cfg.get("timezone_offset", 5)))
    await edit_or_send(uid, "Выберите стиль:",
                       reply_markup=build_time_styles_markup(now.strftime("%H:%M"), page))
    await callback.answer()


@dp.callback_query(F.data.startswith("time_style_"))
async def select_time_style(callback: types.CallbackQuery):
    uid = callback.from_user.id
    try:
        style = int(callback.data.rsplit("_", 1)[1])
        if not 0 <= style < len(TIME_STYLES):
            raise ValueError
    except (ValueError, IndexError):
        await callback.answer("Неизвестный стиль.")
        return
    if not await ensure_client_connected(uid):
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    cfg = MEMORY_DB["config"][str(uid)]
    cfg["time_style"] = style
    await persist_user_config_now(uid, cfg)
    await callback.answer()
    if cfg.get("time_nick_active"):
        await update_profile_branding(uid)
    await menu_timenick(callback)


@dp.callback_query(F.data == "toggle_timenick")
async def toggle_timenick(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}

    if not await ensure_client_connected(user_id):
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    new_status = not cfg.get("time_nick_active", False)
    cfg["time_nick_active"] = new_status
    data["time_nick_active"] = new_status
    await persist_user_config_now(user_id, cfg)

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
                if not cfg.get("online_247", False):
                    begin_profile_activity_suppression(user_id)
                await data["client"].update_profile(first_name=base_first, last_name=base_last)
                data.pop("last_profile_key", None)
                # При возврате обычного имени тоже не отправляем forced offline:
                # suppression уже отсекает служебный Online от update_profile().
            except Exception as e:
                logging.error(f"Ошибка сброса имени профиля: {e}")

    log_action(user_id, f"Время в профиле: {'Включено' if new_status else 'Выключено'}")
    await maybe_recreate_ui(callback)
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
    try:
        tz_val = int(callback.data.split("_")[-1])
        if tz_val not in TIMEZONE_NAMES:
            raise ValueError
    except ValueError:
        await callback.answer("Неизвестный часовой пояс.")
        return
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    cfg["timezone_offset"] = tz_val
    await persist_user_config_now(user_id, cfg)

    sign_str = f"+{tz_val}" if tz_val >= 0 else str(tz_val)
    log_action(user_id, f"Изменён часовой пояс: UTC{sign_str}")

    if cfg.get("time_nick_active", False):
        asyncio.create_task(update_profile_branding(user_id))

    await menu_timenick(callback)


@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: types.CallbackQuery):
    try: await callback.answer()
    except Exception: pass

# -----------------------------------------------------------------------------
# Серверная статистика (админ)
# -----------------------------------------------------------------------------

SERVER_STATS_CACHE = {"supabase_db_mb": None, "supabase_source": None, "updated_at": 0.0}
SERVER_STATS_LOCK = asyncio.Lock()
PROCESS_STARTED_AT = time.monotonic()
try:
    PROCESS_STARTED_AT -= max(0, time.time() - psutil.Process(os.getpid()).create_time())
except Exception:
    pass


def _next_render_reset_utc(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if now.month == 12:
        return datetime.datetime(now.year + 1, 1, 1, tzinfo=datetime.timezone.utc)
    return datetime.datetime(now.year, now.month + 1, 1, tzinfo=datetime.timezone.utc)


async def _get_supabase_db_mb():
    if not supabase:
        return None, "нет подключения"

    # Получаем реальный размер текущей Supabase PostgreSQL БД через pg_database_size().
    # Функция get_database_size_bytes() создаётся один раз в SQL Editor Supabase.
    rpc_name = SUPABASE_DB_SIZE_RPC or "get_database_size_bytes"
    try:
        result = await asyncio.to_thread(lambda: supabase.rpc(rpc_name, {}).execute())
        raw = result.data
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if isinstance(raw, dict):
            raw = (
                raw.get("bytes")
                or raw.get("size_bytes")
                or raw.get("db_size_bytes")
                or raw.get("size")
                or raw.get("database_size_bytes")
            )
        value = float(raw)
        return (value / (1024 * 1024), "rpc_bytes")
    except Exception as e:
        logging.warning(f"Не удалось получить точный размер Supabase через RPC {rpc_name}: {e}")
        # Не показываем ложный размер как точный.
        return None, "rpc_error"


async def refresh_server_stats_cache(force=False):
    # Только Supabase, не чаще раза в пять минут. Render API не используется.
    async with SERVER_STATS_LOCK:
        if time.monotonic() - SERVER_STATS_CACHE["updated_at"] >= 300 or not SERVER_STATS_CACHE["updated_at"]:
            value, source = await _get_supabase_db_mb()
            SERVER_STATS_CACHE.update(supabase_db_mb=value, supabase_source=source, updated_at=time.monotonic())
    return SERVER_STATS_CACHE


def _build_server_stats_text(cache):
    now = datetime.datetime.now(datetime.timezone.utc)
    reset = _next_render_reset_utc(now)
    lines = ["Статистика сервера:", "", "🟣 Render",
             f"Сброс лимита: {reset.strftime('%d.%m.%Y %H:%M UTC')}",
             f"До сброса: {format_remaining_time((reset - now).total_seconds())}",
             f"Аптайм процесса: {format_remaining_time(time.monotonic() - PROCESS_STARTED_AT)}",
             "", "🟢 Supabase"]
    size = cache.get("supabase_db_mb")
    lines.append(f"База: {size:.1f} / {SUPABASE_DB_LIMIT_MB:g} MB" if size is not None else "База: данные недоступны")
    return "\n".join(lines)


def build_admin_stats_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_server_stats_back")
    return builder.as_markup()


async def _admin_server_stats_loop(user_id):
    data = get_user_state(user_id)
    while data.get("admin_stats_active"):
        await asyncio.sleep(1)
        if not data.get("admin_stats_active"):
            return
        try:
            await bot.edit_message_text(chat_id=user_id, message_id=data["msg_id"],
                text=_build_server_stats_text(SERVER_STATS_CACHE), reply_markup=build_admin_stats_markup())
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                data["admin_stats_active"] = False
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning("Статистика сервера: %s", e)
            await asyncio.sleep(5)


def stop_admin_server_stats_loop(user_id):
    data = get_user_state(user_id)
    data["admin_stats_active"] = False
    task = data.get("admin_stats_task")
    if task and not task.done():
        task.cancel()
    data["admin_stats_task"] = None


def build_admin_menu_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(ADMIN_ID, "btn_server_stats"), callback_data="admin_server_stats")
    builder.button(text="Активнные🟢", callback_data="admin_users_1")
    builder.button(text="Не-входящие🔴", callback_data="admin_entries_1")
    builder.button(text="Назад в меню 🏠", callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data.in_(["admin_server_stats", "admin_server_stats_refresh"]))
async def admin_server_stats(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        return
    user_id = callback.from_user.id
    stop_admin_server_stats_loop(user_id)
    data = get_user_state(user_id)
    data["state"] = "ADMIN_STATS"
    try: await callback.answer("Обновляю…")
    except TelegramBadRequest: pass
    cache = await refresh_server_stats_cache(force=True)
    if data.get("state") != "ADMIN_STATS":
        return
    await edit_or_send(
        user_id,
        _build_server_stats_text(cache),
        reply_markup=build_admin_stats_markup(),
    )
    data["admin_stats_active"] = True
    data["admin_stats_task"] = asyncio.create_task(_admin_server_stats_loop(user_id))
    try: await callback.answer()
    except Exception: pass


@dp.callback_query(F.data == "admin_server_stats_back")
async def admin_server_stats_back(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        return
    user_id = callback.from_user.id
    stop_admin_server_stats_loop(user_id)
    data = get_user_state(user_id)
    data["state"] = "ADMIN"
    await edit_or_send(
        user_id,
        "Админ меню:",
        reply_markup=build_admin_menu_markup(),
    )
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.in_(["admin_menu", "admin_users_back"]))
async def admin_users_back(callback: types.CallbackQuery):
    if not is_admin(callback.from_user):
        return
    stop_admin_server_stats_loop(callback.from_user.id)
    get_user_state(callback.from_user.id)["state"] = "ADMIN"
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
    stop_admin_server_stats_loop(callback.from_user.id)

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
    stop_admin_server_stats_loop(callback.from_user.id)
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
            start_activity_tracker(user_id)
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
    stop_admin_server_stats_loop(callback.from_user.id)

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
    stop_admin_server_stats_loop(callback.from_user.id)
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
    if not supabase:
        raise RuntimeError("Для сохранения настроек задайте рабочие SUPABASE_URL и SUPABASE_KEY.")
    await start_web_server()


    await sync_world_clock(force=True)
    asyncio.create_task(ntp_sync_loop())

    db_task = asyncio.create_task(db_retry_loop())
    await restore_saved_sessions()
    recovery_task = asyncio.create_task(session_recovery_loop())
    logging.info("🚀 Бот успешно запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        tasks = []
        for uid in USER_DATA:
            accrue_activity(uid)
        for data in USER_DATA.values():
            for key in ("activity_task", "online_task", "time_nick_task", "ui_refresh_task", "admin_stats_task", "activity_ui_task", "auto_read_offline_task"):
                task = data.get(key)
                if task and not task.done():
                    task.cancel()
                    tasks.append(task)
        await asyncio.gather(*tasks, return_exceptions=True)
        recovery_task.cancel()
        await asyncio.gather(recovery_task, return_exceptions=True)
        db_task.cancel()
        await asyncio.gather(db_task, return_exceptions=True)
        await asyncio.gather(*list(DB_TASKS), return_exceptions=True)
        for uid, cfg in MEMORY_DB["config"].items():
            await async_db_save("config", uid, cfg)
        for uid, activity in MEMORY_DB["activity"].items():
            await async_db_save("activity", uid, activity)
        for data in USER_DATA.values():
            if data.get("client"):
                await close_pyrogram_client(data["client"])
        await bot.session.close()

if __name__ == "__main__":
    loop.run_until_complete(main())
