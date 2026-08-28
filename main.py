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
    "btn_delete": "Очистить сообщения 🧹", 
    "btn_turn_on": "Включить ▶️",
    "btn_turn_off": "Выключить ❌", 
    "btn_tz_select": "Выбрать часовой пояс 🕒", 
    "btn_refresh": "Обновить 🔄",
    "btn_autoresp_setup": "Изменить текст 📝",
    "btn_im_sure": "Я уверен ✅", 
    "btn_register": "Регистрироваться 📝",
    "msg_start": "Здравствуйте!\nДобро пожаловать в бота автоматизированного управления аккаунтом.\nОзнакомьтесь с правилами.",
    "msg_start_register": "Чтобы зарегистрироваться заново, нажмите кнопку ниже 👇",
    "msg_menu": "Что умеет этот бот?\nВыбирайте доступные функции управления вашим аккаунтом на кнопках снизу:",
    "msg_rules_text": "📜 **Правила использования бота:**\n\n1. Бот только для ознакомительных целей.\n2. Бот работает через юзербота.\n3. Очистка истории безвозвратна.\n4. Не авторизуйтесь слишком часто.\n5. Все действия автоматизированы.\n\n_Соблюдайте правила для безопасности._",
    "msg_rules_done": "Всё, правила прочитаны! 👍\n\nЖмите кнопку начала ниже, чтобы привязать аккаунт.",
    "msg_phone_req": "Пожалуйста, отправьте ваш номер телефона в международном формате (например, +998901234567).",
    "msg_code_req": "Код авторизации отправлен в Telegram.\n⚠️ Напишите код через пробел или дефис!",
    "msg_pwd_req": "Аккаунт защищен облачным паролем.\nВведите его в чат:",
    "msg_success_login": "Бот успешно зашел в аккаунт!\nНажмите кнопку ниже для продолжения.",
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
    "msg_limit_del": "🔒 Исчерпан дневной лимит очисток (макс 2 раза)!",
    "msg_limit_del_alert": "🚫 Превышен суточный лимит!\nОсталось: {0} ч. {1} мин.",
    "msg_activity_text": "Ваша история активности (за 5 дней):\n\n{0}",
    "msg_timenick_text": "Вывод текущего времени в имя профиля.\n\nТекущий статус: {0}\nСмещение часового пояса: UTC+{1}",
    "msg_tz_select": "Выберите ваш часовой пояс👇", 
    "msg_tz_saved": "Часовой пояс изменен на UTC+{0}!",
    "msg_autoresp_text": "🤖 **Автоответчик**\n\nСтатус: {1}\nТекст приветствия:\n👉 \"{0}\"",
    "msg_autoresp_req": "Напишите новый текст приветствия в чат 👇", 
    "msg_autoresp_saved": "Приветствие успешно сохранено! ✅",
    "msg_autoresp_default": "👋 Здравствуйте! Сейчас я не в сети, отвечу позже.",
    "msg_del_text": "🗑 Зачистка истории\nВыберите число последних сообщений для удаления:",
    "msg_del_confirm": "Вы уверены что хотите удалить последних {0} сообщений?",
    "msg_del_start": "🚀 Удаление {0} сообщений... Пожалуйста, подождите.",
    "msg_del_done": "Успешно зачищено: {0} из {1}.",
    "msg_247_text": "⚡️ **Режим 24/7**\n\nСтатус: {0}\nРаботает без суточного лимита.\nИспользовано времени: {1} ч. {2} мин.",
    "msg_limit_247_reached": "Режим 24/7 больше не имеет суточного лимита."
}

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

def get_missing_session_markup(user_id):
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
            if u_state["state"] == "START":
                uid_str = str(user_id)
                cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
                if cfg and cfg.get("logged_in", False):
                    u_state["state"] = "MENU"
        return await handler(event, data)

dp.callback_query.middleware(RestartMiddleware())

async def edit_or_send(user_id, text, reply_markup=None, parse_mode=None):
    data = get_user_state(user_id)
    if data["msg_id"]:
        try:
            await bot.edit_message_text(
                chat_id=user_id, message_id=data["msg_id"], text=text,
                reply_markup=reply_markup, parse_mode=parse_mode
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower(): return
            try:
                await bot.delete_message(chat_id=user_id, message_id=data["msg_id"])
            except Exception:
                pass
    msg = await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    data["msg_id"] = msg.message_id

    uid_str = str(user_id)
    if uid_str in MEMORY_DB["config"]:
        MEMORY_DB["config"][uid_str]["msg_id"] = msg.message_id
        asyncio.create_task(async_db_save("config", uid_str, MEMORY_DB["config"][uid_str]))

def show_start_menu(user_id):
    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_view")
    builder.button(text=get_text(user_id, "btn_start"), callback_data="start_login")
    builder.adjust(1)
    return builder.as_markup()

# === АВТООТВЕТЧИК ===
async def autoresponder_func(client, message):
    """Автоответчик для входящих личных сообщений.

    Важно: этот handler работает только когда Pyrogram-клиент запущен через
    start()/initialize(), а не просто подключён через connect().
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
        if not user_cfg:
            return
        MEMORY_DB["config"][uid_str] = user_cfg

        if not user_cfg.get("autoresponder_active", False):
            return

        sender_id = message.from_user.id
        replied_users = [int(x) for x in user_cfg.get("replied_users", [])]
        if sender_id in replied_users:
            return

        # Если владелец уже отвечал этому собеседнику, автоответ не нужен.
        try:
            recent = []
            async for msg in client.get_chat_history(sender_id, limit=10):
                recent.append(msg)
                if len(recent) >= 10:
                    break
            owner_replied = any(
                msg.from_user and msg.from_user.is_self
                for msg in recent
                if msg.id != message.id
            )
            if owner_replied:
                replied_users.append(sender_id)
                user_cfg["replied_users"] = replied_users[-1000:]
                MEMORY_DB["config"][uid_str] = user_cfg
                await async_db_save("config", uid_str, user_cfg)
                return
        except Exception as history_error:
            logging.warning(f"Не удалось проверить историю для автоответчика: {history_error}")

        custom_greeting = user_cfg.get(
            "autoresponder_greeting",
            get_text(owner_id, "msg_autoresp_default")
        )
        await client.send_message(chat_id=sender_id, text=custom_greeting)

        replied_users.append(sender_id)
        user_cfg["replied_users"] = replied_users[-1000:]
        MEMORY_DB["config"][uid_str] = user_cfg
        await async_db_save("config", uid_str, user_cfg)
        log_action(owner_id, f"Сработал автоответчик для пользователя {sender_id}")
    except Unauthorized:
        await handle_revoked_session(owner_id, reason="сессия отозвана")
    except Exception as e:
        logging.error(f"Ошибка автоответчика: {e}")


async def activity_tracker_loop(user_id):
    data = get_user_state(user_id)
    while True:
        await asyncio.sleep(60)
        if not data["client"]: break
        try:
            await data["client"].get_me()
        except Unauthorized:
            await handle_revoked_session(user_id, reason="сессия деактивирована пользователем")
            break
        except Exception:
            continue

        uid_str = str(user_id)
        if uid_str not in MEMORY_DB["activity"]: 
            MEMORY_DB["activity"][uid_str] = db_get_data("activity", uid_str) or {}
        
        today = datetime.datetime.now().strftime("%d.%m.%Y")
        if today not in MEMORY_DB["activity"][uid_str]: MEMORY_DB["activity"][uid_str][today] = 0
        MEMORY_DB["activity"][uid_str][today] += 60

        today_date = datetime.datetime.now().date()
        for date_str in list(MEMORY_DB["activity"][uid_str].keys()):
            try:
                d = datetime.datetime.strptime(date_str, "%d.%m.%Y").date()
                if (today_date - d).days > 4: del MEMORY_DB["activity"][uid_str][date_str]
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
    user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
    if not data["client"] or not data["client"].is_connected: return

    try:
        me = await data["client"].get_me()
        base_name = me.first_name or "User"
        base_name = re.sub(r'\s*\[.*?\]', '', base_name).strip()
        branding = ""
        if user_cfg.get("time_nick_active", False):
            offset = user_cfg.get("timezone_offset", 5)
            tz_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=offset)
            branding += f" [{tz_now.strftime('%H:%M')}]"
        final_name = f"{base_name}{branding}"
        if final_name != me.first_name:
            await data["client"].update_profile(first_name=final_name)
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
    data = get_user_state(user_id)
    uid_str = str(user_id)
    user_cfg = MEMORY_DB["config"].get(uid_str) or await async_db_get("config", uid_str)
    if user_cfg:
        MEMORY_DB["config"][uid_str] = user_cfg

    if not user_cfg.get("logged_in", False):
        return False

    # Основной вариант после рестарта: берём session string из Supabase.
    session_string = user_cfg.get("session_string")
    if session_string:
        try:
            client = await _build_runtime_client(user_id, session_string)
            data["client"] = client
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
        except Exception as e:
            logging.error(f"Ошибка восстановления session string для {user_id}: {e}")
            await handle_revoked_session(user_id, reason="сессия недействительна")
            return False

    # Миграция старой локальной .session, если она ещё есть.
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
        device_model="PC",
        system_version="Windows 11",
        app_version="4.15.0",
        lang_code="ru",
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
    except Exception as e:
        await close_pyrogram_client(client)
        logging.error(f"Ошибка миграции локальной сессии: {e}")
        await handle_revoked_session(user_id, reason="сессия недействительна")
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

        # Не поднимаем все обычные сессии без активных функций.
        # Это экономит соединения, но автоответчик/24/7/время в профиль переживают рестарт.
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
    try:
        await message.delete()
    except Exception:
        pass

    if data.get("msg_id"):
        try:
            await bot.delete_message(chat_id=user_id, message_id=data["msg_id"])
        except Exception:
            pass
        data["msg_id"] = None

    if uid_str not in MEMORY_DB["config"]:
        MEMORY_DB["config"][uid_str] = db_get_data("config", uid_str) or {
            "phone": "Не указан", "password": "Нет", "status_24_7": False,
            "time_nick_active": False, "autoresponder_active": False,
            "autoresponder_greeting": get_text(user_id, "msg_autoresp_default"),
            "timezone_offset": 5, "delete_today_count": 0, "delete_limit_reset_ts": 0.0,
            "used_247_seconds": 0.0, "last_247_start_ts": 0.0, "used_timenick_seconds": 0.0,
            "replied_users": [], "username": message.from_user.username or "N/A",
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
    await edit_or_send(user_id, get_text(user_id, "msg_start_register"), reply_markup=show_start_menu(user_id))
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data == "start_login")
async def start_login(callback: types.CallbackQuery):
    user_id = callback.from_user.id
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
    phone = message.text.strip().replace(" ", "")
    try: await message.delete()
    except Exception: pass

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
        device_model="PC", system_version="Windows 11", app_version="4.15.0",
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
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back"), callback_data="cancel_auth")
        await edit_or_send(user_id, get_text(user_id, "msg_limit_tg", e.value), reply_markup=builder.as_markup(), parse_mode="Markdown")
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
        "used_247_seconds": old_cfg.get("used_247_seconds", 0.0),
        "last_247_start_ts": old_cfg.get("last_247_start_ts", 0.0),
        "used_timenick_seconds": old_cfg.get("used_timenick_seconds", 0.0),
        "replied_users": old_cfg.get("replied_users", []),
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

    try: await message.delete()
    except Exception: pass

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

    try: await message.delete()
    except Exception: pass

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
    user_cfg = MEMORY_DB["config"].get(str(user_id), {})
    now = time.time()
    reset_ts = user_cfg.get("delete_limit_reset_ts", 0.0)
    day_count = user_cfg.get("delete_today_count", 0)
    if now >= reset_ts: day_count = 0

    builder.button(text=get_text(user_id, "btn_activity"), callback_data="menu_activity")
    builder.button(text=get_text(user_id, "btn_autoresp"), callback_data="menu_autoresponder")
    builder.button(text=get_text(user_id, "btn_timenick"), callback_data="menu_timenick")

    builder.button(text=get_text(user_id, "btn_247"), callback_data="menu_247")

    if day_count >= 2:
        btn_del_clean = get_text(user_id, 'btn_delete').replace('🧹', '').strip()
        builder.button(text=f"{btn_del_clean} 🚫", callback_data="del_limit_blocked")
    else:
        builder.button(text=get_text(user_id, "btn_delete"), callback_data="menu_delete")

    builder.button(text=get_text(user_id, "btn_rules"), callback_data="rules_menu_view")

    builder.adjust(2, 2, 2)
    return builder

@dp.callback_query(F.data == "del_limit_blocked")
async def del_limit_blocked_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_cfg = MEMORY_DB["config"].get(str(user_id), {})
    reset_ts = user_cfg.get("delete_limit_reset_ts", 0.0)
    rem = max(0, int(reset_ts - time.time()))
    hours, minutes = rem // 3600, (rem % 3600) // 60
    try: await callback.answer(get_text(user_id, "msg_limit_del_alert", hours, minutes), show_alert=True)
    except Exception: pass

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
    try: await message.delete()
    except Exception: pass

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
    text = get_text(user_id, "msg_timenick_text", status, offset)

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

# === ОЧИСТКА СООБЩЕНИЙ ===
@dp.callback_query(F.data == "menu_delete")
async def menu_delete(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    builder = InlineKeyboardBuilder()
    builder.button(text="10", callback_data="confirm_del_10")
    builder.button(text="50", callback_data="confirm_del_50")
    builder.button(text="100", callback_data="confirm_del_100")
    builder.button(text="200", callback_data="confirm_del_200")
    builder.button(text="500", callback_data="confirm_del_500")
    builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
    builder.adjust(2, 2, 2)
    await edit_or_send(user_id, get_text(user_id, "msg_del_text"), reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data.startswith("confirm_del_"))
async def confirm_del(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    count = int(callback.data.split("_")[-1])
    data = get_user_state(user_id)
    data["delete_count"] = count

    builder = InlineKeyboardBuilder()
    builder.button(text=get_text(user_id, "btn_im_sure"), callback_data="execute_delete")
    builder.button(text=get_text(user_id, "btn_back"), callback_data="menu_delete")
    builder.adjust(1)
    await edit_or_send(user_id, get_text(user_id, "msg_del_confirm", count), reply_markup=builder.as_markup())
    try: await callback.answer()
    except Exception: pass

@dp.callback_query(F.data == "execute_delete")
async def execute_delete(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = get_user_state(user_id)
    client = data.get("client")
    count = data.get("delete_count", 100)

    if not client or not client.is_connected:
        await edit_or_send(user_id, get_text(user_id, "msg_session_missing"), reply_markup=get_missing_session_markup(user_id))
        return

    await edit_or_send(user_id, get_text(user_id, "msg_del_start", count))
    
    deleted = 0
    try:
        msg_ids = []
        async for msg in client.get_chat_history("me", limit=count):
            msg_ids.append(msg.id)
            if len(msg_ids) >= 100:
                await client.delete_messages("me", msg_ids)
                deleted += len(msg_ids)
                msg_ids = []
                await asyncio.sleep(1)
        if msg_ids:
            await client.delete_messages("me", msg_ids)
            deleted += len(msg_ids)

        uid_str = str(user_id)
        user_cfg = MEMORY_DB["config"].get(uid_str) or db_get_data("config", uid_str)
        user_cfg["delete_today_count"] = user_cfg.get("delete_today_count", 0) + 1
        if user_cfg.get("delete_today_count", 0) >= 2:
            user_cfg["delete_limit_reset_ts"] = time.time() + 86400
        MEMORY_DB["config"][uid_str] = user_cfg
        asyncio.create_task(async_db_save("config", uid_str, user_cfg))

        log_action(user_id, f"Удалено {deleted} сообщений в 'Избранное'")
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
        await edit_or_send(user_id, get_text(user_id, "msg_del_done", deleted, count), reply_markup=builder.as_markup())
    except Exception as e:
        builder = InlineKeyboardBuilder()
        builder.button(text=get_text(user_id, "btn_back_menu"), callback_data="main_menu")
        await edit_or_send(user_id, f"Ошибка при удалении: {e}", reply_markup=builder.as_markup())

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
