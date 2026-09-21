import copy
import struct
import binascii
import base64
import hashlib
import json
import sqlite3
import uuid
import zlib
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
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from pyrogram import Client, enums, filters
from pyrogram.handlers import MessageHandler, EditedMessageHandler, RawUpdateHandler
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
    cfg = cached_config(uid_str)
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
    "msg_timenick_text": "Вывод текущего времени в имя профиля.\n\nТекущий статус: {0}\nПредпросмотр: {1}\nСмещение часового пояса: UTC{2}",
    "msg_tz_select": "Выберите ваш часовой пояс🌐",
    "msg_tz_saved": "Часовой пояс изменен на UTC{0}!",
    "msg_autoresp_text": "🤖 **Автоответчик**\n\nСтатус: {1}\nОтветы: только новым собеседникам 👤\n\nТекст приветствия:\n💬 \"{0}\"",
    "msg_autoresp_req": "Напишите новый текст приветствия в чат ✏️",
    "msg_autoresp_saved": "Приветствие успешно сохранено! 🎉",
    "msg_autoresp_default": "👋 Здравствуйте! Сейчас я не в сети, отвечу позже.",
}


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


def _time_style_suffix_match(value, style_index):

    text = (value or "").rstrip()
    if not text:
        return text, False
    try:
        index = int(style_index)
        digits, left, right, colon = TIME_STYLES[index]
    except (TypeError, ValueError, IndexError):
        return text, False


    digit_class = re.escape(digits)
    pattern = re.compile(
        re.escape(left)
        + rf"([{digit_class}]{{2}})"
        + re.escape(colon)
        + rf"([{digit_class}]{{2}})"
        + re.escape(right)
        + r"$"
    )
    match = pattern.search(text)
    if not match:
        return text, False

    reverse_digits = {char: str(pos) for pos, char in enumerate(digits)}
    try:
        hour = int("".join(reverse_digits[ch] for ch in match.group(1)))
        minute = int("".join(reverse_digits[ch] for ch in match.group(2)))
    except (KeyError, ValueError):
        return text, False
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return text, False

    return text[:match.start()].rstrip(), True


def strip_profile_time_suffix(value, preferred_style=None):

    order = []
    try:
        preferred = int(preferred_style)
        if 0 <= preferred < len(TIME_STYLES):
            order.append(preferred)
    except (TypeError, ValueError):
        pass
    order.extend(index for index in range(len(TIME_STYLES)) if index not in order)

    for index in order:
        base, matched = _time_style_suffix_match(value, index)
        if matched:
            return base, True
    return (value or "").rstrip(), False


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
    cfg = cached_config(uid_str)
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

MEMORY_DB = {"config": {}, "logs": {}}
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
        payload = {"id": str(user_id), "data": data}
        query = supabase.table(table)
        try:


            query.upsert(payload, on_conflict="id").execute()
        except TypeError:

            query.upsert(payload).execute()
        return True
    except Exception as e:
        logging.warning(
            "Supabase write failed (%s/%s): %s: %s",
            table,
            user_id,
            type(e).__name__,
            str(e)[:300],
        )
        return False

async def async_db_get(table: str, user_id: str):


    delays = (0.0, 0.35, 0.9)
    last_error = None
    for delay in delays:
        if delay:
            await asyncio.sleep(delay)
        try:
            return await asyncio.to_thread(db_get_data, table, str(user_id))
        except RuntimeError as e:
            last_error = e
    raise last_error or RuntimeError("Не удалось загрузить данные из Supabase.")

DB_WRITE_LOCKS = {}
DB_DIRTY = set()
DB_TASKS = set()
DB_WORKERS = {}
DB_REVISIONS = {}
DB_SAVED_REVISIONS = {}
DB_WRITE_SEMAPHORE = asyncio.Semaphore(max(1, int(os.getenv("SUPABASE_MAX_PARALLEL_WRITES", "4") or 4)))


DB_SAVE_DEBOUNCE_SECONDS = 0.35
DB_CONFIG_SAVE_DEBOUNCE_SECONDS = max(0.05, float(os.getenv("SUPABASE_CONFIG_DEBOUNCE", "0.20") or 0.20))
DB_LOG_SAVE_DEBOUNCE_SECONDS = max(0.20, float(os.getenv("SUPABASE_LOG_DEBOUNCE", "0.80") or 0.80))
DB_SAVE_RETRY_BASE_SECONDS = 1.0
DB_SAVE_RETRY_MAX_SECONDS = 30.0
DB_SAVE_WARNING_AFTER_SECONDS = 20.0
DB_SAVE_WARNING_TEXT = "⚠️ Настройки пока не сохранены в базе. Повторная попытка выполняется автоматически."


def _db_save_debounce_for(table):
    if table == "config":
        return DB_CONFIG_SAVE_DEBOUNCE_SECONDS
    if table == "logs":
        return DB_LOG_SAVE_DEBOUNCE_SECONDS
    return DB_SAVE_DEBOUNCE_SECONDS


def _bump_db_revision(key):
    DB_REVISIONS[key] = int(DB_REVISIONS.get(key, 0) or 0) + 1
    return DB_REVISIONS[key]


def _track_db_task(task):
    DB_TASKS.add(task)
    task.add_done_callback(DB_TASKS.discard)
    return task


def _ensure_db_worker(table, uid):
    key = (table, str(uid))
    task = DB_WORKERS.get(key)
    if task and not task.done():
        return task
    task = asyncio.create_task(_db_save_worker(table, str(uid)))
    DB_WORKERS[key] = task
    _track_db_task(task)
    return task


def queue_db_save(table, uid, data):

    uid = str(uid)
    key = (table, uid)


    table_cache = MEMORY_DB.setdefault(table, {})
    if uid not in table_cache:
        table_cache[uid] = copy.deepcopy(data)

    DB_DIRTY.add(key)
    _bump_db_revision(key)
    _ensure_db_worker(table, uid)


def _record_db_save_result(table: str, user_id: str, ok: bool):

    try:
        numeric_uid = int(user_id)
    except (TypeError, ValueError):
        return

    state = USER_DATA.get(numeric_uid)
    if state is None:
        return

    if table == "config":
        if ok:
            had_error = bool(state.get("save_error"))
            state["config_save_failures"] = 0
            state["config_save_failed_since"] = 0.0
            state["save_error"] = False
            state.pop("config_save_error", None)
            if had_error and state.get("msg_id") and state.get("last_ui_text") is not None:
                try:
                    asyncio.create_task(refresh_ui_after_db_recovery(numeric_uid))
                except RuntimeError:
                    pass
            return

        failures = int(state.get("config_save_failures", 0) or 0) + 1
        state["config_save_failures"] = failures
        if not state.get("config_save_failed_since"):
            state["config_save_failed_since"] = time.monotonic()

        failed_for = time.monotonic() - float(state.get("config_save_failed_since", 0.0) or 0.0)


        persistent = failures >= 6 or failed_for >= DB_SAVE_WARNING_AFTER_SECONDS
        state["save_error"] = persistent
        if persistent:
            state["config_save_error"] = True


async def _write_latest_snapshot(table: str, user_id: str, fallback_data=None):

    uid = str(user_id)
    key = (table, uid)
    lock = DB_WRITE_LOCKS.setdefault(key, asyncio.Lock())

    async with lock:
        current = MEMORY_DB.get(table, {}).get(uid, fallback_data if fallback_data is not None else {})
        snapshot = copy.deepcopy(current)
        revision = int(DB_REVISIONS.get(key, 0) or 0)
        async with DB_WRITE_SEMAPHORE:
            ok = await asyncio.to_thread(db_save_data, table, uid, snapshot)

        if ok:
            DB_SAVED_REVISIONS[key] = max(int(DB_SAVED_REVISIONS.get(key, 0) or 0), revision)
            latest = MEMORY_DB.get(table, {}).get(uid, fallback_data if fallback_data is not None else {})

            if revision == int(DB_REVISIONS.get(key, 0) or 0) and snapshot == latest:
                DB_DIRTY.discard(key)
        return ok


async def _db_save_worker(table: str, user_id: str):

    uid = str(user_id)
    key = (table, uid)
    backoff = DB_SAVE_RETRY_BASE_SECONDS
    was_cancelled = False
    try:
        while key in DB_DIRTY:

            before = int(DB_REVISIONS.get(key, 0) or 0)
            await asyncio.sleep(_db_save_debounce_for(table))
            if before != int(DB_REVISIONS.get(key, 0) or 0):
                continue

            ok = await _write_latest_snapshot(table, uid)
            _record_db_save_result(table, uid, ok)
            if ok:
                backoff = DB_SAVE_RETRY_BASE_SECONDS


                continue


            await asyncio.sleep(backoff + random.uniform(0.0, min(1.0, backoff * 0.25)))
            backoff = min(DB_SAVE_RETRY_MAX_SECONDS, backoff * 2.0)
    except asyncio.CancelledError:
        was_cancelled = True
        raise
    except Exception as e:
        logging.exception("DB worker crashed for %s/%s: %s", table, uid, e)
    finally:
        if DB_WORKERS.get(key) is asyncio.current_task():
            DB_WORKERS.pop(key, None)


        if key in DB_DIRTY and not was_cancelled:
            try:
                _ensure_db_worker(table, uid)
            except RuntimeError:
                pass


async def async_db_save(table: str, user_id: str, data: dict, max_attempts=3, background_on_fail=True):

    uid = str(user_id)
    key = (table, uid)
    table_cache = MEMORY_DB.setdefault(table, {})
    if uid not in table_cache:
        table_cache[uid] = copy.deepcopy(data)

    DB_DIRTY.add(key)
    _bump_db_revision(key)

    delay = 0.35
    attempts = max(1, int(max_attempts or 1))
    for attempt in range(attempts):
        ok = await _write_latest_snapshot(table, uid, fallback_data=data)
        if ok:
            _record_db_save_result(table, uid, True)


            if key in DB_DIRTY and background_on_fail:
                _ensure_db_worker(table, uid)
            return True
        if attempt + 1 < attempts:
            await asyncio.sleep(delay + random.uniform(0.0, 0.15))
            delay = min(2.0, delay * 2.0)

    _record_db_save_result(table, uid, False)
    if background_on_fail:
        _ensure_db_worker(table, uid)
    return False


async def db_retry_loop():

    while True:
        await asyncio.sleep(15)
        for table, uid in list(DB_DIRTY):

            _ensure_db_worker(table, uid)


async def persist_user_config_now(user_id: int, cfg: dict):

    uid = str(user_id)
    MEMORY_DB["config"][uid] = cfg
    queue_db_save("config", uid, cfg)


    await asyncio.sleep(0)
    return True


async def sync_profile_base_from_telegram(user_id: int, cfg=None, me=None, persist=True):

    data = get_user_state(user_id)
    uid = str(user_id)
    if cfg is None:
        cfg = cached_config(uid)
    MEMORY_DB["config"][uid] = cfg

    client = data.get("client")
    if me is None:
        if not client or not client.is_connected:
            return cfg
        me = await client.get_me()
    if not me:
        return cfg

    current_first = ((getattr(me, "first_name", None) or "User").strip() or "User")[:64]
    current_last = (getattr(me, "last_name", None) or "").strip()[:64]
    base_first = current_first
    base_last = current_last

    if cfg.get("time_nick_active", False):
        preferred_style = cfg.get("time_style", 1)
        stored_first = ((cfg.get("profile_base_first_name") or "User").strip() or "User")[:64]
        stored_last = (cfg.get("profile_base_last_name") or "").strip()[:64]


        if (current_first, current_last) != (stored_first, stored_last):
            last_profile_key = data.get("last_profile_key")
            if last_profile_key and len(last_profile_key) == 2:


                if current_last == last_profile_key[1]:
                    clean_last, last_had_time = strip_profile_time_suffix(current_last, preferred_style)
                    if last_had_time:
                        base_last = clean_last
                if current_first == last_profile_key[0]:
                    clean_first, first_had_time = strip_profile_time_suffix(current_first, preferred_style)
                    if first_had_time:
                        base_first = clean_first or "User"
            else:


                clean_last, last_had_time = strip_profile_time_suffix(current_last, preferred_style)
                clean_first, first_had_time = strip_profile_time_suffix(current_first, preferred_style)
                if last_had_time:
                    base_last = clean_last
                elif first_had_time:
                    base_first = clean_first or "User"

    base_first = (base_first.strip() or "User")[:64]
    base_last = base_last.strip()[:64]
    changed = (
        cfg.get("profile_base_first_name") != base_first
        or cfg.get("profile_base_last_name", "") != base_last
    )
    if changed:
        cfg["profile_base_first_name"] = base_first
        cfg["profile_base_last_name"] = base_last
        cfg["first_name"] = base_first
        MEMORY_DB["config"][uid] = cfg
        data.pop("last_profile_key", None)
        if persist:
            await persist_user_config_now(user_id, cfg)
    return cfg

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
            "session_string": cfg.get("session_string"),
            "client": None,
            "state": "MENU" if cfg.get("logged_in", False) else "START",
            "time_nick_active": bool(cfg.get("time_nick_active", False)),
            "time_nick_task": None,
            "autoresponder_active": bool(cfg.get("autoresponder_active", False)),
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
    builder.button(text='Назад в главное меню 🏠', callback_data='root_menu')
    builder.adjust(1)
    return builder.as_markup()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class RestartMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        lock = get_user_state(event.from_user.id).setdefault("ui_lock", asyncio.Lock())
        async with lock:
            return await self.dispatch(handler, event, data)

    async def dispatch(self, handler, event, data):
        if isinstance(event, types.CallbackQuery) and event.message:
            if event.message.chat.id != event.from_user.id:
                await event.answer("Откройте бота в личном чате.", show_alert=True)
                return
            if event.data == "saved_ok":
                return await handler(event, data)
            user_id = event.from_user.id
            u_state = get_user_state(user_id)
            stats_task = u_state.get("admin_stats_task")
            if stats_task and not stats_task.done():
                stop_admin_server_stats_loop(user_id)
                await asyncio.gather(stats_task, return_exceptions=True)
            u_state["msg_id"] = event.message.message_id
            action = event.data or ''
            if is_preview(user_id) and preview_action(action):
                return await render_userbot_preview(event)
            if not is_preview(user_id) and (action.startswith(('toggle_', 'set_tz_', 'time_style_')) or action in ('saved_toggle', 'autoresp_setup')):
                if not await ensure_client_connected(user_id):
                    if is_preview(user_id):
                        return await preview_registration(event)
                    await event.answer('Соединение временно недоступно. Повторите позже.', show_alert=True)
                    return
            if event.data not in ("guard", "ignore"):
                u_state["ui_action_count"] = u_state.get("ui_action_count", 0) + 1
                u_state["recreate_pending"] = u_state["ui_action_count"] % 5 == 0

            if u_state["state"] == "START":
                uid_str = str(user_id)
                cfg = cached_config(uid_str)
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

    task = get_user_state(user_id).get("ui_refresh_task")
    if task and not task.done():
        task.cancel()


async def edit_or_send(user_id, text, reply_markup=None, parse_mode=None):
    data = get_user_state(user_id)


    if is_preview(user_id) and data.get('state') not in ('ROOT', 'USERBOT_ENTRY', 'ADMIN', 'ADMIN_STATS'):
        text = '#Предпросмотр\n\n' + text.removeprefix('#Предпросмотр\n\n')
    clean_text = text
    display_text = clean_text
    if data.get("save_error"):
        display_text += "\n\n" + DB_SAVE_WARNING_TEXT
    data["last_ui_text"] = clean_text
    data["last_ui_reply_markup"] = reply_markup
    data["last_ui_parse_mode"] = parse_mode
    start_ui_refresh_task(user_id)

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
                text=display_text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return True
        except TelegramBadRequest as e:
            error_text = str(e).lower()
            if "message is not modified" in error_text:
                return True


            if "message to edit not found" not in error_text and "message identifier is not specified" not in error_text:
                logging.warning(f"Не удалось изменить UI-сообщение {user_id}: {e}")
                return False
        except Exception as e:
            logging.warning(f"Не удалось изменить UI-сообщение {user_id}: {e}")
            return False
        data["msg_id"] = None

    msg = await bot.send_message(
        chat_id=user_id,
        text=display_text,
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
    return True


async def refresh_ui_after_db_recovery(user_id):

    data = get_user_state(user_id)
    lock = data.setdefault("ui_lock", asyncio.Lock())
    async with lock:
        if data.get("save_error") or not data.get("msg_id") or data.get("last_ui_text") is None:
            return
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=data["msg_id"],
                text=data["last_ui_text"],
                reply_markup=data.get("last_ui_reply_markup"),
                parse_mode=data.get("last_ui_parse_mode"),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                logging.debug("Не удалось убрать DB-предупреждение %s: %s", user_id, e)
        except Exception as e:
            logging.debug("Не удалось обновить UI после восстановления БД %s: %s", user_id, e)


async def maybe_recreate_ui(callback):

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
        user_cfg = cached_config(uid_str)
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

async def auto_read_message(client, message):
    uid = client.owner_id
    cfg = MEMORY_DB["config"].get(str(uid), {})
    if not cfg.get("auto_read", False):
        return
    data = get_user_state(uid)
    try:


        await client.read_chat_history(message.chat.id, max_id=message.id)
        data.pop("auto_read_error", None)
    except FloodWait as e:
        data["auto_read_error"] = f"Telegram ограничил чтение на {e.value} сек."
    except Unauthorized:
        await handle_revoked_session(uid, "сессия отозвана")
    except Exception as e:
        logging.warning("Автопрочтение %s: %s", uid, type(e).__name__)


async def auto_read_offline(uid, client):


    return


async def send_online_status(user_id):

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


def start_userbot_features(user_id):
    start_online_mode(user_id)
    start_saved_history(user_id)


async def update_profile_branding(user_id, sync_base=True):
    data = get_user_state(user_id)
    uid_str = str(user_id)

    if not data.get("client") or not data["client"].is_connected:
        return

    try:

        user_cfg = cached_config(uid_str)

        if not user_cfg.get("time_nick_active", False):
            return


        if sync_base:
            user_cfg = await sync_profile_base_from_telegram(user_id, user_cfg, persist=True)
        base_first = (user_cfg.get("profile_base_first_name") or "User").strip() or "User"
        base_last = (user_cfg.get("profile_base_last_name") or "").strip()

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

        await data["client"].update_profile(first_name=new_first, last_name=new_last)
        data["last_profile_key"] = profile_key


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
    await SAVED.ensure_user(user_id)
    client = Client(
        name=f"user_{user_id}_runtime",
        workers=1,
        in_memory=True,
        sleep_threshold=0,
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
    incoming_private = filters.private & filters.incoming & ~filters.me & ~filters.bot
    client.add_handler(MessageHandler(saved_new_message, incoming_private), group=-4)
    client.add_handler(EditedMessageHandler(saved_edited_message, incoming_private), group=-4)
    client.add_handler(RawUpdateHandler(saved_raw_update), group=-3)
    client.add_handler(MessageHandler(auto_read_message, filters.private & filters.incoming & ~filters.me), group=-1)
    try:
        authorized = await client.connect()
        if not authorized:
            raise Unauthorized()
        await client.invoke(functions.updates.GetState())
        client.me = await client.get_me()
        client.account_id = client.me.id
        await client.initialize()
        return client
    except BaseException:
        await close_pyrogram_client(client)
        raise

async def _persist_session_string(user_id, client):
    uid_str = str(user_id)
    session_string = await client.export_session_string()
    user_cfg = cached_config(uid_str)
    state = get_user_state(user_id)
    await purge_saved_session_data(user_id)
    state['session_string'] = session_string
    for key in ('session_invalid', 'session_backup_attempted', 'session_retry_at', 'session_error'):
        state.pop(key, None)
    user_cfg["session_string"] = session_string
    user_cfg["logged_in"] = True
    MEMORY_DB["config"][uid_str] = user_cfg
    await async_db_save("config", uid_str, user_cfg)
    return session_string

def cached_config(uid):
    return MEMORY_DB['config'].setdefault(str(uid), {})


def is_preview(uid):
    try:
        state = USER_DATA.get(int(uid), {}).get('state')
    except (TypeError, ValueError):
        state = None
    return state == 'PREVIEW' or not cached_config(uid).get('logged_in', False)


def entry_time_text(cfg):
    stamp = cfg.get('last_entry_ts')
    if stamp is not None:
        value = datetime.datetime.fromtimestamp(float(stamp), datetime.timezone.utc)
    else:
        try:
            value = datetime.datetime.strptime(cfg.get('last_entry_at', ''), '%d.%m.%Y %H:%M:%S').replace(tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            return 'Неизвестно'
    return (value + datetime.timedelta(hours=5)).strftime('%d.%m.%Y %H:%M:%S')


async def _activate_client(uid, client, session_string):
    data = get_user_state(uid)
    data['client'] = client
    data['session_string'] = session_string
    data['session_checked_at'] = time.monotonic()
    data.pop('session_retry_at', None)
    data.pop('session_error', None)
    data.pop('session_backup_attempted', None)
    data.pop('session_invalid', None)
    cfg = cached_config(uid)
    data['time_nick_active'] = bool(cfg.get('time_nick_active'))
    data['autoresponder_active'] = bool(cfg.get('autoresponder_active'))
    start_userbot_features(uid)
    if data['time_nick_active']:
        task = data.get('time_nick_task')
        if not task or task.done():
            data['time_nick_task'] = asyncio.create_task(time_nickname_loop(uid))
    return True


async def _drop_invalid_session(uid, reason):
    data = get_user_state(uid)
    cfg = cached_config(uid)
    for key in ('time_nick_task', 'saved_history_task', 'online_task', 'auto_read_offline_task'):
        task = data.get(key)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        data[key] = None
    client = data.pop('client', None)
    if client:
        await close_pyrogram_client(client)
    await clear_session_files(uid)
    keep = ('phone', 'username', 'first_name', 'entry_first_name', 'entry_username',
            'entry_phone', 'last_entry_at', 'last_entry_ts', 'ever_registered',
            'profile_base_first_name', 'profile_base_last_name', 'msg_id', 'timezone_offset',
            'registration_block_until_ts')
    clean = {key: cfg[key] for key in keep if key in cfg}
    clean.update(logged_in=False, saved_purge_pending=True, session_string=None, time_nick_active=False,
                 autoresponder_active=False, online_247=False, auto_read=False,
                 saved_messages_enabled=False, password='Нет')
    clean['entry_phone'] = clean.get('phone') or clean.get('entry_phone') or 'Не виден'
    if not clean.get('last_entry_at') and not clean.get('last_entry_ts'):
        clean['last_entry_ts'] = get_world_utc_timestamp()
    cfg.clear()
    cfg.update(clean)
    for key in ('session_string', 'phone_code_hash', 'password', 'saved_view',
                'saved_history_done', 'temp_greeting', 'last_profile_key', 'session_invalid',
                'session_backup_attempted', 'session_retry_at'):
        data.pop(key, None)
    data.update(client=None, state='PREVIEW', time_nick_active=False, autoresponder_active=False)
    data['session_error'] = reason
    MEMORY_DB['logs'][str(uid)] = []
    queue_db_save('logs', str(uid), [])
    await async_db_save('config', str(uid), cfg)
    await purge_saved_session_data(uid)
    try:
        await edit_or_send(uid, '⚠️ Сессия недействительна. Подключите аккаунт заново.',
                           reply_markup=get_missing_session_markup(uid))
    except Exception:
        pass
    return False


async def purge_saved_session_data(uid):
    cfg = cached_config(uid)
    if not cfg.get('saved_purge_pending'):
        return
    try:
        if await SAVED.ensure_user(uid):
            async with SAVED.lock:
                await SAVED._clear_locked(uid, saved_day(uid))
            if await SAVED.flush(uid):
                cfg.pop('saved_purge_pending', None)
                await persist_user_config_now(uid, cfg)
    except Exception as e:
        logging.warning('Session archive purge %s: %s', uid, type(e).__name__)


async def _recover_session_locked(uid, reason):
    data = get_user_state(uid)
    if time.monotonic() < data.get('session_retry_at', 0):
        return False
    if data.get('session_backup_attempted'):
        data['session_retry_at'] = time.monotonic() + 60
        return False
    try:
        fresh = await asyncio.to_thread(db_get_data, 'config', str(uid))
    except Exception:
        data['session_error'] = 'Не удалось проверить резервную сессию. Повторим позже.'
        data['session_retry_at'] = time.monotonic() + 60
        return False
    data['session_backup_attempted'] = True
    session = fresh.get('session_string') if fresh and fresh.get('logged_in') else None
    old = data.pop('client', None)
    if old:
        await close_pyrogram_client(old)
    if not session:
        if data.get('session_invalid') or not (data.get('session_string') or cached_config(uid).get('session_string')):
            return await _drop_invalid_session(uid, reason)
        data['session_error'] = 'Резервная копия пока недоступна. Сессия в памяти сохранена.'
        data['session_retry_at'] = time.monotonic() + 60
        return False
    try:
        client = await _build_runtime_client(uid, session)
    except (Unauthorized, ValueError, struct.error, binascii.Error):
        return await _drop_invalid_session(uid, reason)
    except FloodWait as e:
        data['session_retry_at'] = time.monotonic() + e.value + 1
        data['session_error'] = f'Пауза Telegram: {e.value} сек.'
    except Exception as e:
        data['session_retry_at'] = time.monotonic() + 60
        data['session_error'] = 'Telegram временно недоступен. Сессия сохранена.'
        logging.warning('Session recovery %s: %s', uid, type(e).__name__)
    else:
        cfg = cached_config(uid)
        cfg['session_string'] = session
        cfg['logged_in'] = True
        return await _activate_client(uid, client, session)
    data['session_string'] = session
    cached_config(uid)['session_string'] = session
    data['session_invalid'] = False
    return False


async def ensure_client_connected(user_id, force_check=False):
    data = get_user_state(user_id)
    async with data.setdefault('connect_lock', asyncio.Lock()):
        return await _ensure_client_connected(user_id, force_check)


async def _ensure_client_connected(uid, force_check=False):
    data = get_user_state(uid)
    cfg = cached_config(uid)
    if not cfg.get('logged_in') or SAVED.closing:
        return False
    if time.monotonic() < data.get('session_retry_at', 0):
        return False
    if data.get('session_invalid'):
        return await _recover_session_locked(uid, 'Telegram отклонил обе копии сессии.')
    client = data.get('client')
    session = data.get('session_string') or cfg.get('session_string')
    try:
        if client and client.is_connected:
            if force_check and time.monotonic() - data.get('session_checked_at', 0) >= 120:
                await client.get_me()
                data['session_checked_at'] = time.monotonic()
            start_userbot_features(uid)
            return True
        if client:
            await close_pyrogram_client(client)
            data['client'] = None
        if not session:
            return await _recover_session_locked(uid, 'Сохранённая сессия отсутствует.')
        client = await _build_runtime_client(uid, session)
        return await _activate_client(uid, client, session)
    except (Unauthorized, ValueError, struct.error, binascii.Error):
        data['session_invalid'] = True
        if data.get('session_backup_attempted'):
            return await _drop_invalid_session(uid, 'Telegram отклонил резервную сессию.')
        return await _recover_session_locked(uid, 'Telegram отклонил обе копии сессии.')
    except FloodWait as e:
        data['session_retry_at'] = time.monotonic() + e.value + 1
        data['session_error'] = f'Пауза Telegram: {e.value} сек.'
    except Exception as e:
        data['session_error'] = 'Временная ошибка соединения. Сессия сохранена.'
        logging.warning('Session check %s: %s', uid, type(e).__name__)
        if not data.get('session_backup_attempted'):
            return await _recover_session_locked(uid, data['session_error'])
        data['session_retry_at'] = time.monotonic() + 60
    return False


async def handle_revoked_session(user_id, reason='сессия была отозвана'):
    data = get_user_state(user_id)
    data['session_invalid'] = True
    task = data.get('session_repair_task')
    if not task or task.done():
        async def repair():
            await ensure_client_connected(user_id, force_check=True)
        data['session_repair_task'] = asyncio.create_task(repair())


async def session_recovery_loop():
    while True:
        await asyncio.sleep(60)
        for uid, cfg in list(MEMORY_DB['config'].items()):
            if uid.isdigit() and cfg.get('saved_purge_pending'):
                await purge_saved_session_data(int(uid))
            if uid.isdigit() and cfg.get('logged_in'):
                try:
                    await ensure_client_connected(int(uid), force_check=True)
                except Exception as e:
                    logging.warning('Session monitor %s: %s', uid, type(e).__name__)
                await asyncio.sleep(0.25)


async def restore_saved_sessions():
    rows = await asyncio.to_thread(db_get_all_config)
    restored = 0
    skipped = 0
    loaded = 0

    for row in rows:
        uid_str = str(row.get("id", "")).strip()
        cfg = row.get("data") or {}

        if not uid_str.isdigit() or not isinstance(cfg, dict):
            continue

        MEMORY_DB["config"][uid_str] = cfg
        loaded += 1
        if uid_str.isdigit():
            await SAVED.ensure_user(int(uid_str))

        if not cfg.get("logged_in") or not cfg.get("session_string"):
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

def stop_admin_server_stats_loop(user_id):
    data = get_user_state(user_id)
    data["admin_stats_active"] = False
    task = data.get("admin_stats_task")
    if task and not task.done():
        task.cancel()
    data["admin_stats_task"] = None


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
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
            "online_247": False, "auto_read": False,
            "autoresponder_greeting": get_text(user_id, "msg_autoresp_default"),
            "timezone_offset": 5,
            "used_timenick_seconds": 0.0,
            "registration_block_until_ts": 0.0,
            "replied_users": [], "autoresponder_last_replied": {},
            "profile_base_first_name": message.from_user.first_name or "User",
            "profile_base_last_name": message.from_user.last_name or "",
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
    if message.from_user:
        cfg["last_entry_ts"] = get_world_utc_timestamp()
        cfg["last_entry_at"] = get_world_utc_datetime().strftime("%d.%m.%Y %H:%M:%S")
        cfg["entry_first_name"] = message.from_user.first_name or "User"
        cfg["entry_username"] = message.from_user.username or "N/A"
        cfg["entry_phone"] = cfg.get("phone") if cfg.get("phone") not in (None, "", "Не указан") else "Не виден"
        MEMORY_DB["config"][uid_str] = cfg
        queue_db_save("config", uid_str, cfg)

    data["state"] = "ROOT"
    log_action(user_id, "Ввёл команду /start")
    await edit_or_send(user_id, "Главное меню:", reply_markup=root_menu_markup(user_id))


def root_menu_markup(user_id=None):
    builder = InlineKeyboardBuilder()
    builder.button(text="♨️UserBot", callback_data="userbot")
    builder.button(text="🔰Guard", callback_data="guard")
    if user_id == ADMIN_ID:
        builder.button(text="👑Admin", callback_data="admin_menu")
        builder.adjust(2, 1)
    else:
        builder.adjust(2)
    return builder.as_markup()


@dp.callback_query(F.data == "root_menu")
async def root_menu(callback: types.CallbackQuery):
    uid = callback.from_user.id
    get_user_state(uid)["state"] = "ROOT"
    await edit_or_send(uid, "Главное меню:", reply_markup=root_menu_markup(uid))
    await callback.answer()


def userbot_entry_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="Предпросмотр 👁", callback_data="userbot_preview")
    builder.button(text="Регистрация 📝", callback_data="start_re_register_menu")
    builder.button(text="Назад в меню 🏠", callback_data="root_menu")
    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data == "userbot")
async def open_userbot(callback: types.CallbackQuery):
    uid = callback.from_user.id
    cfg = cached_config(str(uid))

    # После регистрации никаких предпросмотров и промежуточных экранов:
    # сразу открываем настоящее меню UserBot с рабочими функциями.
    if cfg.get("logged_in", False):
        return await main_menu(callback)

    # Для незарегистрированных доступен входной экран с предпросмотром/регистрацией.
    get_user_state(uid)["state"] = "USERBOT_ENTRY"
    await edit_or_send(uid, "♨️UserBot", reply_markup=userbot_entry_markup())
    try:
        await callback.answer()
    except Exception:
        pass


@dp.callback_query(F.data == "userbot_preview")
async def open_userbot_preview(callback: types.CallbackQuery):
    uid = callback.from_user.id
    cfg = cached_config(str(uid))

    # Защита от старых inline-кнопок: зарегистрированный пользователь
    # никогда не попадает в режим предпросмотра.
    if cfg.get("logged_in", False):
        return await main_menu(callback)

    get_user_state(uid)["state"] = "PREVIEW"
    await edit_or_send(
        uid,
        "♨️UserBot — управление аккаунтом:",
        reply_markup=show_main_menu_builder(uid, user_obj=callback.from_user).as_markup()
    )
    try:
        await callback.answer()
    except Exception:
        pass

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
    await clear_session_files(user_id)
    uid_str = str(user_id)
    cfg = cached_config(uid_str)
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
        cfg = cached_config(uid_str)
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


        "password": "Нет",
        "time_nick_active": data["time_nick_active"],
        "autoresponder_active": data.get("autoresponder_active", old_cfg.get("autoresponder_active", False)),
        "online_247": bool(old_cfg.get("online_247", False)),
        "auto_read": bool(old_cfg.get("auto_read", False)),
        "autoresponder_greeting": old_cfg.get("autoresponder_greeting", get_text(user_id, "msg_autoresp_default")),
        "timezone_offset": old_cfg.get("timezone_offset", 5),
        "delete_today_count": old_cfg.get("delete_today_count", 0),
        "delete_limit_reset_ts": old_cfg.get("delete_limit_reset_ts", 0.0),
        "registration_block_until_ts": old_cfg.get("registration_block_until_ts", 0.0),
        "used_timenick_seconds": old_cfg.get("used_timenick_seconds", 0.0),
        "replied_users": old_cfg.get("replied_users", []),
        "autoresponder_last_replied": old_cfg.get("autoresponder_last_replied", {}),
        "profile_base_first_name": old_cfg.get("profile_base_first_name", message.from_user.first_name or "User"),
        "profile_base_last_name": old_cfg.get("profile_base_last_name", message.from_user.last_name or ""),
        "username": message.from_user.username or old_cfg.get("username", "N/A"),
        "first_name": message.from_user.first_name or old_cfg.get("first_name", "User"),
        "logged_in": is_logged_in,
        "ever_registered": True if is_logged_in else old_cfg.get("ever_registered", False),
        "last_entry_at": get_world_utc_datetime().strftime("%d.%m.%Y %H:%M:%S"),
        "last_entry_ts": get_world_utc_timestamp(),
        "entry_first_name": old_cfg.get("entry_first_name", message.from_user.first_name or "User"),
        "entry_username": old_cfg.get("entry_username", message.from_user.username or "N/A"),
        "entry_phone": old_cfg.get("entry_phone", "Не виден"),
        "msg_id": data.get("msg_id", old_cfg.get("msg_id", None)),
        "session_string": old_cfg.get("session_string")
    }
    MEMORY_DB["config"][uid_str] = cfg
    queue_db_save("config", uid_str, cfg)

async def build_2fa_password_prompt(user_id, client):

    text = get_text(user_id, "msg_pwd_req")
    hint = ""
    try:

        hint = (await client.get_password_hint() or "").strip()
    except FloodWait:

        pass
    except Exception as e:
        logging.debug("Не удалось получить 2FA-подсказку %s: %s", user_id, type(e).__name__)

    if hint:
        text += f"\n\nПодсказка: {hint}"
    return text


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
        start_userbot_features(user_id)
        save_user_config(user_id, message)
        data["state"] = "MENU"
        await edit_or_send(user_id, "♨️UserBot — управление аккаунтом:", reply_markup=show_main_menu_builder(user_id, user_obj=message.from_user).as_markup())
    except SessionPasswordNeeded:
        data["state"] = "WAITING_PASSWORD"
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        password_prompt = await build_2fa_password_prompt(user_id, client)
        await edit_or_send(user_id, password_prompt, reply_markup=builder.as_markup())
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
        start_userbot_features(user_id)
        save_user_config(user_id, message)
        data["state"] = "MENU"
        await edit_or_send(user_id, "♨️UserBot — управление аккаунтом:", reply_markup=show_main_menu_builder(user_id, user_obj=message.from_user).as_markup())
    except Exception:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_pwd_wrong"), reply_markup=builder.as_markup())

def show_main_menu_builder(user_id, user_obj: types.User = None):

    builder = InlineKeyboardBuilder()
    count = 0 if is_preview(user_id) else SAVED.unread_chat_count(user_id)
    suffix = f" ({count})" if count else ""
    builder.row(types.InlineKeyboardButton(
        text=f"Сохранённые сообщения{suffix} 🗂", callback_data="saved_menu"))
    builder.row(
        types.InlineKeyboardButton(text="Вечный онлайн 📊", callback_data="menu_online"),
        types.InlineKeyboardButton(text="Автопрочтение 👀", callback_data="menu_auto_read"),
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


@dp.callback_query(F.data == "menu_online")
async def menu_online(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not await ensure_client_connected(uid):
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    cfg = MEMORY_DB["config"].get(str(uid), {})
    active = cfg.get("online_247", False)
    text = "Вечный онлайн 📊:\n\nСтатус: " + ("🟢 Включен" if active else "🔴 Выключен")
    text += "\nПоддерживает статус вечного «в сети»."
    if get_user_state(uid).get("online_error") and active:
        text += "\n⚠️ " + get_user_state(uid)["online_error"]

    builder = InlineKeyboardBuilder()
    builder.button(text="🔴 Выключить" if active else "🟢 Включить", callback_data="toggle_247")
    builder.button(text="Назад в меню 🏠", callback_data="main_menu")
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
    text = "Автопрочтение 👀:\n\nСтатус: " + ("🟢 Включен" if active else "🔴 Выключен")
    text += "\nАвтоматически прочитает новые сообщения в ЛС."
    builder = InlineKeyboardBuilder()
    builder.button(text="🔴 Выключить" if active else "🟢 Включить", callback_data="toggle_auto_read")
    builder.button(text="Назад в меню 🏠", callback_data="main_menu")
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


SAVED_REMOTE_TABLE = "activity"
SAVED_FORMAT = "qwitty.saved.v1"
SAVED_CHAT_LIMIT = 100
SAVED_RAM_LIMIT = 24 * 1024 * 1024
SAVED_REMOTE_LIMIT = 24 * 1024 * 1024
SAVED_HISTORY_LOCK = asyncio.Lock()
SAVED_TASKS = []
SAVED_NOTIFICATIONS = asyncio.Queue(maxsize=256)


def saved_day(uid):
    offset = int(MEMORY_DB["config"].get(str(uid), {}).get("timezone_offset", 5))
    return (get_world_utc_datetime() + datetime.timedelta(hours=offset)).date().isoformat()


def saved_enabled(uid):
    cfg = MEMORY_DB["config"].get(str(uid), {})
    return bool(cfg.get("saved_messages_enabled") and cfg.get("logged_in") and not cfg.get("saved_purge_pending"))


def saved_pack(chat):
    return zlib.compress(json.dumps(chat, ensure_ascii=False, separators=(",", ":")).encode(), 3)


def saved_unpack(blob):
    return json.loads(zlib.decompress(blob))


def saved_trim(chat):

    while len(chat["base"]) + len(chat["events"]) > SAVED_CHAT_LIMIT:
        read = next((e for e in chat["events"] if e.get("read")), None)
        if read is not None:
            chat["events"].remove(read)
        elif chat["base"]:
            del chat["base"][min(chat["base"], key=int)]
        else:

            raise ValueError("Archive contains more than 100 pinned events")


class SavedMessageStore:
    def __init__(self, path=None):
        self.path = path or os.getenv("SAVED_MESSAGES_SQLITE", os.path.join(SESSIONS_DIR, "saved_messages.sqlite3"))
        self.db = None
        self.lock = asyncio.Lock()
        self.remote_lock = asyncio.Lock()
        self.load_locks = {}
        self.ready = set()
        self.days = {}
        self.revisions = {}
        self.remote_dirty = set()
        self.due = {}
        self.pending = {}
        self.pending_bytes = 0
        self.pending_index = {}
        self.summary = {}
        self.errors = {}
        self.retry_load_at = {}
        self.closing = False

    def _open_sync(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA cache_size=-2048")
        db.execute("PRAGMA secure_delete=ON")

        size = db.execute("PRAGMA page_size").fetchone()[0]
        db.execute(f"PRAGMA max_page_count={350 * 1024 * 1024 // size}")
        db.executescript('''
            CREATE TABLE IF NOT EXISTS saved_meta (
                uid INTEGER PRIMARY KEY, day TEXT NOT NULL, revision INTEGER NOT NULL,
                dirty INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS saved_chats (
                uid INTEGER NOT NULL, cid INTEGER NOT NULL, day TEXT NOT NULL,
                name TEXT NOT NULL, unread INTEGER NOT NULL, last REAL NOT NULL,
                payload BLOB NOT NULL, PRIMARY KEY(uid,cid));
            CREATE TABLE IF NOT EXISTS saved_index (
                uid INTEGER NOT NULL, mid INTEGER NOT NULL, cid INTEGER NOT NULL,
                PRIMARY KEY(uid,mid));
            CREATE INDEX IF NOT EXISTS saved_index_chat ON saved_index(uid,cid);
        ''')
        db.commit()
        return db

    async def open(self):
        async with self.lock:
            if self.db is None:
                self.db = await asyncio.to_thread(self._open_sync)

    async def _sql(self, fn, *args):


        task = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _load_local_sync(self, uid):
        meta = self.db.execute("SELECT day,revision,dirty FROM saved_meta WHERE uid=?", (uid,)).fetchone()
        rows = self.db.execute("SELECT cid,name,unread,last FROM saved_chats WHERE uid=?", (uid,)).fetchall()
        return meta, rows

    async def ensure_user(self, uid):
        uid = int(uid)
        if uid in self.ready:
            async with self.lock:
                await self._roll_locked(uid)
            return True
        if time.monotonic() < self.retry_load_at.get(uid, 0):
            return False
        await self.open()
        async with self.load_locks.setdefault(uid, asyncio.Lock()):
            if uid in self.ready:
                return True
            async with self.lock:
                meta, rows = await self._sql(self._load_local_sync, uid)
            today = saved_day(uid)
            remote = None

            if not meta or meta[0] != today:
                try:
                    remote = await async_db_get(SAVED_REMOTE_TABLE, str(uid))
                except Exception:
                    self.errors[uid] = "Не удалось загрузить архив. Повторяем подключение к базе."
                    self.retry_load_at[uid] = time.monotonic() + 30
                    return False
            async with self.lock:
                self.days[uid] = meta[0] if meta else today
                self.revisions[uid] = meta[1] if meta else 0
                self.summary[uid] = {cid: (name, unread, last) for cid, name, unread, last in rows}
                if meta and meta[2]:
                    self.remote_dirty.add(uid)
                if isinstance(remote, dict) and remote.get("format") == SAVED_FORMAT and remote.get("day") == today:
                    try:

                        decoded = []
                        total = 0
                        for cid, encoded in remote.get("chats", []):
                            blob = base64.b64decode(encoded, validate=True)
                            total += len(blob)
                            if total > SAVED_REMOTE_LIMIT:
                                raise ValueError("Archive too large")
                            unpacker = zlib.decompressobj()
                            raw = unpacker.decompress(blob, 8 * 1024 * 1024)
                            if not unpacker.eof:
                                raise ValueError("Invalid archive size")
                            chat = json.loads(raw)
                            if len(chat["base"]) + len(chat["events"]) > 100:
                                raise ValueError("Invalid chat limit")
                            decoded.append((int(cid), blob))
                        await self._clear_locked(uid, today)
                        for cid, blob in decoded:
                            self._put_locked(uid, cid, saved_unpack(blob))
                            await self._pressure_locked()
                        await self._flush_local_locked(uid)
                        self.remote_dirty.discard(uid)
                        await self._sql(self._mark_clean_sync, uid)
                        self.due[uid] = time.monotonic() + random.uniform(450, 510)
                    except Exception as e:
                        self.errors[uid] = "Архив не удалось восстановить. Сохранение приостановлено."
                        self.retry_load_at[uid] = time.monotonic() + 60
                        logging.warning("Saved archive restore %s: %s", uid, type(e).__name__)
                        return False
                else:
                    await self._roll_locked(uid)
                    if remote is not None and remote:

                        self.remote_dirty.add(uid)
                        self.due[uid] = time.monotonic()
                self.ready.add(uid)
                self.errors.pop(uid, None)
                self.due.setdefault(uid, time.monotonic() + random.uniform(450, 510))
                return True

    def _mark_clean_sync(self, uid):
        with self.db:
            self.db.execute("UPDATE saved_meta SET dirty=0 WHERE uid=?", (uid,))

    def _clear_sync(self, uid, day, revision):
        with self.db:
            self.db.execute("DELETE FROM saved_chats WHERE uid=?", (uid,))
            self.db.execute("DELETE FROM saved_index WHERE uid=?", (uid,))
            self.db.execute("INSERT OR REPLACE INTO saved_meta VALUES (?,?,?,1)", (uid, day, revision))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    async def _clear_locked(self, uid, day):
        for key in [k for k in self.pending if k[0] == uid]:
            self.pending_bytes -= len(self.pending.pop(key))
        self.pending_index.pop(uid, None)
        self.summary[uid] = {}
        self.days[uid] = day
        self.revisions[uid] = self.revisions.get(uid, 0) + 1
        self.remote_dirty.add(uid)
        self.due[uid] = time.monotonic()
        await self._sql(self._clear_sync, uid, day, self.revisions[uid])
        state = USER_DATA.get(uid, {})
        state.pop("saved_view", None)
        state.pop("saved_history_done", None)
        state["saved_ui_dirty"] = True

    async def _roll_locked(self, uid):
        if self.days.get(uid) != saved_day(uid):
            await self._clear_locked(uid, saved_day(uid))

    def _get_sync(self, uid, cid):
        row = self.db.execute("SELECT payload FROM saved_chats WHERE uid=? AND cid=?", (uid, cid)).fetchone()
        return row[0] if row else None

    async def _get_locked(self, uid, cid):
        blob = self.pending.get((uid, cid))
        if blob is None:
            blob = await self._sql(self._get_sync, uid, cid)
        return saved_unpack(blob) if blob else {"name": str(cid), "base": {}, "events": []}

    def _put_locked(self, uid, cid, chat):
        saved_trim(chat)
        key = (uid, cid)
        old = self.pending.get(key)
        if old:
            for mid in saved_unpack(old)["base"]:
                self.pending_index.setdefault(uid, {}).pop(int(mid), None)
        blob = saved_pack(chat)
        self.pending_bytes += len(blob) - len(old or b"")
        self.pending[key] = blob
        self.pending_index.setdefault(uid, {}).update({int(mid): cid for mid in chat["base"]})
        unread = [e for e in chat["events"] if not e.get("read")]
        old_summary = self.summary.setdefault(uid, {}).get(cid)
        summary = (chat["name"], len(unread), max((e["ts"] for e in unread), default=0))
        self.summary[uid][cid] = summary
        if old_summary != summary:
            USER_DATA.get(uid, {})["saved_ui_dirty"] = True
        self.revisions[uid] = self.revisions.get(uid, 0) + 1
        self.remote_dirty.add(uid)
        self.due.setdefault(uid, time.monotonic() + random.uniform(450, 510))

    def _flush_sync(self, pending, metas):
        with self.db:
            for (uid, cid), blob in pending.items():
                chat = saved_unpack(blob)
                unread = [e for e in chat["events"] if not e.get("read")]
                self.db.execute("INSERT OR REPLACE INTO saved_chats VALUES (?,?,?,?,?,?,?)",
                                (uid, cid, metas[uid][0], chat["name"], len(unread),
                                 max((e["ts"] for e in unread), default=0), blob))
                self.db.execute("DELETE FROM saved_index WHERE uid=? AND cid=?", (uid, cid))
                self.db.executemany("INSERT OR REPLACE INTO saved_index VALUES (?,?,?)",
                                    [(uid, int(mid), cid) for mid in chat["base"]])
            for uid, (day, revision, dirty) in metas.items():
                self.db.execute("INSERT OR REPLACE INTO saved_meta VALUES (?,?,?,?)", (uid, day, revision, dirty))

    async def _flush_local_locked(self, uid=None):
        pending = {k: v for k, v in self.pending.items() if uid is None or k[0] == uid}
        users = set(self.ready) if uid is None else {uid}
        users.update(k[0] for k in pending)
        metas = {u: (self.days[u], self.revisions[u], int(u in self.remote_dirty)) for u in users}
        await self._sql(self._flush_sync, pending, metas)
        for key, blob in pending.items():
            self.pending.pop(key, None)
            self.pending_bytes -= len(blob)
        for u in users:
            self.pending_index.pop(u, None)

    async def _pressure_locked(self):
        if self.pending_bytes >= SAVED_RAM_LIMIT:
            await self._flush_local_locked()

    def unread_chat_count(self, uid):
        if self.days.get(uid) != saved_day(uid):
            return 0
        return sum(1 for _, count, _ in self.summary.get(uid, {}).values() if count)

    async def chats(self, uid):
        if not await self.ensure_user(uid):
            return []
        async with self.lock:
            await self._roll_locked(uid)
            # Показываем все лички, где за текущий день есть сохранённые события.
            # unread теперь влияет только на уведомление/счётчик, а не на срок жизни архива.
            rows = []
            for cid, (name, unread, last_unread) in self.summary.get(uid, {}).items():
                chat = await self._get_locked(uid, cid)
                if not chat.get("events"):
                    continue
                last_event = max((float(e.get("ts", 0) or 0) for e in chat["events"]), default=0.0)
                rows.append((cid, name, unread, max(float(last_unread or 0), last_event)))
            return sorted(rows, key=lambda r: r[3], reverse=True)

    async def chat(self, uid, cid):
        if not await self.ensure_user(uid):
            return {"name": str(cid), "base": {}, "events": []}
        async with self.lock:
            await self._roll_locked(uid)
            return await self._get_locked(uid, cid)

    async def remember(self, uid, cid, name, records, day, seed=False):
        if not await self.ensure_user(uid):
            return
        async with self.lock:
            await self._roll_locked(uid)
            if not saved_enabled(uid) or day != self.days[uid]:
                return
            chat = await self._get_locked(uid, cid)
            chat["name"] = name
            event_mids = {e["mid"] for e in chat["events"]}
            for record in records:
                mid = str(record["mid"])

                if seed and (mid in chat["base"] or record["mid"] in event_mids):
                    continue
                if not seed and (mid in chat["base"] or any(
                        e["mid"] == record["mid"] and e["kind"] == "delete" for e in chat["events"])):
                    continue
                chat["base"][mid] = record
            self._put_locked(uid, cid, chat)
            await self._pressure_locked()

    async def edit(self, uid, cid, name, record):
        if not await self.ensure_user(uid):
            return None
        async with self.lock:
            await self._roll_locked(uid)
            if not saved_enabled(uid):
                return None
            chat = await self._get_locked(uid, cid)
            chat["name"] = name
            mid = str(record["mid"])
            old = chat["base"].get(mid)
            if not old and any(e["mid"] == record["mid"] and e["kind"] == "delete" for e in chat["events"]):
                return None
            if old and record["version"] < old["version"]:
                return None
            event = None
            if old and old["signature"] != record["signature"]:
                event = self._event(record["mid"], "edit", old["text"], record["text"])
                chat["events"].append(event)
            chat["base"][mid] = record
            self._put_locked(uid, cid, chat)
            await self._pressure_locked()
            return event

    @staticmethod
    def _event(mid, kind, before, after=None):
        return {"id": uuid.uuid4().hex[:16], "mid": mid, "kind": kind,
                "before": before, "after": after, "ts": get_world_utc_timestamp(), "read": False}

    def _find_sync(self, uid, mid):
        row = self.db.execute("SELECT cid FROM saved_index WHERE uid=? AND mid=?", (uid, mid)).fetchone()
        return row[0] if row else None

    async def delete(self, uid, mids):
        if not await self.ensure_user(uid):
            return []
        notices = []
        async with self.lock:
            await self._roll_locked(uid)
            if not saved_enabled(uid):
                return []
            for mid in mids:
                cid = self.pending_index.get(uid, {}).get(mid)
                if cid is None:
                    cid = await self._sql(self._find_sync, uid, mid)
                if cid is None:
                    continue
                chat = await self._get_locked(uid, cid)
                old = chat["base"].pop(str(mid), None)
                if old is None:
                    continue
                event = self._event(mid, "delete", old["text"])
                chat["events"].append(event)
                self._put_locked(uid, cid, chat)
                notices.append((cid, chat["name"], event, self.days[uid]))
            await self._pressure_locked()
        return notices

    async def mark_read(self, uid, cid, day, ids):
        if not await self.ensure_user(uid):
            return
        async with self.lock:
            await self._roll_locked(uid)
            if day != self.days[uid]:
                return
            chat = await self._get_locked(uid, cid)
            for event in chat["events"]:
                if event["id"] in ids:
                    event["read"] = True
            self._put_locked(uid, cid, chat)
            await self._pressure_locked()

    def _snapshot_sync(self, uid):
        rows = []
        size = 0
        for cid, blob in self.db.execute("SELECT cid,payload FROM saved_chats WHERE uid=? ORDER BY cid", (uid,)):
            encoded = base64.b64encode(blob).decode("ascii")
            size += len(encoded)
            if size > SAVED_REMOTE_LIMIT:
                raise ValueError("Сжатый архив аккаунта превысил безопасный размер резервной копии.")
            rows.append([cid, encoded])
        return rows

    async def flush(self, uid):
        async with self.remote_lock:
            async with self.lock:
                await self._roll_locked(uid)
                await self._flush_local_locked(uid)
                if uid not in self.remote_dirty:
                    return True
                day, revision = self.days[uid], self.revisions[uid]
                rows = await self._sql(self._snapshot_sync, uid)
                payload = {"format": SAVED_FORMAT, "day": day, "chats": rows}

            async with DB_WRITE_SEMAPHORE:
                request = asyncio.create_task(asyncio.to_thread(db_save_data, SAVED_REMOTE_TABLE, str(uid), payload))
                try:
                    ok = await asyncio.shield(request)
                except asyncio.CancelledError:

                    await request
                    raise
            async with self.lock:
                if ok:
                    self.errors.pop(uid, None)
                    if day == self.days[uid] and revision == self.revisions[uid]:
                        self.remote_dirty.discard(uid)
                        await self._sql(self._mark_clean_sync, uid)
                else:
                    self.errors[uid] = "Резервная копия пока не обновлена. Данные остаются на сервере; повторяем запись."

                self.due[uid] = (time.monotonic() if day != self.days[uid] else
                                 time.monotonic() + random.uniform(450, 510))
            return ok

    async def close(self):
        async with self.lock:
            await self._flush_local_locked()
            if self.db:
                await self._sql(self.db.close)
                self.db = None


SAVED = SavedMessageStore()


def saved_message_record(message):
    if (not message.chat or message.chat.type != enums.ChatType.PRIVATE
            or not message.from_user or message.from_user.is_self or message.from_user.is_bot
            or message.outgoing or message.service or message.empty):
        return None
    labels = {"sticker": "🎭 Стикер", "photo": "🖼 Фото", "video": "🎬 Видео",
              "voice": "🎤 Голосовое сообщение", "video_note": "📹 Видеосообщение",
              "audio": "🎵 Аудио", "animation": "🎞 GIF", "document": "📎 Файл",
              "contact": "👤 Контакт", "location": "📍 Геопозиция", "venue": "📍 Место",
              "poll": "📊 Опрос", "dice": "🎲 Кубик", "game": "🎮 Игра"}
    media_signature = ""
    label = ""
    for attr, value in labels.items():
        media = getattr(message, attr, None)
        if media:
            label = value
            detail = getattr(media, "file_name", None) or getattr(media, "emoji", None) or getattr(media, "question", None)
            if detail:
                label += " — " + str(detail)[:180]
            media_signature = str(getattr(media, "file_unique_id", None) or getattr(media, "id", None) or label)
            break
    text = str(message.text or message.caption or "")
    if label:
        text = label + ("\n" + text if text else "")
    text = text[:8192] or "📨 Сообщение без текста"

    signature = hashlib.sha256((text + "\0" + media_signature).encode()).hexdigest()
    date = message.edit_date or message.date
    if date and date.tzinfo is None:
        date = date.replace(tzinfo=datetime.timezone.utc)
    return {"mid": message.id, "text": text, "signature": signature,
            "version": date.timestamp() if date else 0}


def saved_peer_name(message):
    user = message.from_user
    return (" ".join(x for x in (user.first_name, user.last_name) if x) or user.username or str(user.id))[:100]


async def saved_new_message(client, message):
    uid = client.owner_id
    if SAVED.closing or not saved_enabled(uid):
        return
    try:
        record = saved_message_record(message)
        if record:
            await SAVED.remember(uid, message.chat.id, saved_peer_name(message), [record], saved_day(uid))
    except Exception as e:
        SAVED.errors[uid] = "Не удалось сохранить сообщение. Проверьте доступное место на сервере."
        logging.warning("Archive new %s: %s", uid, type(e).__name__)


async def saved_edited_message(client, message):
    uid = client.owner_id
    if SAVED.closing or not saved_enabled(uid):
        return
    try:
        record = saved_message_record(message)
        if record:
            name = saved_peer_name(message)
            event = await SAVED.edit(uid, message.chat.id, name, record)
            if event:
                try:
                    SAVED_NOTIFICATIONS.put_nowait((uid, message.chat.id, name, event, saved_day(uid)))
                except asyncio.QueueFull:
                    SAVED.errors[uid] = "Очередь уведомлений заполнена. Все изменения доступны в разделе «Лички»."
    except Exception as e:
        SAVED.errors[uid] = "Не удалось сохранить изменение. Проверьте доступное место на сервере."
        logging.warning("Archive edit %s: %s", uid, type(e).__name__)


async def saved_raw_update(client, update, users, chats):


    if SAVED.closing or not isinstance(update, raw_types.UpdateDeleteMessages) or not saved_enabled(client.owner_id):
        return
    uid = client.owner_id
    try:
        for cid, name, event, day in await SAVED.delete(uid, update.messages):
            try:
                SAVED_NOTIFICATIONS.put_nowait((uid, cid, name, event, day))
            except asyncio.QueueFull:
                SAVED.errors[uid] = "Очередь уведомлений заполнена. Все удаления доступны в разделе «Лички»."
    except Exception as e:
        SAVED.errors[uid] = "Не удалось обработать удаление. Проверьте доступное место на сервере."
        logging.warning("Archive deletion %s: %s", uid, type(e).__name__)


async def saved_history_request(factory):

    async with SAVED_HISTORY_LOCK:
        try:
            return await factory()
        finally:
            await asyncio.sleep(1.1)


async def saved_history_loop(uid):
    state = get_user_state(uid)
    day = saved_day(uid)
    client = state.get("client")
    if not client or not await SAVED.ensure_user(uid):
        return
    state["saved_history_loading"] = True
    state.pop("saved_history_error", None)
    completed = False
    partial = False
    try:
        dialogs = client.get_dialogs()
        while saved_enabled(uid) and day == saved_day(uid) and state.get("client") is client:
            try:
                dialog = await saved_history_request(lambda: anext(dialogs))
            except StopAsyncIteration:
                completed = True
                break
            chat = dialog.chat
            if chat.type != enums.ChatType.PRIVATE or chat.id == getattr(client, "account_id", uid):
                continue
            count, offset_id = 0, 0
            name = (" ".join(x for x in (chat.first_name, chat.last_name) if x) or chat.username or str(chat.id))[:100]
            while (count < SAVED_CHAT_LIMIT and saved_enabled(uid) and day == saved_day(uid)
                   and state.get("client") is client):
                async def fetch_page():
                    return [m async for m in client.get_chat_history(chat.id, limit=100, offset_id=offset_id)]
                try:
                    messages = await saved_history_request(fetch_page)
                except FloodWait as e:
                    state["saved_history_error"] = f"Загрузка истории на паузе Telegram: {e.value} сек."
                    await asyncio.sleep(e.value + 1)
                    continue
                except Unauthorized:
                    raise
                except Exception as e:
                    partial = True
                    logging.warning("Archive history peer %s/%s: %s", uid, chat.id, type(e).__name__)
                    state["saved_history_error"] = "Часть личек пока недоступна. Остальные продолжают сохраняться."
                    break
                if not messages:
                    break
                records = []
                for message in messages:
                    record = saved_message_record(message)
                    if record:
                        records.append(record)
                        count += 1
                        if count >= SAVED_CHAT_LIMIT:
                            break
                await SAVED.remember(uid, chat.id, name, records, day, seed=True)
                next_offset = messages[-1].id
                if next_offset == offset_id or len(messages) < 100:
                    break
                offset_id = next_offset
        if completed and day == saved_day(uid):
            if partial:
                state["saved_history_retry_at"] = time.monotonic() + 300
            else:
                state["saved_history_done"] = day
                state.pop("saved_history_error", None)
    except asyncio.CancelledError:
        raise
    except FloodWait as e:
        state["saved_history_retry_at"] = time.monotonic() + e.value + 1
        state["saved_history_error"] = f"Загрузка истории на паузе Telegram: {e.value} сек."
    except Unauthorized:
        await handle_revoked_session(uid)
        state["saved_history_retry_at"] = time.monotonic() + 60
        state["saved_history_error"] = "Сессия Telegram недоступна. Подключите аккаунт повторно."
    except Exception as e:
        state["saved_history_retry_at"] = time.monotonic() + 60
        state["saved_history_error"] = "История загружена частично. Продолжим автоматически."
        logging.warning("Archive history %s: %s", uid, type(e).__name__)
    finally:
        state["saved_history_loading"] = False
        state["saved_ui_dirty"] = True


def start_saved_history(uid):
    state = get_user_state(uid)
    client = state.get("client")
    task = state.get("saved_history_task")
    if (not SAVED.closing and saved_enabled(uid) and client and client.is_connected
            and (not task or task.done()) and state.get("saved_history_done") != saved_day(uid)
            and time.monotonic() >= state.get("saved_history_retry_at", 0)):
        state["saved_history_task"] = asyncio.create_task(saved_history_loop(uid))


def saved_clip(text, units):
    raw = str(text).encode("utf-16-le")
    return str(text) if len(raw) <= units * 2 else raw[:(units - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


async def delete_saved_notification_later(chat_id, message_id, delay=300):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        pass
    except Exception as e:
        logging.debug("Archive notification auto-delete %s/%s: %s", chat_id, message_id, type(e).__name__)


async def saved_notification_loop():
    while True:
        uid, cid, name, event, day = await SAVED_NOTIFICATIONS.get()
        try:

            while day == saved_day(uid):
                try:
                    markup = InlineKeyboardBuilder()
                    markup.button(text="Окей ✅", callback_data="saved_ok")

                    if event.get("kind") == "edit":
                        notification_text = (
                            f"✏️ В личке с {saved_clip(name, 100)} отредактировано входящее сообщение:\n\n"
                            f"Было:\n«{saved_clip(event.get('before', ''), 1700)}»\n\n"
                            f"Стало:\n«{saved_clip(event.get('after', ''), 1700)}»"
                        )
                    else:
                        notification_text = (
                            f"🗑 В личке с {saved_clip(name, 100)} удалено входящее сообщение:\n\n"
                            f"«{saved_clip(event.get('before', ''), 3500)}»"
                        )

                    notice = await bot.send_message(
                        uid,
                        notification_text,
                        reply_markup=markup.as_markup(),
                        parse_mode=None
                    )
                    asyncio.create_task(delete_saved_notification_later(uid, notice.message_id, 300))
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                except Exception as e:
                    SAVED.errors[uid] = "Уведомление не доставлено. Событие доступно в разделе «Лички»."
                    logging.warning("Archive notification %s: %s", uid, type(e).__name__)
                    break
            await asyncio.sleep(1.1)
        finally:
            SAVED_NOTIFICATIONS.task_done()


async def saved_writer_loop():
    while True:
        due = sorted((u for u in SAVED.ready if u in SAVED.remote_dirty), key=lambda u: SAVED.due.get(u, 0))
        for uid in due:
            if time.monotonic() < SAVED.due.get(uid, 0):
                continue
            try:
                await SAVED.flush(uid)
            except Exception as e:
                SAVED.errors[uid] = "Архив пока не записан. Повторим попытку; проверьте место в базе."
                SAVED.due[uid] = time.monotonic() + 60
                logging.warning("Archive flush %s: %s", uid, type(e).__name__)
            await asyncio.sleep(1.1)
        await asyncio.sleep(1)


async def saved_maintenance_loop():
    while True:
        for uid in list(SAVED.ready):
            try:
                async with SAVED.lock:
                    await SAVED._roll_locked(uid)
                start_saved_history(uid)
                await saved_refresh_visible(uid)
            except Exception as e:
                logging.warning("Archive maintenance %s: %s", uid, type(e).__name__)

        for uid_text in list(MEMORY_DB["config"]):
            if uid_text.isdigit() and int(uid_text) not in SAVED.ready:
                uid = int(uid_text)
                if time.monotonic() >= SAVED.retry_load_at.get(uid, 0):
                    await SAVED.ensure_user(uid)
                    start_saved_history(uid)
        await asyncio.sleep(1)


def start_saved_service():
    if SAVED_TASKS:
        return
    SAVED.closing = False
    SAVED_TASKS.extend(asyncio.create_task(coro()) for coro in
                       (saved_writer_loop, saved_maintenance_loop, saved_notification_loop))


async def stop_saved_service():
    SAVED.closing = True

    for task in SAVED_TASKS:
        task.cancel()
    await asyncio.gather(*SAVED_TASKS, return_exceptions=True)
    SAVED_TASKS.clear()
    for uid in list(SAVED.ready):
        try:
            await SAVED.flush(uid)
        except Exception as e:
            logging.warning("Archive final flush %s: %s", uid, type(e).__name__)
    await SAVED.close()


def saved_menu_content(uid):
    active = saved_enabled(uid)
    count = SAVED.unread_chat_count(uid)
    text = ("🗂 Сохранение удалённых и отредактированных сообщений\n"
            "Действует только в личных чатах 👤\n\n"
            f"Статус: {'Включено 🟢' if active else 'Выключено 🔴'}\n\n"
            "🕛 Архив очищается в 00:00 по часовому поясу аккаунта.\n"
            "Учитываются только сообщения собеседников, до 100 записей на личку.")
    if not active:
        text += "\nПосле выключения сохранённые записи доступны до полуночи."
    state = get_user_state(uid)
    if state.get("saved_history_loading"):
        text += "\n⏳ Последние сообщения загружаются по очереди. Новые уже сохраняются."
    for error in (state.get("saved_history_error"), SAVED.errors.get(uid)):
        if error:
            text += "\n⚠️ " + error
    builder = InlineKeyboardBuilder()
    builder.button(text="Выключить 🔴" if active else "Включить 🟢", callback_data="saved_toggle")
    builder.button(text=f"Лички ({count}) 🗣", callback_data="saved_chats:0")
    builder.button(text="Назад ⬅️", callback_data="main_menu")
    builder.adjust(1)
    return text, builder.as_markup()


def saved_page_row(builder, page, pages, prefix):
    if pages <= 1:
        return
    builder.row(
        types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"{prefix}{page - 1}" if page else "ignore"),
        types.InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="ignore"),
        types.InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"{prefix}{page + 1}" if page + 1 < pages else "ignore"),
    )


async def saved_render_chats(uid, page=0):
    rows = await SAVED.chats(uid)
    pages = max(1, (len(rows) + 4) // 5)
    page = min(max(page, 0), pages - 1)
    builder = InlineKeyboardBuilder()
    for cid, name, count, _ in rows[page * 5:page * 5 + 5]:
        builder.row(types.InlineKeyboardButton(text=f"{saved_clip(name, 45)} ({count})",
                                              callback_data=f"saved_chat:{cid}"))
    saved_page_row(builder, page, pages, "saved_chats:")
    builder.row(types.InlineKeyboardButton(text="Назад ⬅️", callback_data="saved_menu"))
    state = get_user_state(uid)
    state["saved_screen"] = ("chats", page)
    text = "Лички 🗣" if rows else "Лички 🗣\n\nСохранённых удалений и изменений за сегодня пока нет ✨"
    await edit_or_send(uid, text, reply_markup=builder.as_markup())


async def saved_render_view(uid, token, page):
    state = get_user_state(uid)
    view = state.get("saved_view")
    if not view or view["token"] != token or view["day"] != saved_day(uid):
        await saved_render_chats(uid)
        return
    chat = await SAVED.chat(uid, view["cid"])
    lookup = {e["id"]: e for e in chat["events"]}
    events = [lookup[eid] for eid in view["ids"] if eid in lookup]
    pages = max(1, (len(events) + 4) // 5)
    page = min(max(0, page), pages - 1)
    selected = events[page * 5:page * 5 + 5]
    offset = int(MEMORY_DB["config"].get(str(uid), {}).get("timezone_offset", 5))
    lines = [f"👤 Личка с {saved_clip(chat['name'], 90)}:", ""]
    for number, event in enumerate(selected, page * 5 + 1):
        stamp = (datetime.datetime.fromtimestamp(event["ts"], datetime.timezone.utc)
                 + datetime.timedelta(hours=offset)).strftime("%d.%m.%Y — %H:%M")

        lines.append(f"{number}) {saved_clip(chat['name'], 40)}: «{saved_clip(event['before'], 255)}»")
        if event["kind"] == "delete":
            lines.append(f"🗑 Удалено — {stamp}")
        else:
            lines.append(f"✏️ Изменено на «{saved_clip(event['after'], 255)}» — {stamp}")
        lines.append("")
    if not selected:
        lines.append("Архив очищен или записи уже недоступны ✨")
    builder = InlineKeyboardBuilder()
    saved_page_row(builder, page, pages, f"saved_page:{token}:")
    for i, event in enumerate(selected, page * 5 + 1):
        if len(event["before"].encode("utf-16-le")) > 510 or len((event.get("after") or "").encode("utf-16-le")) > 510:
            builder.row(types.InlineKeyboardButton(text=f"📄 Полный текст №{i}",
                        callback_data=f"saved_full:{token}:{event['id']}:0"))
    builder.row(types.InlineKeyboardButton(text="Назад к личкам ⬅️", callback_data=f"saved_back:{token}"))
    delivered = await edit_or_send(uid, "\n".join(lines), reply_markup=builder.as_markup())
    if delivered:
        view["seen"].update(e["id"] for e in selected)
    view["page"] = page
    state["saved_screen"] = ("view", token)


async def saved_refresh_visible(uid):
    state = USER_DATA.get(uid, {})
    if not state.get("saved_ui_dirty") or not state.get("msg_id"):
        return
    if time.monotonic() < state.get("saved_refresh_at", 0):
        return
    lock = state.setdefault("ui_lock", asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        state["saved_ui_dirty"] = False
        state["saved_refresh_at"] = time.monotonic() + 3
        markup = state.get("last_ui_reply_markup")
        callbacks = {b.callback_data for row in getattr(markup, "inline_keyboard", []) for b in row}
        if "saved_menu" in callbacks and "menu_online" in callbacks:
            await edit_or_send(uid, state.get("last_ui_text") or "♨️ UserBot — управление аккаунтом:",
                               reply_markup=show_main_menu_builder(uid).as_markup())
        elif "saved_toggle" in callbacks:
            text, keyboard = saved_menu_content(uid)
            await edit_or_send(uid, text, reply_markup=keyboard)
        elif any(c and c.startswith("saved_back:") for c in callbacks):

            if not state.get("saved_view"):
                await saved_render_chats(uid)
        elif "saved_menu" in callbacks:
            screen = state.get("saved_screen", ("chats", 0))
            await saved_render_chats(uid, screen[1] if screen[0] == "chats" else 0)


@dp.callback_query(F.data == "saved_ok")
async def saved_ok(callback: types.CallbackQuery):
    if callback.message and callback.message.chat.id == callback.from_user.id:
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass
    await callback.answer()


@dp.callback_query(F.data == "saved_menu")
async def saved_menu(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    await SAVED.ensure_user(uid)
    text, keyboard = saved_menu_content(uid)
    await edit_or_send(uid, text, reply_markup=keyboard)


@dp.callback_query(F.data == "saved_toggle")
async def saved_toggle(callback: types.CallbackQuery):
    uid = callback.from_user.id
    cfg = MEMORY_DB["config"].setdefault(str(uid), {})
    enabling = not cfg.get("saved_messages_enabled", False)
    if enabling and cfg.get('saved_purge_pending'):
        await purge_saved_session_data(uid)
        if cfg.get('saved_purge_pending'):
            await callback.answer('Очистка старого архива ещё не завершена. Повторите позже.', show_alert=True)
            return
    if enabling:
        if not await ensure_client_connected(uid):
            await callback.answer("Сначала подключите аккаунт.", show_alert=True)
            return
        if not await SAVED.ensure_user(uid):
            await callback.answer("Архив пока недоступен. Попробуйте чуть позже.", show_alert=True)
            return
    cfg["saved_messages_enabled"] = enabling
    await persist_user_config_now(uid, cfg)
    state = get_user_state(uid)
    if enabling:
        state.pop("saved_history_done", None)
        start_saved_history(uid)
    else:
        task = state.get("saved_history_task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await saved_menu(callback)


@dp.callback_query(F.data.startswith("saved_chats:"))
async def saved_chats_callback(callback: types.CallbackQuery):
    await callback.answer()
    try:
        page = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await saved_render_chats(callback.from_user.id, page)


@dp.callback_query(F.data.startswith("saved_chat:"))
async def saved_chat_callback(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    try:
        cid = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        return
    chat = await SAVED.chat(uid, cid)
    events = sorted(chat["events"], key=lambda e: e["ts"], reverse=True)
    if not events:
        await saved_render_chats(uid)
        return

    # Открытие конкретной лички снимает только её уведомление/счётчик.
    # Сами события НЕ удаляются и остаются доступными до смены дня (00:00).
    unread_ids = {e["id"] for e in events if not e.get("read")}
    if unread_ids:
        await SAVED.mark_read(uid, cid, saved_day(uid), unread_ids)

    state = get_user_state(uid)
    token = uuid.uuid4().hex[:8]
    state["saved_view"] = {"token": token, "cid": cid, "day": saved_day(uid),
                           "ids": [e["id"] for e in events], "seen": set(), "page": 0}
    await saved_render_view(uid, token, 0)


@dp.callback_query(F.data.startswith("saved_page:"))
async def saved_page_callback(callback: types.CallbackQuery):
    await callback.answer()
    try:
        _, token, page = callback.data.split(":")
        await saved_render_view(callback.from_user.id, token, int(page))
    except (ValueError, IndexError):
        return


@dp.callback_query(F.data.startswith("saved_back:"))
async def saved_back_callback(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    state = get_user_state(uid)
    view = state.get("saved_view")
    if view and view["token"] == callback.data.split(":")[-1]:
        # Уже отмечено прочитанным при открытии лички. Архив сохраняем до полуночи.
        state.pop("saved_view", None)
    await saved_render_chats(uid)


@dp.callback_query(F.data.startswith("saved_full:"))
async def saved_full_callback(callback: types.CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    try:
        _, token, eid, part = callback.data.split(":")
        part = max(0, int(part))
    except (ValueError, IndexError):
        return
    view = get_user_state(uid).get("saved_view")
    if not view or view["token"] != token or view["day"] != saved_day(uid) or eid not in view["ids"]:
        await saved_render_chats(uid)
        return
    chat = await SAVED.chat(uid, view["cid"])
    event = next((e for e in chat["events"] if e["id"] == eid), None)
    if not event:
        await saved_render_view(uid, token, view["page"])
        return
    text = "📄 Исходное сообщение:\n" + event["before"]
    if event["after"] is not None:
        text += "\n\n✏️ Изменено на:\n" + event["after"]

    parts = [text[i:i + 1500] for i in range(0, len(text), 1500)]
    part = min(part, len(parts) - 1)
    builder = InlineKeyboardBuilder()
    saved_page_row(builder, part, len(parts), f"saved_full:{token}:{eid}:")
    builder.row(types.InlineKeyboardButton(text="К сообщениям ⬅️", callback_data=f"saved_page:{token}:{view['page']}"))
    builder.row(types.InlineKeyboardButton(text="Назад к личкам ⬅️", callback_data=f"saved_back:{token}"))
    await edit_or_send(uid, parts[part], reply_markup=builder.as_markup())


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
    cfg = cached_config(uid_str)
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
    cfg = cached_config(uid_str)

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
    if is_preview(user_id):
        data['state'] = 'PREVIEW'
        await edit_or_send(user_id, 'Сначала подключите аккаунт 👤', reply_markup=get_missing_session_markup(user_id))
        return
    new_text = message.text.strip() if message.text else ""

    if new_text:
        uid_str = str(user_id)
        cfg = cached_config(uid_str)
        cfg["autoresponder_greeting"] = new_text
        await persist_user_config_now(user_id, cfg)
        log_action(user_id, "Изменён текст автоответчика")

    data["state"] = "MENU"
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_autoresponder")
    await edit_or_send(user_id, get_text(user_id, "msg_autoresp_saved"), reply_markup=builder.as_markup())

@dp.callback_query(F.data == "menu_timenick")
async def menu_timenick(callback: types.CallbackQuery, sync_base=True):
    user_id = callback.from_user.id
    is_valid = await ensure_client_connected(user_id)
    if not is_valid:
        await edit_or_send(user_id, get_text(user_id, "msg_session_missing"), reply_markup=get_missing_session_markup(user_id))
        try: await callback.answer()
        except Exception: pass
        return

    uid_str = str(user_id)
    cfg = cached_config(uid_str)
    if sync_base:
        cfg = await sync_profile_base_from_telegram(user_id, cfg, persist=True)
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
    cfg = await sync_profile_base_from_telegram(uid, cfg, persist=True)
    cfg["time_style"] = style
    await persist_user_config_now(uid, cfg)
    await callback.answer()
    if cfg.get("time_nick_active"):
        await update_profile_branding(uid, sync_base=False)
    await menu_timenick(callback, sync_base=False)


@dp.callback_query(F.data == "toggle_timenick")
async def toggle_timenick(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    uid_str = str(user_id)
    cfg = cached_config(uid_str)

    if not await ensure_client_connected(user_id):
        await callback.answer("Сначала подключите аккаунт.", show_alert=True)
        return
    cfg = await sync_profile_base_from_telegram(user_id, cfg, persist=True)
    new_status = not cfg.get("time_nick_active", False)
    cfg["time_nick_active"] = new_status
    data["time_nick_active"] = new_status
    await persist_user_config_now(user_id, cfg)

    if new_status:
        if not data.get("time_nick_task") or data["time_nick_task"].done():
            data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
        asyncio.create_task(update_profile_branding(user_id, sync_base=False))
    else:
        if data.get("time_nick_task"):
            data["time_nick_task"].cancel()
            data["time_nick_task"] = None
        if data.get("client") and data["client"].is_connected:
            try:
                base_first = cfg.get("profile_base_first_name", "User")
                base_last = cfg.get("profile_base_last_name", "")
                await data["client"].update_profile(first_name=base_first, last_name=base_last)
                data.pop("last_profile_key", None)


            except Exception as e:
                logging.error(f"Ошибка сброса имени профиля: {e}")

    log_action(user_id, f"Время в профиле: {'Включено' if new_status else 'Выключено'}")
    await maybe_recreate_ui(callback)
    await menu_timenick(callback, sync_base=False)

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
    cfg = cached_config(uid_str)
    if await ensure_client_connected(user_id):
        cfg = await sync_profile_base_from_telegram(user_id, cfg, persist=True)
    cfg["timezone_offset"] = tz_val
    await persist_user_config_now(user_id, cfg)

    sign_str = f"+{tz_val}" if tz_val >= 0 else str(tz_val)
    log_action(user_id, f"Изменён часовой пояс: UTC{sign_str}")

    if cfg.get("time_nick_active", False):
        asyncio.create_task(update_profile_branding(user_id, sync_base=False))

    await menu_timenick(callback, sync_base=False)


@dp.callback_query(F.data == "ignore")
async def ignore_callback(callback: types.CallbackQuery):
    try: await callback.answer()
    except Exception: pass


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

        return None, "rpc_error"


async def refresh_server_stats_cache(force=False):

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


async def preview_registration(callback):
    uid = callback.from_user.id
    get_user_state(uid)['state'] = 'PREVIEW_REGISTER'
    builder = InlineKeyboardBuilder()
    builder.button(text='Правила и регистрация 📝', callback_data='start_re_register_menu')
    builder.button(text='Назад в главное меню 🏠', callback_data='root_menu')
    builder.adjust(1)
    await edit_or_send(uid, 'Для включения функций подключите свой Telegram-аккаунт 👤', reply_markup=builder.as_markup())
    await callback.answer()


async def render_userbot_preview(callback):
    uid = callback.from_user.id
    action = callback.data
    get_user_state(uid)['state'] = 'PREVIEW'
    builder = InlineKeyboardBuilder()
    if action in ('userbot_preview', 'main_menu'):
        text = '♨️UserBot — управление аккаунтом:'
        builder = show_main_menu_builder(uid)
    elif action == 'saved_menu':
        text = ('🗂 Сохранение удалённых и отредактированных сообщений\n'
                'Действует только в личных чатах 👤\n\nСтатус: Выключено 🔴')
        builder.button(text='Включить 🟢', callback_data='saved_toggle')
        builder.button(text='Лички (0) 🗣', callback_data='saved_chats:0')
        builder.button(text='Назад ⬅️', callback_data='main_menu')
        builder.adjust(1)
    elif action.startswith(('saved_chats:', 'saved_chat:', 'saved_page:', 'saved_back:', 'saved_full:')):
        text = 'Лички 🗣\n\nСохранённых сообщений пока нет ✨'
        builder.button(text='Назад ⬅️', callback_data='saved_menu')
    elif action in ('menu_online', 'menu_auto_read'):
        online = action == 'menu_online'
        text = ('Вечный онлайн 📊' if online else 'Автопрочтение 👀') + '\n\nСтатус: Выключено 🔴'
        builder.button(text='Включить 🟢', callback_data='toggle_247' if online else 'toggle_auto_read')
        builder.button(text='Назад ⬅️', callback_data='main_menu')
        builder.adjust(1)
    elif action == 'menu_autoresponder':
        text = get_text(uid, 'msg_autoresp_text', get_text(uid, 'msg_autoresp_default'), 'Выключен 🔴')
        builder.button(text='Включить 🟢', callback_data='toggle_autoresponder')
        builder.button(text='Изменить текст ✏️', callback_data='autoresp_setup')
        builder.button(text='Назад ⬅️', callback_data='main_menu')
        builder.adjust(1)
    elif action == 'menu_timenick':
        text = 'Время в профиле ⏰\n\nСтатус: Выключено 🔴\nЧасовой пояс: UTC+5'
        for label, cb in [('Включить 🟢', 'toggle_timenick'), ('Выбрать часовой пояс 🌐', 'tz_select'),
                          ('Стили 🎨', 'time_styles'), ('Назад ⬅️', 'main_menu')]:
            builder.button(text=label, callback_data=cb)
        builder.adjust(1)
    elif action == 'tz_select':
        text = 'Выберите часовой пояс 🌐'
        for offset, name in TIMEZONE_NAMES.items():
            builder.button(text=name, callback_data=f'set_tz_{offset}')
        builder.adjust(2)
        builder.row(types.InlineKeyboardButton(text='Назад ⬅️', callback_data='menu_timenick'))
    elif action == 'time_styles' or action.startswith('time_styles_page_'):
        try:
            page = int(action.rsplit('_', 1)[-1]) if action.startswith('time_styles_page_') else 0
        except ValueError:
            page = 0
        text = 'Стили времени 🎨'
        clock = (get_world_utc_datetime() + datetime.timedelta(hours=5)).strftime('%H:%M')
        await edit_or_send(uid, text, reply_markup=build_time_styles_markup(clock, page))
        await callback.answer()
        return
    else:
        await preview_registration(callback)
        return
    await edit_or_send(uid, text, reply_markup=builder.as_markup())
    await callback.answer()


def preview_action(action):
    return action in {'userbot_preview', 'main_menu', 'menu_online', 'menu_auto_read',
                      'menu_autoresponder', 'menu_timenick', 'autoresp_setup', 'tz_select',
                      'time_styles', 'saved_menu', 'saved_toggle'} or action.startswith(
                      ('toggle_', 'set_tz_', 'time_style_', 'time_styles_page_', 'saved_chats:',
                       'saved_chat:', 'saved_page:', 'saved_back:', 'saved_full:'))

