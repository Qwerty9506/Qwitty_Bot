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
from aiohttp import web

if sys.platform != "win32":
    try:
        import uvloop
        uvloop.install()
        print("🔥 [Движок]: uvloop успешно активирован (Linux/macOS)")
    except ImportError:
        print("⚠️ [Движок]: uvloop не установлен, используется стандартный asyncio")
else:
    print("💻 Запуск Скрипта")

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

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# Инициализация Supabase
supabase: SupabaseClient = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("⚡ Supabase успешно подключен")
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
    # +0.999 превращает время в понятный потолок до следующей полной секунды.
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
    "btn_style_select": "Выбрать стиль 🍃",
    "btn_style_nickname_revert": "Откатить для никнейма",
    "btn_style_nickname_apply": "Применить для никнейма",
    "btn_style_time_revert": "Откатить для время в профиль",
    "btn_style_time_apply": "Применить для время в профиль",
    "btn_refresh": "Обновить 🔄",
    "btn_autoresp_setup": "Изменить текст 📝",
    "btn_im_sure": "Я уверен ✅", 
    "btn_register": "Регистрироваться 📝",
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
    "msg_timenick_text": "Вывод текущего времени в имя профиля.\n\nТекущий статус: {0}\nСтиль: {1}\nСмещение часового пояса: UTC+{2}",
    "msg_tz_select": "Выберите ваш часовой пояс👇", 
    "msg_tz_saved": "Часовой пояс изменен на UTC+{0}!",
    "msg_style_select": "Стиль по вашему выбору 👇",
    "msg_style_preview": "Выбран стиль:\n\n{0}",
    "msg_autoresp_text": "🤖 **Автоответчик**\n\nСтатус: {1}\nТекст приветствия:\n👉 \"{0}\"",
    "msg_autoresp_req": "Напишите новый текст приветствия в чат 👇", 
    "msg_autoresp_saved": "Приветствие успешно сохранено! ✅",
    "msg_autoresp_default": "👋 Здравствуйте! Сейчас я не в сети, отвечу позже.",
    "msg_247_text": "⚡️ **Режим 24/7**\n\nСтатус: {0}\nРаботает без суточного лимита.",
    "msg_limit_247_reached": "Режим 24/7 больше не имеет суточного лимита."
}


# === СТИЛИ ИМЕНИ/ВРЕМЕНИ ===
# 7 реально разных популярных Unicode-стилей.
STYLE_DEFINITIONS = [
    ("ᴠᴇɴᴏᴍ", "smallcaps"),       # 1
    ("𝔭𝔥𝔞𝔫𝔱𝔬𝔪", "fraktur"),       # 2
    ("𝐒𝐢𝐥𝐞𝐧𝐜𝐞", "bold"),          # 3
    ("𝑆𝑖𝑙𝑒𝑛𝑐𝑒", "italic"),          # 4
    ("𝕊𝕚𝕝𝕖𝕟𝕔𝕖", "double"),         # 5
    ("𝗦𝗶𝗹𝗲𝗻𝗰𝗲", "sans_bold"),      # 6
    ("𝚂𝚒𝚕𝚎𝚗𝚌𝚎", "monospace"),      # 7
]

_ASCII = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_STYLE_ALPHABETS = {
    "bold": (
        "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙",
        "𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳",
    ),
    "italic": (
        "𝐴𝐵𝐶𝐷𝐸𝐹𝐺𝐻𝐼𝐽𝐾𝐿𝑀𝑁𝑂𝑃𝑄𝑅𝑆𝑇𝑈𝑉𝑊𝑋𝑌𝑍",
        "𝑎𝑏𝑐𝑑𝑒𝑓𝑔ℎ𝑖𝑗𝑘𝑙𝑚𝑛𝑜𝑝𝑞𝑟𝑠𝑡𝑢𝑣𝑤𝑥𝑦𝑧",
    ),
    "double": (
        "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ",
        "𝕒𝕓𝕔𝕕𝕖𝕗𝕘𝕙𝕚𝕛𝕜𝕝𝕞𝕟𝕠𝕡𝕢𝕣𝕤𝕥𝕦𝕧𝕨𝕩𝕪𝕫",
    ),
    "sans_bold": (
        "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭",
        "𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘸𝘹𝘺𝘇",
    ),
    "monospace": (
        "𝙰𝙱𝙲𝙳𝙴𝙵𝙶𝙷𝙸𝙹𝙺𝙻𝙼𝙽𝙾𝙿𝚀𝚁𝚂𝚃𝚄𝚅𝚆𝚇𝚈𝚉",
        "𝚊𝚋𝚌𝚍𝚎𝚏𝚐𝚑𝚒𝚓𝚔𝚕𝚖𝚗𝚘𝚙𝚚𝚛𝚜𝚝𝚞𝚟𝚠𝚡𝚢𝚣",
    ),
    "fraktur": (
        "𝔄𝔅ℭ𝔇𝔈𝔉𝔊ℌℑ𝔍𝔎𝔏𝔐𝔑𝔒𝔓𝔔ℜ𝔖𝔗𝔘𝔙𝔚𝔛𝔜ℨ",
        "𝔞𝔟𝔠𝔡𝔢𝔣𝔤𝔥𝔦𝔧𝔨𝔩𝔪𝔫𝔬𝔭𝔮𝔯𝔰𝔱𝔲𝔳𝔴𝔵𝔶𝔷",
    ),
}

_SMALLCAPS_PAIRS = {
    "a":"ᴀ","b":"ʙ","c":"ᴄ","d":"ᴅ","e":"ᴇ","f":"ꜰ","g":"ɢ","h":"ʜ","i":"ɪ","j":"ᴊ",
    "k":"ᴋ","l":"ʟ","m":"ᴍ","n":"ɴ","o":"ᴏ","p":"ᴘ","q":"ǫ","r":"ʀ","s":"s","t":"ᴛ",
    "u":"ᴜ","v":"ᴠ","w":"ᴡ","x":"x","y":"ʏ","z":"ᴢ"
}
_SMALLCAPS_PAIRS.update({k.upper(): v for k, v in list(_SMALLCAPS_PAIRS.items())})
_SMALLCAPS_MAP = str.maketrans(_SMALLCAPS_PAIRS)
_SMALLCAPS_REVERSE = str.maketrans({v: k.lower() for k, v in _SMALLCAPS_PAIRS.items() if k.islower()})

_STYLE_DIGITS = {
    "bold": "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗",
    "double": "𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡",
    "sans_bold": "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵",
    "monospace": "𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿",
    "fraktur": "0123456789",
    "smallcaps": "0123456789",
}

def _style_math(text, style):
    upper, lower = _STYLE_ALPHABETS[style]
    return text.translate(str.maketrans(_ASCII, upper + lower))

def _style_digits(text, style):
    digits = _STYLE_DIGITS.get(style, "0123456789")
    return text.translate(str.maketrans("0123456789", digits))

def style_text(text, style_id):
    text = text or ""
    style_key = STYLE_DEFINITIONS[int(style_id) - 1][1]
    if style_key == "smallcaps":
        return _style_digits(text.translate(_SMALLCAPS_MAP), style_key)
    return _style_digits(_style_math(text, style_key), style_key)

def _reverse_math(text, style):
    upper, lower = _STYLE_ALPHABETS[style]
    reverse = {dst: src for src, dst in zip(_ASCII, upper + lower)}
    return text.translate(str.maketrans(reverse))

def unstyle_text(text):
    text = text or ""
    # Сначала возвращаем математические Unicode-алфавиты обратно в ASCII.
    for style_key in ("bold", "bold_italic", "double", "sans_bold", "monospace", "fraktur"):
        text = _reverse_math(text, style_key)
    text = text.translate(_SMALLCAPS_REVERSE)
    # Удаляем только Unicode-стилизованные цифры, сохраняя обычные цифры.
    reverse_digits = {}
    for styled in _STYLE_DIGITS.values():
        for plain, fancy in zip("0123456789", styled):
            reverse_digits[fancy] = plain
    return text.translate(str.maketrans(reverse_digits)).replace("\u200b", "")

def style_display_name(name, style_id):
    return style_text(name or "User", style_id)

def get_current_styled_profile_preview(base_first, base_last, offset, style_id, include_nick=True, include_time=True):
    style_key = STYLE_DEFINITIONS[int(style_id) - 1][1]
    first = style_text(base_first or "User", style_id) if include_nick else (base_first or "User")
    last = style_text(base_last or "", style_id) if include_nick else (base_last or "")
    time_marker = ""
    if include_time:
        tz_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=offset)
        time_text = _style_digits(tz_now.strftime("%H:%M"), style_key)
        time_marker = f"[{time_text}]"
    if time_marker:
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
        # Базовое имя записываем ОДИН раз. Стили никогда не применяются к нему повторно.
        if not cfg.get("profile_base_first_name"):
            clean_first = re.sub(r"\s*\[[^\]]+\]", "", me.first_name or "User").strip()
            cfg["profile_base_first_name"] = unstyle_text(clean_first) or "User"
        if not cfg.get("profile_base_last_name"):
            clean_last = re.sub(r"\s*\[[^\]]+\]", "", me.last_name or "").strip()
            cfg["profile_base_last_name"] = unstyle_text(clean_last)
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
            "style_preview_id": None,
            "style_preview_nick_enabled": True,
            "style_preview_time_enabled": True
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
    """Корректно останавливает Pyrogram независимо от того, start() или connect() использовался."""
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
            if u_state["state"] == "START":
                uid_str = str(user_id)
                cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
                if cfg and cfg.get("logged_in", False):
                    u_state["state"] = "MENU"
        return await handler(event, data)

async def delete_user_message_later(message: types.Message, delay=USER_MESSAGE_DELETE_DELAY):
    """Удаляет любое входящее сообщение пользователя ровно по истечении 3 секунд."""
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

# === АВТООТВЕТЧИК ===
async def autoresponder_func(client, message):
    """
    Отвечает только тогда, когда входящее сообщение является первым
    видимым сообщением в текущей истории ЛС.

    После очистки/удаления истории Telegram снова возвращает только новое
    сообщение, поэтому автоответчик сможет сработать повторно.
    """
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

        # Защита от повторной обработки одного и того же Telegram-события.
        # В отличие от replied_users, это НЕ блокирует новый автоответ после
        # полной очистки чата: новый входящий получит новый message.id.
        last_replied = user_cfg.get("autoresponder_last_replied") or {}
        if str(last_replied.get(chat_key)) == str(message.id):
            return

        history_messages = []
        async for msg in client.get_chat_history(message.chat.id, limit=10):
            if msg.id != message.id:
                history_messages.append(msg)

        if history_messages:
            # В текущей истории уже есть другие сообщения, значит собеседник
            # не пишет первым. После очистки истории этот список станет пустым.
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
        # Храним только последние 200 ЛС, чтобы config не разрастался бесконечно.
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
    """Определяет, активна ли недавно хотя бы одна другая Telegram-сессия."""
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
    """Поддерживает онлайн без суточного лимита."""
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

        base_first = unstyle_text(user_cfg.get("profile_base_first_name") or me.first_name or "User").strip() or "User"
        base_last = unstyle_text(user_cfg.get("profile_base_last_name") or me.last_name or "").strip()
        style_id = int(user_cfg.get("time_nick_style", 1) or 1)
        style_nick_enabled = bool(user_cfg.get("style_nick_enabled", True))
        style_time_enabled = bool(user_cfg.get("style_time_enabled", True))

        styled_first = style_text(base_first, style_id) if style_nick_enabled else base_first
        styled_last = style_text(base_last, style_id) if (base_last and style_nick_enabled) else base_last

        new_first = styled_first
        new_last = styled_last

        if user_cfg.get("time_nick_active", False):
            offset = user_cfg.get("timezone_offset", 5)
            tz_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=offset)
            time_value = tz_now.strftime('%H:%M')
            styled_time = _style_digits(time_value, STYLE_DEFINITIONS[style_id - 1][1]) if style_time_enabled else time_value
            time_marker = f"[{styled_time}]"

            if styled_last:
                new_last = f"{styled_last} {time_marker}"
            else:
                new_first = f"{styled_first} {time_marker}"

        if new_first != (me.first_name or "") or new_last != (me.last_name or ""):
            await data["client"].update_profile(first_name=new_first, last_name=new_last)

        user_cfg["profile_base_first_name"] = base_first
        user_cfg["profile_base_last_name"] = base_last
        user_cfg["time_nick_style"] = style_id
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
    """Создаёт и запускает in-memory Pyrogram client из session string."""
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
    """Экспортирует авторизованную сессию и сохраняет её в Supabase config."""
    uid_str = str(user_id)
    session_string = await client.export_session_string()
    user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}
    user_cfg["session_string"] = session_string
    user_cfg["logged_in"] = True
    MEMORY_DB["config"][uid_str] = user_cfg
    await async_db_save("config", uid_str, user_cfg)
    return session_string

async def ensure_client_connected(user_id):
    """Проверяет/восстанавливает клиент."""
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
    """После рестарта восстанавливает аккаунты с активными функциями."""
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
            "replied_users": [], "autoresponder_last_replied": {}, "time_nick_style": 1,
            "style_nick_enabled": True, "style_time_enabled": True,
            "profile_base_first_name": message.from_user.first_name or "User",
            "profile_base_last_name": "",
            "username": message.from_user.username or "N/A",
            "first_name": message.from_user.first_name or "User", "logged_in": False,
            "msg_id": None, "session_string": None
        }
        asyncio.create_task(async_db_save("config", uid_str, MEMORY_DB["config"][uid_str]))

    is_valid = await ensure_client_connected(user_id)
    if is_valid:
        data["state"] = "MENU"
        log_action(user_id, "Ввёл команду /start")
        await edit_or_send(user_id, get_text(user_id, "msg_menu"),
                           reply_markup=show_main_menu_builder(user_id).as_markup())
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

        # Сохраняем момент разблокировки в Supabase, поэтому после рестарта
        # регистрация всё равно останется замороженной до истечения полного срока.
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
        "time_nick_style": old_cfg.get("time_nick_style", 1),
        "style_nick_enabled": old_cfg.get("style_nick_enabled", True),
        "style_time_enabled": old_cfg.get("style_time_enabled", True),
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
    builder.adjust(2, 2, 1)
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
    await edit_or_send(user_id, get_text(user_id, "msg_menu"), reply_markup=show_main_menu_builder(user_id).as_markup())
    try: await callback.answer()
    except Exception: pass

# === МЕНЮ АВТООТВЕТЧИКА ===
@dp.callback_query(F.data == "menu_autoresponder")
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

@dp.callback_query(F.data.startswith("toggle_autoresponder_"))
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

@dp.callback_query(F.data == "setup_autoresponder_greeting")
async def setup_autoresponder_greeting(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    data["state"] = "WAITING_AUTORESP_GREETING"

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_autoresponder")
    await edit_or_send(user_id, get_text(user_id, "msg_autoresp_req"), reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_AUTORESP_GREETING")
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
@dp.callback_query(F.data == "menu_activity")
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

@dp.callback_query(F.data == "menu_247")
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

@dp.callback_query(F.data.startswith("toggle_247_"))
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

@dp.callback_query(F.data == "menu_timenick")
async def menu_timenick(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    is_active = user_cfg.get("time_nick_active", False)
    offset = user_cfg.get("timezone_offset", 5)

    status = get_text(user_id, "status_on") if is_active else get_text(user_id, "status_off")
    style_id = int(user_cfg.get("time_nick_style", 1) or 1)
    base_first = user_cfg.get("profile_base_first_name") or user_cfg.get("first_name") or "User"
    base_last = user_cfg.get("profile_base_last_name") or ""
    style_preview = get_current_styled_profile_preview(
        base_first,
        base_last,
        offset,
        style_id,
        include_nick=bool(user_cfg.get("style_nick_enabled", True)),
        include_time=bool(user_cfg.get("style_time_enabled", True)),
    )
    text = get_text(user_id, "msg_timenick_text", status, style_preview, offset)

    builder = InlineKeyboardBuilder()
    if is_active:
        builder.button(text=get_text(user_id, "btn_turn_off"), callback_data="toggle_timenick_off")
    else:
        builder.button(text=get_text(user_id, "btn_turn_on"), callback_data="toggle_timenick_on")
    builder.button(text=get_text(user_id, "btn_tz_select"), callback_data="select_tz_menu")
    builder.button(text=get_text(user_id, "btn_style_select"), callback_data="select_style_menu")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(1)

    await edit_or_send(user_id, text, reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("toggle_timenick_"))
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

def build_style_selection_markup(user_id):
    builder = InlineKeyboardBuilder()
    uid_str = str(user_id)
    cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str) or {}
    base_name = unstyle_text(cfg.get("profile_base_first_name") or cfg.get("first_name") or "Silence").strip() or "Silence"
    for idx, (_, _) in enumerate(STYLE_DEFINITIONS, start=1):
        builder.button(
            text=style_display_name(base_name, idx),
            callback_data=f"style_select_{idx}"
        )
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_timenick")
    builder.adjust(1)
    return builder.as_markup()

def get_style_preview_markup(user_id, data):
    nick_on = bool(data.get("style_preview_nick_enabled", True))
    time_on = bool(data.get("style_preview_time_enabled", True))

    builder = InlineKeyboardBuilder()
    builder.button(
        text=get_text(user_id, "btn_style_nickname_revert") if nick_on
        else get_text(user_id, "btn_style_nickname_apply"),
        callback_data="style_toggle_nickname"
    )
    builder.button(
        text=get_text(user_id, "btn_style_time_revert") if time_on
        else get_text(user_id, "btn_style_time_apply"),
        callback_data="style_toggle_time"
    )
    builder.button(text=get_text(user_id, "btn_confirm"), callback_data="style_confirm")
    builder.button(text=get_text(user_id, "btn_back"), callback_data="style_back")
    builder.adjust(1)
    return builder.as_markup()

async def render_style_preview(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}

    style_id = int(data.get("style_preview_id") or user_cfg.get("time_nick_style", 1) or 1)
    data["style_preview_id"] = style_id

    client = data.get("client")
    if client and client.is_connected:
        user_cfg = await ensure_profile_base(user_id)

    base_first = user_cfg.get("profile_base_first_name") or user_cfg.get("first_name") or "User"
    base_last = user_cfg.get("profile_base_last_name") or ""
    offset = int(user_cfg.get("timezone_offset", 5) or 5)

    preview = get_current_styled_profile_preview(
        base_first,
        base_last,
        offset,
        style_id,
        include_nick=bool(data.get("style_preview_nick_enabled", True)),
        include_time=bool(data.get("style_preview_time_enabled", True)),
    )
    await edit_or_send(
        user_id,
        get_text(user_id, "msg_style_preview", preview),
        reply_markup=get_style_preview_markup(user_id, data)
    )
    try:
        await callback.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "select_style_menu")
async def select_style_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await edit_or_send(
        user_id,
        get_text(user_id, "msg_style_select"),
        reply_markup=build_style_selection_markup(user_id)
    )
    try:
        await callback.answer()
    except Exception:
        pass

@dp.callback_query(F.data.startswith("style_select_"))
async def style_select(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    style_id = int(callback.data.split("_")[-1])
    if not 1 <= style_id <= len(STYLE_DEFINITIONS):
        await callback.answer("Неизвестный стиль", show_alert=True)
        return

    data["style_preview_id"] = style_id
    data["style_preview_nick_enabled"] = True
    data["style_preview_time_enabled"] = True
    await render_style_preview(callback)

@dp.callback_query(F.data == "style_toggle_nickname")
async def style_toggle_nickname(callback: types.CallbackQuery):
    data = get_user_state(callback.from_user.id)
    data["style_preview_nick_enabled"] = not bool(data.get("style_preview_nick_enabled", True))
    await render_style_preview(callback)

@dp.callback_query(F.data == "style_toggle_time")
async def style_toggle_time(callback: types.CallbackQuery):
    data = get_user_state(callback.from_user.id)
    data["style_preview_time_enabled"] = not bool(data.get("style_preview_time_enabled", True))
    await render_style_preview(callback)

@dp.callback_query(F.data == "style_confirm")
async def style_confirm(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str) or {}

    style_id = int(data.get("style_preview_id") or user_cfg.get("time_nick_style", 1) or 1)
    nick_on = bool(data.get("style_preview_nick_enabled", True))
    time_on = bool(data.get("style_preview_time_enabled", True))

    # "Подтвердить" сохраняет именно текущее состояние предпросмотра.
    user_cfg["time_nick_style"] = style_id
    user_cfg["style_nick_enabled"] = nick_on
    user_cfg["style_time_enabled"] = time_on
    MEMORY_DB["config"][uid_str] = user_cfg
    asyncio.create_task(async_db_save("config", uid_str, user_cfg))

    await update_profile_branding(user_id)
    data["style_preview_id"] = None
    await menu_timenick(callback)

@dp.callback_query(F.data == "style_back")
async def style_back(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    data["style_preview_id"] = None
    data["style_preview_nick_enabled"] = True
    data["style_preview_time_enabled"] = True
    # Возвращаемся именно к выбору стилей, без предпросмотра.
    await edit_or_send(
        user_id,
        get_text(user_id, "msg_style_select"),
        reply_markup=build_style_selection_markup(user_id)
    )
    try:
        await callback.answer()
    except Exception:
        pass

@dp.callback_query(F.data == "select_tz_menu")
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

@dp.callback_query(F.data.startswith("set_tz_"))
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

# === RENDER WEB SERVICE ENDPOINT ===
async def handle_ping(request):
    return web.Response(text="OK")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 HTTP WebServer запущен на порту {port}")

# === ОСНОВНОЙ ЗАПУСК ===
async def main():
    await start_web_server()
    await restore_saved_sessions()
    await dp.start_polling(bot)

if __name__ == "__main__":
    loop.run_until_complete(main())
