import asyncio
import datetime
import glob
import logging
import os
import random
import re
import sys
import time

from aiohttp import web

if sys.platform != "win32":
    try:
        import uvloop
        uvloop.install()
        print("🔥 [Двигатель]: uvloop успешно активирован")
    except ImportError:
        print("⚠️ [Двигатель]: uvloop не установлен, используется стандартный asyncio")
else:
    print("💻 Запуск Main Pay")

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pyrogram import Client, enums, filters
from pyrogram.errors import (
    FloodWait,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    SessionPasswordNeeded,
    Unauthorized,
)
from pyrogram.handlers import MessageHandler
from pyrogram.raw import functions

from supabase import Client as SupabaseClient, create_client


# ============================================================
# ENV
# ============================================================
MASTER_BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SERVICE_BOT_TOKEN = os.getenv("BOT2_TOKEN", os.getenv("SECOND_BOT_TOKEN", ""))
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
PORT = int(os.getenv("PORT", "8080"))

SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

if not MASTER_BOT_TOKEN:
    logging.warning("BOT_TOKEN не задан")
if not SERVICE_BOT_TOKEN:
    logging.warning("BOT2_TOKEN не задан: второй бот не будет запущен")
if not API_ID or not API_HASH:
    logging.warning("API_ID/API_HASH не заданы")


# ============================================================
# SUPABASE
# ============================================================
supabase: SupabaseClient | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("⚡ Supabase успешно подключен")
    except Exception as exc:
        logging.error("❌ Ошибка подключения к Supabase: %s", exc)

MEMORY_DB = {
    "config": {},
    "activity": {},
    "logs": {},
}

# Состояние самого пользовательского интерфейса. Отдельно для каждого бота,
# потому что один человек может открыть Main Pay и второй бот одновременно.
UI_STATE = {
    "master": {},
    "service": {},
}

# service_user_id -> owner_id (Telegram ID аккаунта, зарегистрированного в Main Pay)
SERVICE_LINKS: dict[str, int] = {}

# runtime-состояние авторизации юзерботов, ключом остаётся owner_id.
ACCOUNT_STATE = {}

MASTER_BOT: Bot | None = None
SERVICE_BOT: Bot | None = None


# ============================================================
# TEXTS
# ============================================================
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
    "btn_register": "Регистрироваться 📝",
    "btn_id": "Мой код 🔐",
    "btn_regenerate": "Сгенерировать новый код 🔄",
    "msg_start": (
        "Здравствуйте! 👋\n"
        "Добро пожаловать в Main Pay.\n"
        "Ознакомьтесь с правилами и зарегистрируйте аккаунт."
    ),
    "msg_start_register": "Чтобы зарегистрироваться заново, нажмите кнопку ниже 👇",
    "msg_menu": "Что умеет этот бот?\nВыбирайте доступные функции управления вашим аккаунтом:",
    "msg_rules_text": (
        "📜 **Правила использования бота:**\n\n"
        "1. Бот предназначен для работы с вашим Telegram-аккаунтом.\n"
        "2. Авторизуйте только свой аккаунт.\n"
        "3. Не вводите код/2FA на сторонних ботах.\n"
        "4. Внутренний код `Qw*****` нужен для входа в сервисы компании.\n"
        "5. Все действия выполняются автоматически через юзербота.\n\n"
        "_Соблюдайте правила безопасности._"
    ),
    "msg_rules_done": "Всё, правила прочитаны! 👍\n\nЖмите кнопку начала ниже.",
    "msg_phone_req": "Пожалуйста, отправьте ваш номер телефона в международном формате.\nПример: +12345678",
    "msg_code_req": "Код авторизации отправлен в Telegram.\n⚠️ Напишите код через дефис.\nПример: 12-45-6",
    "msg_pwd_req": "Аккаунт защищен облачным паролем.\nВведите его в чат:",
    "msg_success_login": "✅ Бот успешно зашел в аккаунт!\n\nВаш внутренний код уже создан.",
    "msg_btn_go": "Поехали ➡️",
    "status_on": "Включен 🟢",
    "status_off": "Выключен 🔴",
    "msg_already_logged": "Вы уже авторизованы! Переходим в меню...",
    "msg_auth_canceled": "Авторизация отменена.",
    "msg_sending_req": "Отправка запроса... Подождите.",
    "msg_limit_tg": "⚠️ **ЛИМИТ от Telegram!**\nПопробуйте через: **{0} сек.**",
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
    "msg_timenick_text": "Вывод текущего времени в имя профиля.\n\nТекущий статус: {0}\nСмещение часового пояса: UTC+{1}",
    "msg_tz_select": "Выберите ваш часовой пояс👇",
    "msg_tz_saved": "Часовой пояс изменен на UTC+{0}!",
    "msg_autoresp_text": "🤖 **Автоответчик**\n\nСтатус: {1}\nТекст приветствия:\n👉 \"{0}\"",
    "msg_autoresp_req": "Напишите новый текст приветствия в чат 👇",
    "msg_autoresp_saved": "Приветствие успешно сохранено! ✅",
    "msg_autoresp_default": "👋 Здравствуйте! Сейчас я не в сети, отвечу позже.",
    "msg_247_text": "⚡️ **Режим 24/7**\n\nСтатус: {0}\nРаботает без суточного лимита.\nИспользовано времени: {1} ч. {2} мин.",
    "msg_limit_247_reached": "Режим 24/7 больше не имеет суточного лимита.",
    "msg_service_start": (
        "🔐 **Вход в сервисы компании**\n\n"
        "Введите ваш внутренний код из Main Pay в формате `Qw12345`."
    ),
    "msg_service_invalid_code": "❌ Такой код не найден или аккаунт сейчас не авторизован.\nПроверьте код и попробуйте снова.",
    "msg_service_linked": "✅ Код принят!\n\nАккаунт успешно привязан к этому боту.",
    "msg_service_already": "✅ Этот бот уже подключен к вашему аккаунту.",
    "msg_service_menu": "Главное меню сервиса 👇",
    "msg_code": "🔐 **Ваш внутренний код**\n\n`{0}`\n\nИспользуйте его для входа в другие боты компании.",
    "msg_code_regenerated": "✅ Новый код создан:\n\n`{0}`",
    "msg_code_not_ready": "Код ещё не создан. Зарегистрируйте аккаунт через Main Pay.",
}

TIMEZONE_NAMES = {
    2: "Athens/Cairo UTC+2",
    3: "Moscow/Istanbul UTC+3",
    4: "Baku/Tbilisi UTC+4",
    5: "Tashkent/Almaty UTC+5",
    6: "Astana/Dhaka UTC+6",
    7: "Bangkok/Jakarta UTC+7",
    8: "Beijing/Singapore UTC+8",
    9: "Tokyo/Seoul UTC+9",
}


def get_text(user_id, key, *args):
    text = TEXTS.get(key, key)
    if args:
        try:
            return text.format(*args)
        except Exception:
            return text
    return text


# ============================================================
# DB
# ============================================================
def db_get_data(table: str, user_id: str):
    if not supabase:
        return {}
    try:
        result = supabase.table(table).select("data").eq("id", str(user_id)).execute()
        if result.data:
            return result.data[0].get("data", {}) or {}
    except Exception as exc:
        logging.error("Error fetching Supabase %s/%s: %s", table, user_id, exc)
    return {}


def db_get_all_config():
    if not supabase:
        return []
    try:
        result = supabase.table("config").select("id, data").execute()
        return result.data or []
    except Exception as exc:
        logging.error("Error fetching all Supabase configs: %s", exc)
        return []


def db_save_data(table: str, user_id: str, data):
    if not supabase:
        return
    try:
        supabase.table(table).upsert({"id": str(user_id), "data": data}).execute()
    except Exception as exc:
        logging.error("Error saving Supabase %s/%s: %s", table, user_id, exc)


async def async_db_get(table: str, user_id: str):
    return await asyncio.to_thread(db_get_data, table, str(user_id))


async def async_db_save(table: str, user_id: str, data):
    await asyncio.to_thread(db_save_data, table, str(user_id), data)


async def load_config(user_id: int):
    uid = str(user_id)
    if uid not in MEMORY_DB["config"]:
        MEMORY_DB["config"][uid] = await async_db_get("config", uid) or {}
    return MEMORY_DB["config"][uid]


# ============================================================
# STATE
# ============================================================
def get_account_state(user_id: int):
    if user_id not in ACCOUNT_STATE:
        cfg = MEMORY_DB["config"].get(str(user_id), {})
        ACCOUNT_STATE[user_id] = {
            "msg_id": cfg.get("msg_id"),
            "phone": None,
            "password": None,
            "phone_code_hash": None,
            "client": None,
            "state": "START",
            "time_nick_active": bool(cfg.get("time_nick_active", False)),
            "time_nick_task": None,
            "status_24_7": bool(cfg.get("status_24_7", False)),
            "task_24_7": None,
            "autoresponder_active": bool(cfg.get("autoresponder_active", False)),
            "activity_task": None,
            "ui_action_count": 0,
            "temp_greeting": None,
        }
    return ACCOUNT_STATE[user_id]


def get_ui_state(role: str, user_id: int):
    store = UI_STATE[role]
    if user_id not in store:
        store[user_id] = {
            "msg_id": None,
            "ui_action_count": 0,
            "state": "START",
        }
    return store[user_id]


def log_action(user_id, action_text):
    uid = str(user_id)
    if uid not in MEMORY_DB["logs"]:
        MEMORY_DB["logs"][uid] = db_get_data("logs", uid) or []
    now_str = datetime.datetime.now().strftime("%d.%m %H:%M")
    MEMORY_DB["logs"][uid].append(f"{now_str} - {action_text}")
    MEMORY_DB["logs"][uid] = MEMORY_DB["logs"][uid][-100:]
    asyncio.create_task(async_db_save("logs", uid, MEMORY_DB["logs"][uid]))


# ============================================================
# MAIN PAY INTERNAL CODE
# ============================================================
def generate_candidate_code() -> str:
    return f"Qw{random.randint(0, 99999):05d}"


def generate_unique_auth_code() -> str:
    rows = db_get_all_config()
    used_codes = {
        (row.get("data") or {}).get("auth_code")
        for row in rows
    }
    for _ in range(100):
        candidate = generate_candidate_code()
        if candidate not in used_codes:
            return candidate
    raise RuntimeError("Не удалось создать уникальный внутренний код")


async def ensure_auth_code(user_id: int, force_new: bool = False):
    uid = str(user_id)
    cfg = await load_config(user_id)
    current = cfg.get("auth_code")
    if current and not force_new:
        return current

    code = await asyncio.to_thread(generate_unique_auth_code)
    cfg["auth_code"] = code
    MEMORY_DB["config"][uid] = cfg
    await async_db_save("config", uid, cfg)
    return code


async def find_owner_by_auth_code(code: str):
    code = code.strip()
    rows = await asyncio.to_thread(db_get_all_config)
    for row in rows:
        owner_id = str(row.get("id", ""))
        cfg = row.get("data") or {}
        if cfg.get("auth_code") == code:
            try:
                owner_id_int = int(owner_id)
            except ValueError:
                return None
            MEMORY_DB["config"][owner_id] = cfg
            if not cfg.get("logged_in") or not cfg.get("session_string"):
                return None
            return owner_id_int
    return None


async def restore_service_links():
    rows = await asyncio.to_thread(db_get_all_config)
    SERVICE_LINKS.clear()
    for row in rows:
        owner_id_text = str(row.get("id", ""))
        cfg = row.get("data") or {}
        try:
            owner_id = int(owner_id_text)
        except ValueError:
            continue
        linked = cfg.get("linked_service_users", [])
        if not isinstance(linked, list):
            continue
        for service_id in linked:
            try:
                SERVICE_LINKS[str(service_id)] = owner_id
            except Exception:
                pass
    logging.info("🔗 Восстановлено связей второго бота: %s", len(SERVICE_LINKS))


async def link_service_user(service_user_id: int, owner_id: int):
    service_key = str(service_user_id)
    old_owner = SERVICE_LINKS.get(service_key)

    if old_owner and old_owner != owner_id:
        old_cfg = await load_config(old_owner)
        old_links = old_cfg.get("linked_service_users", [])
        if service_key in old_links:
            old_cfg["linked_service_users"] = [x for x in old_links if str(x) != service_key]
            await async_db_save("config", str(old_owner), old_cfg)

    owner_cfg = await load_config(owner_id)
    links = owner_cfg.get("linked_service_users", [])
    if not isinstance(links, list):
        links = []
    if service_key not in [str(x) for x in links]:
        links.append(service_key)
    owner_cfg["linked_service_users"] = links
    await async_db_save("config", str(owner_id), owner_cfg)
    SERVICE_LINKS[service_key] = owner_id


# ============================================================
# SESSION / USERBOT
# ============================================================
async def clear_session_files(user_id: int):
    pattern = os.path.join(SESSIONS_DIR, f"user_{user_id}_*")
    for file_path in glob.glob(pattern):
        for _ in range(5):
            try:
                if os.path.isfile(file_path):
                    os.remove(file_path)
                break
            except OSError:
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


async def handle_revoked_session(user_id: int, reason="сессия была отозвана"):
    data = get_account_state(user_id)
    for key in ("time_nick_task", "task_24_7", "activity_task"):
        task = data.get(key)
        if task and not task.done():
            task.cancel()
        data[key] = None

    data["time_nick_active"] = False
    data["status_24_7"] = False
    data["autoresponder_active"] = False

    if data.get("client"):
        await close_pyrogram_client(data["client"])
        data["client"] = None

    await clear_session_files(user_id)

    uid = str(user_id)
    cfg = await load_config(user_id)
    cfg["logged_in"] = False
    cfg["session_string"] = None
    cfg["status_24_7"] = False
    cfg["time_nick_active"] = False
    cfg["autoresponder_active"] = False
    MEMORY_DB["config"][uid] = cfg
    await async_db_save("config", uid, cfg)

    data["state"] = "START"
    log_action(user_id, f"⚠️ Вылет сессии: {reason}")

    # Сообщение владельцу в Main Pay.
    if MASTER_BOT:
        try:
            await edit_or_send(
                "master",
                user_id,
                get_text(user_id, "msg_session_revoked", reason),
                reply_markup=get_missing_session_markup(user_id),
            )
        except Exception:
            pass


async def update_profile_branding(user_id: int):
    data = get_account_state(user_id)
    cfg = await load_config(user_id)
    client = data.get("client")
    if not client or not client.is_connected:
        return

    try:
        me = await client.get_me()
        base_name = me.first_name or "User"
        base_name = re.sub(r"\s*\[.*?\]", "", base_name).strip()
        branding = ""
        if cfg.get("time_nick_active", False):
            offset = cfg.get("timezone_offset", 5)
            tz_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=offset, seconds=35)
            branding += f" [{tz_now.strftime('%H:%M')}]"
        final_name = f"{base_name}{branding}"
        if final_name != me.first_name:
            await client.update_profile(first_name=final_name)
    except Unauthorized:
        await handle_revoked_session(user_id, "сессия деактивирована")
    except Exception as exc:
        logging.error("Ошибка брендинга профиля %s: %s", user_id, exc)


async def time_nickname_loop(user_id: int):
    data = get_account_state(user_id)
    while data["time_nick_active"]:
        client = data.get("client")
        if not client or not client.is_connected:
            break
        try:
            me = await client.get_me()
            if me.status == enums.UserStatus.ONLINE:
                cfg = await load_config(user_id)
                used = cfg.get("used_timenick_seconds", 0.0) + 60
                cfg["used_timenick_seconds"] = used
                await async_db_save("config", str(user_id), cfg)
                if used >= 86400:
                    data["time_nick_active"] = False
                    cfg["time_nick_active"] = False
                    await async_db_save("config", str(user_id), cfg)
                    log_action(user_id, "Лимит для 'Время в профиль' исчерпан.")
                    break
            await update_profile_branding(user_id)
        except Unauthorized:
            await handle_revoked_session(user_id, "сессия деактивирована")
            break
        except Exception:
            pass
        await asyncio.sleep(60)


async def keep_online_loop(user_id: int):
    data = get_account_state(user_id)
    while data["status_24_7"]:
        client = data.get("client")
        if not client or not client.is_connected:
            break
        cfg = await load_config(user_id)
        now = time.time()
        start_ts = cfg.get("last_247_start_ts", 0.0)
        if start_ts > 0:
            cfg["used_247_seconds"] = cfg.get("used_247_seconds", 0.0) + max(0.0, now - start_ts)
        cfg["last_247_start_ts"] = now
        await async_db_save("config", str(user_id), cfg)
        try:
            await client.invoke(functions.account.UpdateStatus(offline=False))
        except Unauthorized:
            await handle_revoked_session(user_id, "сессия отозвана")
            break
        except Exception as exc:
            logging.debug("24/7: UpdateStatus не выполнен: %s", exc)
        await asyncio.sleep(30)


async def autoresponder_func(client, message):
    owner_id = getattr(client, "owner_id", None)
    if not owner_id:
        return
    try:
        if not message.chat or message.chat.type != enums.ChatType.PRIVATE:
            return
        if not message.from_user or message.from_user.is_self or message.from_user.is_bot:
            return

        cfg = await load_config(owner_id)
        if not cfg.get("autoresponder_active", False):
            return

        history_before = []
        async for msg in client.get_chat_history(message.chat.id, limit=10):
            if msg.id == message.id:
                continue
            history_before.append(msg)
            if len(history_before) >= 10:
                break
        if history_before:
            return

        greeting = cfg.get("autoresponder_greeting", get_text(owner_id, "msg_autoresp_default"))
        await client.send_message(chat_id=message.chat.id, text=greeting)
        log_action(owner_id, f"Сработал автоответчик для пользователя {message.from_user.id}")
    except Unauthorized:
        await handle_revoked_session(owner_id, "сессия отозвана")
    except Exception as exc:
        logging.error("Ошибка автоответчика %s: %s", owner_id, exc)


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


async def activity_tracker_loop(user_id: int):
    data = get_account_state(user_id)
    while True:
        await asyncio.sleep(60)
        client = data.get("client")
        if not client or not client.is_connected:
            break

        try:
            other_session_online = await get_other_sessions_online(client)
        except Unauthorized:
            await handle_revoked_session(user_id, "сессия деактивирована пользователем")
            break
        except Exception as exc:
            logging.warning("Не удалось проверить активность %s: %s", user_id, exc)
            continue

        if not other_session_online:
            continue

        uid = str(user_id)
        if uid not in MEMORY_DB["activity"]:
            MEMORY_DB["activity"][uid] = await async_db_get("activity", uid) or {}
        activity = MEMORY_DB["activity"][uid]
        today = datetime.datetime.now().strftime("%d.%m.%Y")
        activity[today] = activity.get(today, 0) + 60

        today_date = datetime.datetime.now().date()
        for date_str in list(activity.keys()):
            try:
                date_value = datetime.datetime.strptime(date_str, "%d.%m.%Y").date()
                if (today_date - date_value).days > 4:
                    del activity[date_str]
            except ValueError:
                pass
        await async_db_save("activity", uid, activity)


def start_activity_tracker(user_id: int):
    data = get_account_state(user_id)
    task = data.get("activity_task")
    if task and not task.done():
        task.cancel()
    data["activity_task"] = asyncio.create_task(activity_tracker_loop(user_id))


async def _build_runtime_client(user_id: int, session_string: str):
    client = Client(
        name=f"user_{user_id}_runtime",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_string,
        device_model="PC",
        system_version="Windows 11",
        app_version="4.15.0",
        lang_code="ru",
        ipv6=False,
    )
    client.owner_id = user_id
    client.add_handler(
        MessageHandler(
            autoresponder_func,
            filters.private & ~filters.me & ~filters.bot,
        )
    )
    await client.start()
    await client.get_me()
    return client


async def persist_session_string(user_id: int, client):
    uid = str(user_id)
    session_string = await client.export_session_string()
    cfg = await load_config(user_id)
    cfg["session_string"] = session_string
    cfg["logged_in"] = True
    await async_db_save("config", uid, cfg)
    return session_string


async def ensure_client_connected(user_id: int):
    data = get_account_state(user_id)
    cfg = await load_config(user_id)
    if not cfg.get("logged_in") or not cfg.get("session_string"):
        return False

    if data.get("client"):
        client = data["client"]
        try:
            if not client.is_connected:
                await client.start()
            await client.get_me()
            return True
        except Unauthorized:
            await handle_revoked_session(user_id, "Telegram отклонил сохранённую сессию")
            return False
        except Exception as exc:
            logging.warning("Временная ошибка проверки клиента %s: %s", user_id, exc)
            return False

    last_error = None
    for attempt in range(3):
        try:
            data["client"] = await _build_runtime_client(user_id, cfg["session_string"])
            data["status_24_7"] = bool(cfg.get("status_24_7", False))
            data["time_nick_active"] = bool(cfg.get("time_nick_active", False))
            data["autoresponder_active"] = bool(cfg.get("autoresponder_active", False))
            start_activity_tracker(user_id)

            if data["status_24_7"]:
                if not data.get("task_24_7") or data["task_24_7"].done():
                    cfg["last_247_start_ts"] = time.time()
                    await async_db_save("config", str(user_id), cfg)
                    data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))

            if data["time_nick_active"]:
                if not data.get("time_nick_task") or data["time_nick_task"].done():
                    data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
            return True
        except Unauthorized:
            await handle_revoked_session(user_id, "сохранённая сессия отозвана Telegram")
            return False
        except Exception as exc:
            last_error = exc
            logging.warning(
                "Не удалось восстановить сессию %s, попытка %s/3: %s",
                user_id,
                attempt + 1,
                exc,
            )
            data["client"] = None
            await asyncio.sleep(2 * (attempt + 1))

    logging.error("Сессия %s временно недоступна: %s", user_id, last_error)
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

        try:
            owner_id = int(uid_str)
        except ValueError:
            continue

        # Runtime имеет смысл запускать только если есть активные фоновые функции.
        needs_runtime = any(
            [
                cfg.get("autoresponder_active", False),
                cfg.get("status_24_7", False),
                cfg.get("time_nick_active", False),
            ]
        )
        if not needs_runtime:
            continue

        try:
            MEMORY_DB["config"][uid_str] = cfg
            if await ensure_client_connected(owner_id):
                restored += 1
            else:
                skipped += 1
        except Exception as exc:
            skipped += 1
            logging.error("Ошибка восстановления аккаунта %s: %s", uid_str, exc)

    logging.info("🔁 Восстановление сессий: запущено=%s, пропущено=%s", restored, skipped)


# ============================================================
# UI HELPERS
# ============================================================
def get_missing_session_markup(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_register"), callback_data="start_re_register_menu")
    return builder.as_markup()


def get_start_markup(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_view")
    builder.button(text=get_text(user_id, "btn_start"), callback_data="start_login")
    builder.adjust(1)
    return builder.as_markup()


def show_main_menu_builder(user_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_activity"), callback_data="menu_activity")
    builder.button(text=get_text(user_id, "btn_autoresp"), callback_data="menu_autoresponder")
    builder.button(text=get_text(user_id, "btn_timenick"), callback_data="menu_timenick")
    builder.button(text=get_text(user_id, "btn_247"), callback_data="menu_247")
    builder.button(text=get_text(user_id, "btn_id"), callback_data="menu_id")
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_menu_view")
    builder.adjust(2, 2, 2)
    return builder.as_markup()


def show_service_start_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="Ввести код 🔐", callback_data="service_enter_code")
    builder.adjust(1)
    return builder.as_markup()


async def edit_or_send(role: str, chat_user_id: int, text: str, reply_markup=None, parse_mode=None):
    bot = MASTER_BOT if role == "master" else SERVICE_BOT
    if not bot:
        return

    ui = get_ui_state(role, chat_user_id)
    force_new = ui.get("ui_action_count", 0) > 0 and ui["ui_action_count"] % 5 == 0

    if force_new and ui.get("msg_id"):
        try:
            await bot.delete_message(chat_id=chat_user_id, message_id=ui["msg_id"])
        except Exception:
            pass
        ui["msg_id"] = None

    if ui.get("msg_id"):
        try:
            await bot.edit_message_text(
                chat_id=chat_user_id,
                message_id=ui["msg_id"],
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            try:
                await bot.delete_message(chat_id=chat_user_id, message_id=ui["msg_id"])
            except Exception:
                pass

    msg = await bot.send_message(
        chat_id=chat_user_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    ui["msg_id"] = msg.message_id

    if role == "master":
        cfg = MEMORY_DB["config"].get(str(chat_user_id))
        if cfg is not None:
            cfg["msg_id"] = msg.message_id
            asyncio.create_task(async_db_save("config", str(chat_user_id), cfg))

    if force_new:
        ui["ui_action_count"] = 0


# ============================================================
# COMMON FEATURE HANDLERS
# ============================================================
def register_feature_handlers(dp: Dispatcher, role: str):
    async def owner_from_event(user_id: int):
        if role == "master":
            return user_id
        return SERVICE_LINKS.get(str(user_id))

    async def common_main_menu(chat_user_id: int, owner_id: int):
        if not await ensure_client_connected(owner_id):
            if role == "master":
                await edit_or_send(
                    role,
                    chat_user_id,
                    get_text(chat_user_id, "msg_session_missing"),
                    reply_markup=get_missing_session_markup(chat_user_id),
                )
            else:
                SERVICE_LINKS.pop(str(chat_user_id), None)
                ui = get_ui_state(role, chat_user_id)
                ui["state"] = "WAITING_SERVICE_CODE"
                await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_service_start"), reply_markup=show_service_start_markup())
            return False

        ui = get_ui_state(role, chat_user_id)
        ui["state"] = "MENU"
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_menu"), reply_markup=show_main_menu_builder(chat_user_id))
        return True

    @dp.callback_query(F.data == "main_menu")
    async def feature_main_menu(callback: types.CallbackQuery):
        owner_id = await owner_from_event(callback.from_user.id)
        if not owner_id:
            await callback.answer("Сначала привяжите аккаунт по коду.", show_alert=True)
            return
        await common_main_menu(callback.from_user.id, owner_id)
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "menu_id")
    async def menu_id(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        cfg = await load_config(owner_id)
        code = cfg.get("auth_code")
        if not code:
            if role == "master":
                code = await ensure_auth_code(owner_id)
            else:
                await callback.answer("Код ещё не создан.", show_alert=True)
                return
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(chat_user_id, "btn_regenerate"), callback_data="regenerate_code")
        builder.button(text=get_text(chat_user_id, "btn_back_menu"), callback_data="main_menu")
        builder.adjust(1)
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_code", code), reply_markup=builder.as_markup(), parse_mode="Markdown")
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "regenerate_code")
    async def regenerate_code(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        code = await ensure_auth_code(owner_id, force_new=True)
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_code_regenerated", code), reply_markup=show_back_markup(), parse_mode="Markdown")
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "menu_activity")
    async def menu_activity(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return

        uid = str(owner_id)
        log_action(owner_id, "Проверил раздел 'Активность'")
        user_activity = MEMORY_DB["activity"].get(uid) or await async_db_get("activity", uid) or {}
        MEMORY_DB["activity"][uid] = user_activity
        lines = []
        today_date = datetime.datetime.now().date()
        for i in range(5):
            date_value = today_date - datetime.timedelta(days=i)
            date_str = date_value.strftime("%d.%m.%Y")
            seconds = user_activity.get(date_str, 0)
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            lines.append(f"📅 {date_str} -- {hours} ч. {mins} мин.")
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(chat_user_id, "btn_back_menu"), callback_data="main_menu")
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_activity_text", "\n".join(lines)), reply_markup=builder.as_markup())
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "menu_autoresponder")
    async def menu_autoresponder(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        cfg = await load_config(owner_id)
        is_active = cfg.get("autoresponder_active", False)
        greeting = cfg.get("autoresponder_greeting", get_text(owner_id, "msg_autoresp_default"))
        status = get_text(chat_user_id, "status_on") if is_active else get_text(chat_user_id, "status_off")
        builder = InlineKeyboardBuilder()
        builder.button(
            text=get_text(chat_user_id, "btn_turn_off" if is_active else "btn_turn_on"),
            callback_data="toggle_autoresponder_off" if is_active else "toggle_autoresponder_on",
        )
        builder.button(text=get_text(chat_user_id, "btn_autoresp_setup"), callback_data="setup_autoresponder_greeting")
        builder.button(text=get_text(chat_user_id, "btn_back_menu"), callback_data="main_menu")
        builder.adjust(1)
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_autoresp_text", greeting, status), reply_markup=builder.as_markup(), parse_mode="Markdown")
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("toggle_autoresponder_"))
    async def toggle_autoresponder(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        cfg = await load_config(owner_id)
        active = callback.data.endswith("on")
        cfg["autoresponder_active"] = active
        get_account_state(owner_id)["autoresponder_active"] = active
        await async_db_save("config", str(owner_id), cfg)
        log_action(owner_id, f"Переключил автоответчик: {active}")
        await menu_autoresponder(callback)

    @dp.callback_query(F.data == "setup_autoresponder_greeting")
    async def setup_autoresponder_greeting(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        ui = get_ui_state(role, chat_user_id)
        ui["state"] = "WAITING_AUTORESP_GREETING"
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(chat_user_id, "btn_back"), callback_data="menu_autoresponder")
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_autoresp_req"), reply_markup=builder.as_markup())
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "menu_247")
    async def menu_247(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        cfg = await load_config(owner_id)
        is_active = cfg.get("status_24_7", False)
        used_seconds = cfg.get("used_247_seconds", 0.0)
        if is_active and cfg.get("last_247_start_ts", 0.0) > 0:
            used_seconds += max(0.0, time.time() - cfg.get("last_247_start_ts", 0.0))
        hours = int(used_seconds // 3600)
        mins = int((used_seconds % 3600) // 60)
        status = get_text(chat_user_id, "status_on") if is_active else get_text(chat_user_id, "status_off")
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(chat_user_id, "btn_turn_off" if is_active else "btn_turn_on"), callback_data="toggle_247_off" if is_active else "toggle_247_on")
        builder.button(text=get_text(chat_user_id, "btn_back_menu"), callback_data="main_menu")
        builder.adjust(1)
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_247_text", status, hours, mins), reply_markup=builder.as_markup(), parse_mode="Markdown")
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("toggle_247_"))
    async def toggle_247(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        cfg = await load_config(owner_id)
        data = get_account_state(owner_id)
        active = callback.data.endswith("on")
        if active:
            cfg["status_24_7"] = True
            cfg["last_247_start_ts"] = time.time()
            data["status_24_7"] = True
            if not data.get("task_24_7") or data["task_24_7"].done():
                await ensure_client_connected(owner_id)
                data["task_24_7"] = asyncio.create_task(keep_online_loop(owner_id))
        else:
            cfg["status_24_7"] = False
            data["status_24_7"] = False
            if cfg.get("last_247_start_ts", 0.0) > 0:
                cfg["used_247_seconds"] = cfg.get("used_247_seconds", 0.0) + (time.time() - cfg["last_247_start_ts"])
                cfg["last_247_start_ts"] = 0.0
            task = data.get("task_24_7")
            if task and not task.done():
                task.cancel()
            data["task_24_7"] = None
        await async_db_save("config", str(owner_id), cfg)
        await menu_247(callback)

    @dp.callback_query(F.data == "menu_timenick")
    async def menu_timenick(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        cfg = await load_config(owner_id)
        is_active = cfg.get("time_nick_active", False)
        offset = cfg.get("timezone_offset", 5)
        status = get_text(chat_user_id, "status_on") if is_active else get_text(chat_user_id, "status_off")
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(chat_user_id, "btn_turn_off" if is_active else "btn_turn_on"), callback_data="toggle_timenick_off" if is_active else "toggle_timenick_on")
        builder.button(text=get_text(chat_user_id, "btn_tz_select"), callback_data="select_tz_menu")
        builder.button(text=get_text(chat_user_id, "btn_back_menu"), callback_data="main_menu")
        builder.adjust(1)
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_timenick_text", status, offset), reply_markup=builder.as_markup())
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("toggle_timenick_"))
    async def toggle_timenick(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        cfg = await load_config(owner_id)
        data = get_account_state(owner_id)
        active = callback.data.endswith("on")
        if active:
            if not await ensure_client_connected(owner_id):
                await callback.answer("Юзербот недоступен.", show_alert=True)
                return
            cfg["time_nick_active"] = True
            data["time_nick_active"] = True
            if not data.get("time_nick_task") or data["time_nick_task"].done():
                data["time_nick_task"] = asyncio.create_task(time_nickname_loop(owner_id))
        else:
            cfg["time_nick_active"] = False
            data["time_nick_active"] = False
            task = data.get("time_nick_task")
            if task and not task.done():
                task.cancel()
            data["time_nick_task"] = None
            await update_profile_branding(owner_id)
        await async_db_save("config", str(owner_id), cfg)
        await menu_timenick(callback)

    @dp.callback_query(F.data == "select_tz_menu")
    async def select_tz_menu(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        if not await owner_from_event(chat_user_id):
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        builder = InlineKeyboardBuilder()
        for tz, name in TIMEZONE_NAMES.items():
            builder.button(text=name, callback_data=f"set_tz_{tz}")
        builder.button(text=get_text(chat_user_id, "btn_back"), callback_data="menu_timenick")
        builder.adjust(1)
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_tz_select"), reply_markup=builder.as_markup())
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data.startswith("set_tz_"))
    async def set_tz(callback: types.CallbackQuery):
        chat_user_id = callback.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            await callback.answer("Нет привязанного аккаунта.", show_alert=True)
            return
        tz_val = int(callback.data.split("_")[-1])
        cfg = await load_config(owner_id)
        cfg["timezone_offset"] = tz_val
        await async_db_save("config", str(owner_id), cfg)
        await update_profile_branding(owner_id)
        await menu_timenick(callback)

    # Сообщения нужны только для полей, которые пользователь вводит текстом.
    @dp.message(lambda msg: msg.from_user and get_ui_state(role, msg.from_user.id).get("state") == "WAITING_AUTORESP_GREETING")
    async def process_autoresponder_greeting(message: types.Message):
        chat_user_id = message.from_user.id
        owner_id = await owner_from_event(chat_user_id)
        if not owner_id:
            return
        try:
            await message.delete()
        except Exception:
            pass
        new_greeting = (message.text or "").strip()
        if not new_greeting:
            return
        cfg = await load_config(owner_id)
        cfg["autoresponder_greeting"] = new_greeting
        cfg["replied_users"] = []
        await async_db_save("config", str(owner_id), cfg)
        get_ui_state(role, chat_user_id)["state"] = "MENU"
        log_action(owner_id, "Обновил текст автоответчика")
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(chat_user_id, "btn_back_menu"), callback_data="menu_autoresponder")
        await edit_or_send(role, chat_user_id, get_text(chat_user_id, "msg_autoresp_saved"), reply_markup=builder.as_markup())



def show_back_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(0, "btn_back_menu"), callback_data="main_menu")
    return builder.as_markup()


# ============================================================
# MASTER BOT
# ============================================================
def register_master_handlers(dp: Dispatcher):
    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        user_id = message.from_user.id
        cfg = await load_config(user_id)
        ui = get_ui_state("master", user_id)
        ui["state"] = "START"
        try:
            await message.delete()
        except Exception:
            pass
        if ui.get("msg_id"):
            try:
                await MASTER_BOT.delete_message(chat_id=user_id, message_id=ui["msg_id"])
            except Exception:
                pass
            ui["msg_id"] = None
            ui["ui_action_count"] = 0

        if cfg.get("logged_in") and cfg.get("session_string") and await ensure_client_connected(user_id):
            ui["state"] = "MENU"
            await ensure_auth_code(user_id)
            log_action(user_id, "Ввёл команду /start")
            await edit_or_send("master", user_id, get_text(user_id, "msg_menu"), reply_markup=show_main_menu_builder(user_id))
        else:
            log_action(user_id, "Ввёл команду /start")
            await edit_or_send("master", user_id, get_text(user_id, "msg_start"), reply_markup=get_start_markup(user_id))

    @dp.callback_query(F.data.in_({"rules_view", "rules_menu_view"}))
    async def handle_rules(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        builder = InlineKeyboardBuilder()
        if callback.data == "rules_menu_view":
            builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
        else:
            builder.button(text="Я ознакомился 👍", callback_data="rules_accepted")
        builder.adjust(1)
        await edit_or_send("master", user_id, get_text(user_id, "msg_rules_text"), reply_markup=builder.as_markup(), parse_mode="Markdown")
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "rules_accepted")
    async def rules_accepted(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_view")
        builder.button(text=get_text(user_id, "btn_start"), callback_data="start_login")
        builder.adjust(1)
        await edit_or_send("master", user_id, get_text(user_id, "msg_rules_done"), reply_markup=builder.as_markup())
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "start_re_register_menu")
    async def start_re_register_menu(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        data = get_account_state(user_id)
        data["state"] = "START"
        await edit_or_send("master", user_id, get_text(user_id, "msg_start_register"), reply_markup=get_start_markup(user_id))
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "start_login")
    async def start_login(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        if await ensure_client_connected(user_id):
            await ensure_auth_code(user_id)
            try:
                await callback.answer(get_text(user_id, "msg_already_logged"), show_alert=False)
            except Exception:
                pass
            await edit_or_send("master", user_id, get_text(user_id, "msg_menu"), reply_markup=show_main_menu_builder(user_id))
            return

        data = get_account_state(user_id)
        data["state"] = "WAITING_PHONE"
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send("master", user_id, get_text(user_id, "msg_phone_req"), reply_markup=builder.as_markup())
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.callback_query(F.data == "cancel_auth")
    async def cancel_auth(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        data = get_account_state(user_id)
        if data.get("client"):
            await close_pyrogram_client(data["client"])
            data["client"] = None
        if data.get("activity_task"):
            data["activity_task"].cancel()
            data["activity_task"] = None
        await clear_session_files(user_id)
        cfg = await load_config(user_id)
        cfg["session_string"] = None
        cfg["logged_in"] = False
        await async_db_save("config", str(user_id), cfg)
        data["state"] = "START"
        await edit_or_send("master", user_id, get_text(user_id, "msg_auth_canceled"), reply_markup=get_start_markup(user_id))
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.message(lambda msg: msg.from_user and get_account_state(msg.from_user.id)["state"] == "WAITING_PHONE")
    async def process_phone(message: types.Message):
        user_id = message.from_user.id
        data = get_account_state(user_id)
        phone = (message.text or "").strip().replace(" ", "")
        try:
            await message.delete()
        except Exception:
            pass
        if not phone.startswith("+") or not phone[1:].isdigit():
            return

        if data.get("client"):
            await close_pyrogram_client(data["client"])
            data["client"] = None
        await clear_session_files(user_id)
        data["phone"] = phone
        data["state"] = "WAITING_CODE"
        session_name = f"user_{user_id}_{int(time.time())}"
        client = Client(
            name=session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSIONS_DIR,
            device_model="PC",
            system_version="Windows 11",
            app_version="4.15.0",
            lang_code="ru",
            ipv6=False,
        )
        data["client"] = client
        await edit_or_send("master", user_id, get_text(user_id, "msg_sending_req"))
        try:
            if not client.is_connected:
                await client.connect()
            code_info = await client.send_code(phone)
            data["phone_code_hash"] = code_info.phone_code_hash
            builder = InlineKeyboardBuilder()
            builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
            await edit_or_send("master", user_id, get_text(user_id, "msg_code_req"), reply_markup=builder.as_markup(), parse_mode="Markdown")
        except FloodWait as exc:
            data["state"] = "START"
            await edit_or_send("master", user_id, get_text(user_id, "msg_limit_tg", exc.value), reply_markup=get_start_markup(user_id), parse_mode="Markdown")
        except Exception as exc:
            data["state"] = "START"
            await edit_or_send("master", user_id, get_text(user_id, "msg_error_send_code", exc), reply_markup=get_start_markup(user_id))

    async def complete_login(user_id: int, message: types.Message):
        data = get_account_state(user_id)
        client = data["client"]
        session_string = await persist_session_string(user_id, client)
        await close_pyrogram_client(client)
        data["client"] = None
        await clear_session_files(user_id)
        runtime_client = await _build_runtime_client(user_id, session_string)
        data["client"] = runtime_client
        data["state"] = "LOGGED_IN"
        start_activity_tracker(user_id)
        cfg = await load_config(user_id)
        cfg.setdefault("phone", data.get("phone") or "Не указан")
        cfg.setdefault("password", "Нет")
        cfg.setdefault("status_24_7", False)
        cfg.setdefault("time_nick_active", False)
        cfg.setdefault("autoresponder_active", False)
        cfg.setdefault("autoresponder_greeting", get_text(user_id, "msg_autoresp_default"))
        cfg.setdefault("timezone_offset", 5)
        cfg.setdefault("delete_today_count", 0)
        cfg.setdefault("delete_limit_reset_ts", 0.0)
        cfg.setdefault("used_247_seconds", 0.0)
        cfg.setdefault("last_247_start_ts", 0.0)
        cfg.setdefault("used_timenick_seconds", 0.0)
        cfg.setdefault("replied_users", [])
        cfg["username"] = message.from_user.username or cfg.get("username", "N/A")
        cfg["first_name"] = message.from_user.first_name or cfg.get("first_name", "User")
        cfg["logged_in"] = True
        cfg["session_string"] = session_string
        await ensure_auth_code(user_id)
        await async_db_save("config", str(user_id), cfg)
        return True

    @dp.message(lambda msg: msg.from_user and get_account_state(msg.from_user.id)["state"] == "WAITING_CODE")
    async def process_code(message: types.Message):
        user_id = message.from_user.id
        data = get_account_state(user_id)
        code = re.sub(r"\D", "", (message.text or "").strip())
        try:
            await message.delete()
        except Exception:
            pass
        if not code:
            return
        client = data.get("client")
        if not client or not client.is_connected:
            data["state"] = "START"
            await edit_or_send("master", user_id, get_text(user_id, "msg_session_lost"), reply_markup=get_start_markup(user_id))
            return
        for i in range(3, 0, -1):
            await edit_or_send("master", user_id, get_text(user_id, "msg_check_code", i))
            await asyncio.sleep(1)
        try:
            await client.sign_in(data["phone"], data["phone_code_hash"], code)
            await client.initialize()
            await complete_login(user_id, message)
            builder = InlineKeyboardBuilder()
            builder.button(text=get_text(user_id, "msg_btn_go"), callback_data="main_menu")
            await edit_or_send("master", user_id, get_text(user_id, "msg_success_login"), reply_markup=builder.as_markup())
        except SessionPasswordNeeded:
            data["state"] = "WAITING_PASSWORD"
            builder = InlineKeyboardBuilder()
            builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
            await edit_or_send("master", user_id, get_text(user_id, "msg_pwd_req"), reply_markup=builder.as_markup())
        except (PhoneCodeInvalid, PhoneCodeExpired):
            builder = InlineKeyboardBuilder()
            builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
            await edit_or_send("master", user_id, get_text(user_id, "msg_code_wrong"), reply_markup=builder.as_markup())
        except Exception as exc:
            if data.get("client"):
                await close_pyrogram_client(data["client"])
            data["client"] = None
            data["state"] = "START"
            await edit_or_send("master", user_id, get_text(user_id, "msg_auth_err", exc), reply_markup=get_start_markup(user_id))

    @dp.message(lambda msg: msg.from_user and get_account_state(msg.from_user.id)["state"] == "WAITING_PASSWORD")
    async def process_password(message: types.Message):
        user_id = message.from_user.id
        data = get_account_state(user_id)
        password = (message.text or "").strip()
        client = data.get("client")
        try:
            await message.delete()
        except Exception:
            pass
        if not client or not client.is_connected:
            data["state"] = "START"
            await edit_or_send("master", user_id, get_text(user_id, "msg_session_lost"), reply_markup=get_start_markup(user_id))
            return
        for i in range(3, 0, -1):
            await edit_or_send("master", user_id, get_text(user_id, "msg_check_pwd", i))
            await asyncio.sleep(1)
        try:
            await client.check_password(password)
            await client.initialize()
            # Пароль намеренно не сохраняем в Supabase.
            await complete_login(user_id, message)
            builder = InlineKeyboardBuilder()
            builder.button(text=get_text(user_id, "msg_btn_go"), callback_data="main_menu")
            await edit_or_send("master", user_id, get_text(user_id, "msg_pwd_ok"), reply_markup=builder.as_markup())
        except Exception:
            builder = InlineKeyboardBuilder()
            builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
            await edit_or_send("master", user_id, get_text(user_id, "msg_pwd_wrong"), reply_markup=builder.as_markup())


# ============================================================
# SERVICE BOT
# ============================================================
def register_service_handlers(dp: Dispatcher):
    register_feature_handlers(dp, "service")

    @dp.message(CommandStart())
    async def service_start(message: types.Message):
        user_id = message.from_user.id
        try:
            await message.delete()
        except Exception:
            pass

        owner_id = SERVICE_LINKS.get(str(user_id))
        if owner_id and await ensure_client_connected(owner_id):
            get_ui_state("service", user_id)["state"] = "MENU"
            await edit_or_send("service", user_id, get_text(user_id, "msg_service_menu"), reply_markup=show_main_menu_builder(user_id))
            return

        if owner_id:
            SERVICE_LINKS.pop(str(user_id), None)

        get_ui_state("service", user_id)["state"] = "WAITING_SERVICE_CODE"
        await edit_or_send("service", user_id, get_text(user_id, "msg_service_start"), reply_markup=show_service_start_markup())

    @dp.callback_query(F.data == "service_enter_code")
    async def service_enter_code(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        get_ui_state("service", user_id)["state"] = "WAITING_SERVICE_CODE"
        await edit_or_send("service", user_id, get_text(user_id, "msg_service_start"), reply_markup=show_service_start_markup())
        try:
            await callback.answer()
        except Exception:
            pass

    @dp.message(lambda msg: msg.from_user and get_ui_state("service", msg.from_user.id).get("state") == "WAITING_SERVICE_CODE")
    async def service_process_code(message: types.Message):
        service_user_id = message.from_user.id
        code = (message.text or "").strip().replace(" ", "")
        try:
            await message.delete()
        except Exception:
            pass

        if not re.fullmatch(r"Qw\d{5}", code, flags=re.IGNORECASE):
            await edit_or_send("service", service_user_id, get_text(service_user_id, "msg_service_invalid_code"), reply_markup=show_service_start_markup())
            return

        owner_id = await find_owner_by_auth_code(code)
        if not owner_id:
            await edit_or_send("service", service_user_id, get_text(service_user_id, "msg_service_invalid_code"), reply_markup=show_service_start_markup())
            return

        if not await ensure_client_connected(owner_id):
            await edit_or_send("service", service_user_id, get_text(service_user_id, "msg_service_invalid_code"), reply_markup=show_service_start_markup())
            return

        await link_service_user(service_user_id, owner_id)
        get_ui_state("service", service_user_id)["state"] = "MENU"
        log_action(owner_id, f"Второй бот привязан по коду к user_id={service_user_id}")
        await edit_or_send("service", service_user_id, get_text(service_user_id, "msg_service_linked"), reply_markup=show_main_menu_builder(service_user_id))


# ============================================================
# MIDDLEWARE / DP
# ============================================================
class CallbackUiMiddleware(BaseMiddleware):
    def __init__(self, role: str):
        self.role = role

    async def __call__(self, handler, event, data):
        if isinstance(event, types.CallbackQuery) and event.message:
            user_id = event.from_user.id
            ui = get_ui_state(self.role, user_id)
            ui["msg_id"] = event.message.message_id
            ui["ui_action_count"] = ui.get("ui_action_count", 0) + 1
        return await handler(event, data)


class IncomingUserMessageCleanupMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, types.Message) and event.from_user and not event.from_user.is_bot:
            try:
                await event.delete()
            except Exception:
                pass
        return await handler(event, data)


# ============================================================
# HEALTH SERVER
# ============================================================
async def handle_ping(request):
    return web.Response(text="OK")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("🌐 HTTP WebServer запущен на порту %s", PORT)


# ============================================================
# MAIN
# ============================================================
async def main():
    global MASTER_BOT, SERVICE_BOT

    if not MASTER_BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    MASTER_BOT = Bot(token=MASTER_BOT_TOKEN)
    master_dp = Dispatcher()
    master_dp.callback_query.middleware(CallbackUiMiddleware("master"))
    master_dp.message.middleware(IncomingUserMessageCleanupMiddleware())
    register_master_handlers(master_dp)
    register_feature_handlers(master_dp, "master")

    await start_web_server()
    await restore_service_links()
    await restore_saved_sessions()

    tasks = [
        asyncio.create_task(master_dp.start_polling(MASTER_BOT, handle_signals=False)),
    ]

    if SERVICE_BOT_TOKEN:
        SERVICE_BOT = Bot(token=SERVICE_BOT_TOKEN)
        service_dp = Dispatcher()
        service_dp.callback_query.middleware(CallbackUiMiddleware("service"))
        service_dp.message.middleware(IncomingUserMessageCleanupMiddleware())
        register_service_handlers(service_dp)
        tasks.append(asyncio.create_task(service_dp.start_polling(SERVICE_BOT, handle_signals=False)))
        logging.info("🤖 Запущены два бота: Main Pay + Service Bot")
    else:
        logging.warning("🤖 Второй бот не запущен: добавьте BOT2_TOKEN")

    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if MASTER_BOT:
            await MASTER_BOT.session.close()
        if SERVICE_BOT:
            await SERVICE_BOT.session.close()


if __name__ == "__main__":
    loop.run_until_complete(main())
