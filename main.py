import asyncio
import sys
import os
import datetime
import re
import psutil
import time
import random
import logging
from typing import Dict, Any, Optional, List, Tuple
from aiohttp import web
from supabase import create_client, Client as SupabaseClient

if sys.platform != "win32":
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from pyrogram import Client, enums, filters
from pyrogram.handlers import MessageHandler, DeletedMessagesHandler
from pyrogram.raw import functions
from pyrogram.errors import (
    SessionPasswordNeeded, 
    Unauthorized, 
    PhoneCodeInvalid, 
    PasswordHashInvalid, 
    AuthKeyUnregistered,
    FloodWait
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("UserbotController")

LANG: Dict[str, str] = {
    "btn_start": "Начинаем 🚀",
    "btn_rules": "Правила 📜",
    "btn_back": "Назад 🔙",
    "btn_back_menu": "Главное меню 🏠",
    "btn_confirm": "Подтвердить ✅",
    "btn_activity": "Активность 📊",
    "btn_autoresp": "Автоответчик 🤖",
    "btn_timenick": "Время в профиль 🕒",
    "btn_247": "Режим 24/7 ⚡️",
    "btn_delete": "Очистить историю 🧹",
    "btn_turn_on": "Включить ▶️",
    "btn_turn_off": "Выключить ❌",
    "btn_tz_select": "Часовой пояс 🌐",
    "btn_refresh": "Обновить 🔄",
    "btn_autoresp_setup": "Текст Приветствия 📝",
    "btn_block_menu": "Блокировка PIN 🔒",
    "btn_register": "Регистрация 📝",
    "btn_custom_nick": "Оформление Ника ✨",
    "btn_time": "Настройка Времени 🕒",
    "btn_lock_now": "Заблокировать Сейчас 🔒",
    "btn_reset_autoresp_users": "Сбросить Список Ответов 🔄",
    "status_on": "Включен 🟢",
    "status_off": "Выключен 🔴",
    "msg_start": "👋 **Добро пожаловать в центр управления юзерботом!**\n\nСистема позволяет автоматизировать ваш аккаунт Telegram:\n• Автоматическое обновление времени в никнейме\n• Умный автоответчик для входящих сообщений\n• Поддержание постоянного онлайна 24/7\n• Журналирование и автоочистка диалогов\n• Защита меню управления PIN-кодом\n\nОзнакомьтесь с правилами перед стартом.",
    "msg_menu": "🎛 **Главное меню управления**\n\nВыберите необходимый раздел конфигурации вашим юзерботом ниже:",
    "msg_rules_text": "📜 **Правила и условия использования:**\n\n1. Бот функционирует через авторизацию Telegram Client API.\n2. Сессионные данные шифруются и хранятся в защищенном хранилище.\n3. Запрещено использовать юзербота для рассылки спама и массовых нарушений.\n4. Вы в любой момент можете завершить сессию через меню настройки безопасности.\n\n_Статус доступа: Неограниченный (Unlimited)._",
    "msg_phone_req": "📱 **Авторизация по номеру телефона**\n\nОтправьте ваш номер телефона в международном формате.\nПример: `+79991234567` или `+380991234567`",
    "msg_code_req": "📨 **Ввод кода подтверждения**\n\nКод авторизации отправлен в ваше приложение Telegram.\nВведите полученный код в чат цифрами (например: `12345`).",
    "msg_pwd_req": "🔐 **Двухфакторная аутентификация (2FA)**\n\nВаш аккаунт защищен облачным паролем.\nВведите ваш пароль 2FA в чат для завершения входа:",
    "msg_success_login": "🎉 **Авторизация успешно завершена!**\n\nЮзербот подключен и готов к работе.\nНажмите кнопку ниже, чтобы перейти в главное меню.",
    "msg_btn_go": "Перейти в меню ➡️",
    "msg_autoresp_default": "👋 Здравствуйте! Сейчас я не в сети или занят. Отвечу вам сразу, как освобожусь.",
    "msg_del_text": "🧹 **Очистка истории сообщений**\n\nВыберите количество последних сообщений для удаления из диалога с ботом:",
    "msg_session_revoked": "⚠️ **Сессия юзербота была завершена или аннулирована.**\n\nПожалуйста, пройдите процедуру повторной регистрации для возобновления работы.",
    "msg_block_setup": "🔐 **Установка PIN-кода**\n\nВведите 4-значный цифровой PIN-код для защиты меню управления:",
    "msg_unlock_req": "🔒 **Доступ заблокирован**\n\nВведите ваш 4-значный PIN-код для входа в меню управления:"
}

ZONES: List[Tuple[str, float]] = [
    ("Лондон / UTC+0", 0.0),
    ("Европа / UTC+1", 1.0),
    ("Киев, Минск / UTC+2", 2.0),
    ("Москва, СПб / UTC+3", 3.0),
    ("Самара, Баку / UTC+4", 4.0),
    ("Ташкент, Екатеринбург / UTC+5", 5.0),
    ("Омск, Астана / UTC+6", 6.0),
    ("Красноярск / UTC+7", 7.0),
    ("Иркутск, Пекин / UTC+8", 8.0),
    ("Токио, Якутск / UTC+9", 9.0)
]

STYLES: List[Tuple[str, int]] = [
    ("Стандартный [10:30]", 1),
    ("Жирный 𝟏𝟎:𝟑𝟎", 2),
    ("Двойной 𝟙𝟘:𝟛𝟘", 3),
    ("Без засечек 𝟢𝟣:𝟤𝟥", 4),
    ("Моноширинный 𝟶𝟷:𝸸𝟹", 5),
    ("Иконка ⌚ 10:30", 6),
    ("Звезды ★ 10:30 ★", 7),
    ("Украшенный ꧁ 10:30 ꧂", 8)
]

class DatabaseManager:
    def __init__(self, url: str, key: str):
        self.client: SupabaseClient = create_client(url, key)

    async def get_config(self, user_id: int, username: str = "", first_name: str = "") -> Dict[str, Any]:
        try:
            res = self.client.table("user_configs").select("*").eq("user_id", user_id).execute()
            if not res.data:
                default_cfg = {
                    "user_id": user_id,
                    "username": username or "",
                    "first_name": first_name or "Пользователь",
                    "status_24_7": False,
                    "time_nick_active": False,
                    "autoresponder_active": False,
                    "autoresponder_greeting": LANG["msg_autoresp_default"],
                    "timezone_offset": 5.0,
                    "timezone_name": "Ташкент, Екатеринбург / UTC+5",
                    "replied_users": [],
                    "is_menu_locked": False,
                    "menu_lock_code": None,
                    "logged_in": False,
                    "last_interaction_time": time.time(),
                    "custom_nick_style": 1
                }
                self.client.table("user_configs").insert(default_cfg).execute()
                return default_cfg
            else:
                data = res.data[0]
                updates = {}
                if username and data.get("username") != username:
                    updates["username"] = username
                if first_name and data.get("first_name") != first_name:
                    updates["first_name"] = first_name
                if updates:
                    self.client.table("user_configs").update(updates).eq("user_id", user_id).execute()
                    data.update(updates)
                return data
        except Exception as e:
            logger.error(f"Error fetching config for {user_id}: {e}")
            return {
                "user_id": user_id,
                "status_24_7": False,
                "time_nick_active": False,
                "autoresponder_active": False,
                "autoresponder_greeting": LANG["msg_autoresp_default"],
                "timezone_offset": 5.0,
                "timezone_name": "Ташкент, Екатеринбург / UTC+5",
                "replied_users": [],
                "is_menu_locked": False,
                "menu_lock_code": None,
                "logged_in": False,
                "last_interaction_time": time.time(),
                "custom_nick_style": 1
            }

    async def update_config(self, user_id: int, updates: Dict[str, Any]) -> bool:
        try:
            self.client.table("user_configs").update(updates).eq("user_id", user_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating config for {user_id}: {e}")
            return False

    async def get_session(self, user_id: int) -> Optional[str]:
        try:
            res = self.client.table("user_sessions").select("session_string").eq("user_id", user_id).execute()
            return res.data[0]["session_string"] if res.data else None
        except Exception as e:
            logger.error(f"Error getting session for {user_id}: {e}")
            return None

    async def save_session(self, user_id: int, session_string: str, phone: str) -> bool:
        try:
            res = self.client.table("user_sessions").select("user_id").eq("user_id", user_id).execute()
            if res.data:
                self.client.table("user_sessions").update({"session_string": session_string, "phone": phone}).eq("user_id", user_id).execute()
            else:
                self.client.table("user_sessions").insert({"user_id": user_id, "session_string": session_string, "phone": phone}).execute()
            return True
        except Exception as e:
            logger.error(f"Error saving session for {user_id}: {e}")
            return False

    async def drop_session(self, user_id: int) -> None:
        try:
            self.client.table("user_sessions").delete().eq("user_id", user_id).execute()
            await self.update_config(user_id, {"logged_in": False})
        except Exception as e:
            logger.error(f"Error dropping session for {user_id}: {e}")

    async def update_daily_stats(self, stat_type: str) -> None:
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            res = self.client.table("daily_stats").select("*").eq("date", today).execute()
            if res.data:
                curr = res.data[0].get(stat_type) or 0
                self.client.table("daily_stats").update({stat_type: curr + 1}).eq("date", today).execute()
            else:
                inc_val = 1 if stat_type == "incoming" else 0
                act_val = 1 if stat_type == "active" else 0
                self.client.table("daily_stats").insert({"date": today, "incoming": inc_val, "active": act_val}).execute()
        except Exception as e:
            logger.error(f"Error updating daily stats: {e}")

    async def log_pm_message(self, user_id: int, chat_id: int, msg_id: int, sender_id: int, sender_name: str, text: str, is_media: bool, media_type: str, date_iso: str, is_deleted: bool = False) -> None:
        try:
            res = self.client.table("messages_log").select("id").eq("user_id", user_id).eq("msg_id", msg_id).execute()
            if res.data:
                if is_deleted:
                    self.client.table("messages_log").update({"is_deleted": True}).eq("id", res.data[0]["id"]).execute()
            else:
                log_data = {
                    "user_id": user_id, "chat_id": chat_id, "msg_id": msg_id,
                    "sender_id": sender_id, "sender_name": sender_name,
                    "text": text, "is_deleted": is_deleted, "date": date_iso,
                    "is_media": is_media, "media_type": media_type
                }
                self.client.table("messages_log").insert(log_data).execute()
        except Exception as e:
            logger.error(f"Error logging PM message: {e}")

    async def mark_message_deleted(self, user_id: int, msg_id: int) -> None:
        try:
            self.client.table("messages_log").update({"is_deleted": True}).eq("user_id", user_id).eq("msg_id", msg_id).execute()
        except Exception as e:
            logger.error(f"Error marking message deleted: {e}")

    async def get_activity_data(self, user_id: int) -> Dict[str, int]:
        try:
            res = self.client.table("user_activity").select("activity_data").eq("user_id", user_id).execute()
            return res.data[0]["activity_data"] if res.data and "activity_data" in res.data[0] else {}
        except Exception as e:
            logger.error(f"Error fetching activity data: {e}")
            return {}

    async def update_activity_data(self, user_id: int, activity_data: Dict[str, int]) -> None:
        try:
            res = self.client.table("user_activity").select("activity_data").eq("user_id", user_id).execute()
            if res.data:
                self.client.table("user_activity").update({"activity_data": activity_data}).eq("user_id", user_id).execute()
            else:
                self.client.table("user_activity").insert({"user_id": user_id, "activity_data": activity_data}).execute()
        except Exception as e:
            logger.error(f"Error updating activity data: {e}")

db = DatabaseManager(SUPABASE_URL, SUPABASE_KEY)
USER_DATA: Dict[int, Dict[str, Any]] = {}

def get_user_state(user_id: int) -> Dict[str, Any]:
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {
            "msg_id": None,
            "phone": None,
            "password": None,
            "phone_code_hash": None,
            "client": None,
            "temp_client": None,
            "state": "START",
            "time_nick_active": False,
            "time_nick_task": None,
            "status_24_7": False,
            "task_24_7": None,
            "activity_task": None,
            "is_menu_locked": False,
            "last_interaction_time": time.time()
        }
    return USER_DATA[user_id]

def strip_time_nick(name: Optional[str]) -> str:
    if not name:
        return "User"
    cleaned = re.sub(r'\s*(\[.*?\]|⌚.*|⏳.*|★.*|꧁.*?꧂|[\d𝟎-𝟗𝟘-𝟡𝟢-𝟫𝟶-𝟿]+[:∶][\d𝟎-𝟗𝟘-𝟡𝟢-𝟫𝟶-𝟿]+)$', '', name)
    cleaned = cleaned.replace("꧁ ", "").replace(" ꧂", "").replace("★ ", "").replace(" ⌚", "")
    return cleaned.strip() or "User"

def apply_custom_nick(base_name: str, time_str: str, style_idx: int) -> str:
    bold_map = str.maketrans("0123456789", "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗")
    double_map = str.maketrans("0123456789", "𝟘𝟙𝟚𝟛𝟜𝟝𝞮𝟟𝟠𝟡")
    sans_map = str.maketrans("0123456789", "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫")
    mono_map = str.maketrans("0123456789", "𝟶𝟷𝸸𝟹𝟺𝟻𝟼𝟽𝟾𝟿")
    
    if style_idx == 1:
        return f"{base_name} [{time_str}]"
    elif style_idx == 2:
        return f"{base_name} {time_str.translate(bold_map)}"
    elif style_idx == 3:
        return f"{base_name} {time_str.translate(double_map)}"
    elif style_idx == 4:
        return f"{base_name} {time_str.translate(sans_map)}"
    elif style_idx == 5:
        return f"{base_name} {time_str.translate(mono_map)}"
    elif style_idx == 6:
        return f"⌚ {time_str} | {base_name}"
    elif style_idx == 7:
        return f"★ {base_name} ({time_str.translate(bold_map)}) ★"
    elif style_idx == 8:
        return f"꧁ {base_name} ꧂ [{time_str}]"
    return f"{base_name} [{time_str}]"

async def log_pm_message_handler(client, message):
    if not message.chat or message.chat.type != enums.ChatType.PRIVATE:
        return
    user_id = getattr(client, "owner_id", 0)
    if not user_id:
        return
    chat_id = message.chat.id
    msg_id = message.id
    sender_id = message.from_user.id if message.from_user else 0
    sender_name = message.from_user.first_name if message.from_user else "Unknown"
    text = message.text or message.caption or ""
    is_media = bool(message.media)
    media_type = f"[{message.media.value}]" if is_media else ""
    date_iso = message.date.isoformat() if message.date else datetime.datetime.now().isoformat()
    await db.log_pm_message(user_id, chat_id, msg_id, sender_id, sender_name, text, is_media, media_type, date_iso, False)

async def process_autoresponder(client, message):
    if not message.chat or message.chat.type != enums.ChatType.PRIVATE:
        return
    if message.from_user and (message.from_user.is_self or message.from_user.is_bot):
        return
    
    user_id = getattr(client, "owner_id", 0)
    if not user_id:
        return
    
    cfg = await db.get_config(user_id)
    if not cfg.get("autoresponder_active"):
        return
    
    sender_id = message.from_user.id
    replied = cfg.get("replied_users", [])
    if sender_id in replied:
        return

    my_last, their_last = 0.0, 0.0
    try:
        async for msg in client.get_chat_history(sender_id, limit=15):
            if msg.from_user and msg.from_user.is_self:
                if not my_last and msg.date:
                    my_last = msg.date.timestamp()
            else:
                if not their_last and msg.date:
                    their_last = msg.date.timestamp()
    except Exception as e:
        logger.error(f"Error fetching chat history for autoresponder: {e}")

    if my_last > their_last:
        return

    custom_greeting = cfg.get("autoresponder_greeting", LANG["msg_autoresp_default"])
    try:
        await client.send_message(chat_id=sender_id, text=custom_greeting)
        if isinstance(replied, list):
            replied.append(sender_id)
            await db.update_config(user_id, {"replied_users": replied})
    except Exception as e:
        logger.error(f"Error sending autoresponder message: {e}")

async def on_new_message(client, message):
    await process_autoresponder(client, message)
    await log_pm_message_handler(client, message)

async def on_deleted_message(client, messages):
    user_id = getattr(client, "owner_id", 0)
    if not user_id:
        return
    for msg in messages:
        if msg.chat and msg.chat.type == enums.ChatType.PRIVATE:
            await db.mark_message_deleted(user_id, msg.id)

async def keep_online_loop(user_id: int):
    data = get_user_state(user_id)
    while data["status_24_7"]:
        try:
            if not data["client"] or not data["client"].is_connected:
                break
            await data["client"].invoke(functions.account.UpdateStatus(offline=False))
        except Unauthorized:
            logger.warning(f"Unauthorized error in keep_online_loop for user {user_id}")
            await handle_revoked_session(user_id)
            break
        except Exception as e:
            logger.error(f"Error in keep_online_loop for {user_id}: {e}")
        await asyncio.sleep(30)

async def time_nickname_loop(user_id: int):
    data = get_user_state(user_id)
    while data["time_nick_active"]:
        try:
            if not data["client"] or not data["client"].is_connected:
                break
            me = await data["client"].get_me()
            cfg = await db.get_config(user_id)
            offset = float(cfg.get("timezone_offset", 5.0))
            style = int(cfg.get("custom_nick_style", 1))
            
            tz_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=offset)
            time_str = tz_now.strftime('%H:%M')
            
            base_name = strip_time_nick(me.first_name)
            final_name = apply_custom_nick(base_name, time_str, style)
            
            if final_name != me.first_name:
                await data["client"].update_profile(first_name=final_name)
        except Unauthorized:
            logger.warning(f"Unauthorized error in time_nickname_loop for user {user_id}")
            await handle_revoked_session(user_id)
            break
        except Exception as e:
            logger.error(f"Error in time_nickname_loop for {user_id}: {e}")
        await asyncio.sleep(60)

async def activity_tracker_loop(user_id: int):
    data = get_user_state(user_id)
    while True:
        try:
            if not data["client"] or not data["client"].is_connected:
                break
            auths = await data["client"].invoke(functions.account.GetAuthorizations())
            now_ts = time.time()
            is_active = any((now_ts - getattr(a, "date_active", 0)) < 120 for a in auths.authorizations)
            
            if is_active:
                today = datetime.datetime.now().strftime("%d.%m.%Y")
                act_data = await db.get_activity_data(user_id)
                act_data[today] = act_data.get(today, 0) + 1
                
                today_date = datetime.datetime.now().date()
                keys_to_del = []
                for k in list(act_data.keys()):
                    try:
                        k_date = datetime.datetime.strptime(k, "%d.%m.%Y").date()
                        if (today_date - k_date).days > 7:
                            keys_to_del.append(k)
                    except ValueError:
                        keys_to_del.append(k)
                for k in keys_to_del:
                    act_data.pop(k, None)
                    
                await db.update_activity_data(user_id, act_data)
        except Exception as e:
            logger.error(f"Error in activity_tracker_loop for {user_id}: {e}")
        await asyncio.sleep(60)

async def ensure_client_connected(user_id: int) -> bool:
    data = get_user_state(user_id)
    cfg = await db.get_config(user_id)
    data["is_menu_locked"] = cfg.get("is_menu_locked", False)
    data["last_interaction_time"] = cfg.get("last_interaction_time", time.time())
    
    if data["client"] and data["client"].is_connected:
        return True

    session_str = await db.get_session(user_id)
    if session_str:
        if data["client"]:
            try:
                await data["client"].disconnect()
            except Exception:
                pass
        client = Client(f"user_{user_id}", session_string=session_str, api_id=API_ID, api_hash=API_HASH, ipv6=False, in_memory=True)
        setattr(client, "owner_id", user_id)
        client.add_handler(MessageHandler(on_new_message, filters.private))
        client.add_handler(DeletedMessagesHandler(on_deleted_message, filters.private))
        data["client"] = client
        try:
            await client.connect()
            await client.get_me()
            
            if not cfg.get("logged_in"):
                await db.update_config(user_id, {"logged_in": True})

            if not data.get("activity_task") or data["activity_task"].done():
                data["activity_task"] = asyncio.create_task(activity_tracker_loop(user_id))
            if cfg.get("status_24_7") and (not data.get("task_24_7") or data["task_24_7"].done()):
                data["status_24_7"] = True
                data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))
            if cfg.get("time_nick_active") and (not data.get("time_nick_task") or data["time_nick_task"].done()):
                data["time_nick_active"] = True
                data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
            return True
        except Exception as e:
            logger.error(f"Failed to connect client for {user_id}: {e}")
            await handle_revoked_session(user_id)
            return False
    return False

async def handle_revoked_session(user_id: int):
    data = get_user_state(user_id)
    for task_name in ["time_nick_task", "task_24_7", "activity_task"]:
        task = data.get(task_name)
        if task and not task.done():
            task.cancel()
        data[task_name] = None
    
    data.update({"time_nick_active": False, "status_24_7": False, "state": "START"})
    if data["client"]:
        try:
            await data["client"].disconnect()
        except Exception:
            pass
        data["client"] = None

    await db.drop_session(user_id)
    builder = InlineKeyboardBuilder().button(text=LANG["btn_register"], callback_data="start_login")
    await edit_or_send(user_id, LANG["msg_session_revoked"], reply_markup=builder.as_markup())

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def safe_delete_message(message: types.Message, delay: int = 0):
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

class LockMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = None
        if isinstance(event, types.CallbackQuery):
            user_id = event.from_user.id if event.from_user else None
            if event.message and user_id:
                get_user_state(user_id)["msg_id"] = event.message.message_id
        elif isinstance(event, types.Message):
            user_id = event.from_user.id if event.from_user else None
            if event.chat.type == "private":
                asyncio.create_task(safe_delete_message(event, 1))
            
        if user_id:
            now = time.time()
            u_state = get_user_state(user_id)
            cfg = await db.get_config(user_id)
            has_pin = bool(cfg.get("menu_lock_code"))
            last_active = u_state.get("last_interaction_time", now)
            
            if has_pin:
                if u_state.get("state") == "WAITING_UNLOCK_CODE" or (now - last_active >= 300):
                    u_state["state"] = "WAITING_UNLOCK_CODE"
                    if isinstance(event, types.CallbackQuery):
                        await edit_or_send(user_id, LANG["msg_unlock_req"])
                        return
            
            await db.update_config(user_id, {"last_interaction_time": now})
            u_state["last_interaction_time"] = now
            
        return await handler(event, data)

dp.update.middleware(LockMiddleware())

async def background_lock_monitor():
    while True:
        try:
            await asyncio.sleep(15)
            now = time.time()
            for uid, data in list(USER_DATA.items()):
                cfg = await db.get_config(uid)
                has_pin = bool(cfg.get("menu_lock_code"))
                if has_pin and data.get("state") != "WAITING_UNLOCK_CODE":
                    last_active = data.get("last_interaction_time", now)
                    if now - last_active >= 300:
                        data["state"] = "WAITING_UNLOCK_CODE"
                        try:
                            await edit_or_send(uid, LANG["msg_unlock_req"])
                        except Exception:
                            pass
        except Exception as e:
            logger.error(f"Error in background lock monitor: {e}")

async def edit_or_send(user_id: int, text: str, reply_markup=None, parse_mode: Optional[str] = None):
    data = get_user_state(user_id)
    if data["msg_id"]:
        try:
            await bot.edit_message_text(chat_id=user_id, message_id=data["msg_id"], text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return
            data["msg_id"] = None
        except Exception:
            data["msg_id"] = None
    
    try:
        msg = await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        data["msg_id"] = msg.message_id
    except Exception as e:
        logger.error(f"Failed to send message to {user_id}: {e}")

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    data = get_user_state(user_id)
    now = time.time()
    
    cfg = await db.get_config(user_id, username=message.from_user.username or "", first_name=message.from_user.first_name or "")
    has_pin = bool(cfg.get("menu_lock_code"))
    last_active = data.get("last_interaction_time", now)
    
    if has_pin and (data.get("state") == "WAITING_UNLOCK_CODE" or (now - last_active >= 300)):
        data["state"] = "WAITING_UNLOCK_CODE"
        await edit_or_send(user_id, LANG["msg_unlock_req"])
        return

    data["state"] = "START"
    await db.update_daily_stats('incoming')

    if await ensure_client_connected(user_id):
        await show_main_menu(user_id)
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text=LANG["btn_rules"], callback_data="rules_view")
        builder.button(text=LANG["btn_start"], callback_data="start_login")
        builder.adjust(1)
        await edit_or_send(user_id, LANG["msg_start"], reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "rules_view")
async def rules_view(cb: types.CallbackQuery):
    builder = InlineKeyboardBuilder().button(text="Я ознакомился 👍", callback_data="rules_accepted")
    await edit_or_send(cb.from_user.id, LANG["msg_rules_text"], reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "rules_accepted")
async def rules_accepted(cb: types.CallbackQuery):
    if await ensure_client_connected(cb.from_user.id):
        await show_main_menu(cb.from_user.id)
    else:
        await start_login(cb)

@dp.callback_query(F.data == "start_login")
async def start_login(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    if await ensure_client_connected(user_id):
        await show_main_menu(user_id)
        return
    get_user_state(user_id)["state"] = "WAITING_PHONE"
    builder = InlineKeyboardBuilder().button(text=LANG["btn_back"], callback_data="cancel_auth")
    await edit_or_send(user_id, LANG["msg_phone_req"], reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "cancel_auth")
async def cancel_auth(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    data = get_user_state(user_id)
    data["state"] = "START"
    if data["temp_client"]:
        try:
            await data["temp_client"].disconnect()
        except Exception:
            pass
        data["temp_client"] = None
    if cb.message:
        await cmd_start(cb.message)

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_PHONE")
async def process_phone(message: types.Message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    phone = message.text.strip().replace(" ", "")
    
    if not phone.startswith("+"):
        phone = "+" + phone
    phone = re.sub(r'[^\d+]', '', phone)
    
    if len(phone) < 8:
        msg_err = await message.answer("❌ Некорректный формат телефона! Отправьте номер вида +79991234567")
        asyncio.create_task(safe_delete_message(msg_err, 4))
        return
    
    data = get_user_state(user_id)
    data["phone"] = phone
    data["state"] = "WAITING_CODE"
    
    if data["temp_client"]:
        try:
            await data["temp_client"].disconnect()
        except Exception:
            pass
            
    temp_client = Client(f"temp_{user_id}", api_id=API_ID, api_hash=API_HASH, in_memory=True)
    data["temp_client"] = temp_client
    
    try:
        await temp_client.connect()
        code_info = await temp_client.send_code(phone)
        data["phone_code_hash"] = code_info.phone_code_hash
        builder = InlineKeyboardBuilder().button(text=LANG["btn_back"], callback_data="cancel_auth")
        await edit_or_send(user_id, LANG["msg_code_req"], reply_markup=builder.as_markup(), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error sending code to {phone}: {e}")
        data["state"] = "WAITING_PHONE"
        builder = InlineKeyboardBuilder().button(text=LANG["btn_back"], callback_data="cancel_auth")
        await edit_or_send(user_id, f"❌ Ошибка отправки кода: {e}\n\nПопробуйте ввести номер заново:", reply_markup=builder.as_markup())

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_CODE")
async def process_code(message: types.Message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    code = re.sub(r'\D', '', message.text.strip())
    
    data = get_user_state(user_id)
    temp_client = data.get("temp_client")
    if not temp_client:
        data["state"] = "WAITING_PHONE"
        await edit_or_send(user_id, "⚠️ Сессия авторизации истекла. Отправьте номер заново.")
        return
    
    try:
        await temp_client.sign_in(data["phone"], data["phone_code_hash"], code)
        await finish_login(user_id, temp_client)
    except SessionPasswordNeeded:
        data["state"] = "WAITING_PASSWORD"
        builder = InlineKeyboardBuilder().button(text=LANG["btn_back"], callback_data="cancel_auth")
        await edit_or_send(user_id, LANG["msg_pwd_req"], reply_markup=builder.as_markup(), parse_mode="Markdown")
    except PhoneCodeInvalid:
        msg_err = await message.answer("❌ Неверный код подтверждения! Проверьте и введите снова.")
        asyncio.create_task(safe_delete_message(msg_err, 4))
    except Exception as e:
        logger.error(f"Error entering code for {user_id}: {e}")
        msg_err = await message.answer(f"❌ Ошибка входа: {e}")
        asyncio.create_task(safe_delete_message(msg_err, 4))

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_PASSWORD")
async def process_password(message: types.Message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    pwd = message.text.strip()
    
    data = get_user_state(user_id)
    temp_client = data.get("temp_client")
    if not temp_client:
        data["state"] = "WAITING_PHONE"
        await edit_or_send(user_id, "⚠️ Сессия авторизации истекла. Отправьте номер заново.")
        return
        
    try:
        await temp_client.check_password(pwd)
        await finish_login(user_id, temp_client)
    except PasswordHashInvalid:
        msg_err = await message.answer("❌ Неверный облачный пароль! Попробуйте еще раз.")
        asyncio.create_task(safe_delete_message(msg_err, 4))
    except Exception as e:
        logger.error(f"Error checking password for {user_id}: {e}")
        msg_err = await message.answer(f"❌ Ошибка пароля: {e}")
        asyncio.create_task(safe_delete_message(msg_err, 4))

async def finish_login(user_id: int, client: Client):
    data = get_user_state(user_id)
    session_str = await client.export_session_string()
    phone = data.get("phone", "")
    
    try:
        await client.disconnect()
    except Exception:
        pass
    data["temp_client"] = None
    
    await db.save_session(user_id, session_str, phone)
    await db.update_config(user_id, {"logged_in": True})
    await db.update_daily_stats('active')
    
    await ensure_client_connected(user_id)
    
    data["state"] = "MENU"
    builder = InlineKeyboardBuilder().button(text=LANG["msg_btn_go"], callback_data="main_menu")
    await edit_or_send(user_id, LANG["msg_success_login"], reply_markup=builder.as_markup(), parse_mode="Markdown")

async def show_main_menu(user_id: int):
    get_user_state(user_id)["state"] = "MENU"
    builder = InlineKeyboardBuilder()
    builder.button(text=LANG["btn_activity"], callback_data="menu_activity")
    builder.button(text=LANG["btn_autoresp"], callback_data="menu_autoresponder")
    builder.button(text=LANG["btn_timenick"], callback_data="menu_profile_settings")
    builder.button(text=LANG["btn_247"], callback_data="toggle_247")
    builder.button(text=LANG["btn_delete"], callback_data="menu_delete")
    builder.button(text=LANG["btn_block_menu"], callback_data="menu_block_settings")
    builder.adjust(2, 2, 2)
    await edit_or_send(user_id, LANG["msg_menu"], reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: types.CallbackQuery):
    await show_main_menu(cb.from_user.id)

@dp.callback_query(F.data == "menu_profile_settings")
async def menu_profile_settings(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    builder = InlineKeyboardBuilder()
    builder.button(text=LANG["btn_time"], callback_data="menu_timenick")
    builder.button(text=LANG["btn_custom_nick"], callback_data="menu_custom_nick")
    builder.button(text=LANG["btn_back_menu"], callback_data="main_menu")
    builder.adjust(2, 1)
    await edit_or_send(user_id, "⚙️ **Настройки профиля и времени**\n\nВыберите подраздел конфигурации вашего никнейма:", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_timenick")
async def menu_timenick(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    cfg = await db.get_config(user_id)
    status_txt = LANG["status_on"] if cfg.get("time_nick_active") else LANG["status_off"]
    tz_name = cfg.get("timezone_name", "Ташкент, Екатеринбург / UTC+5")
    
    builder = InlineKeyboardBuilder()
    builder.button(text=LANG["btn_turn_off"] if cfg.get("time_nick_active") else LANG["btn_turn_on"], callback_data="toggle_timenick")
    builder.button(text=LANG["btn_tz_select"], callback_data="select_tz")
    builder.button(text=LANG["btn_back"], callback_data="menu_profile_settings")
    builder.adjust(1)
    
    text = f"🕒 **Вывод времени в имя профиля**\n\nТекущий статус: {status_txt}\nТекущий часовой пояс: **{tz_name}**"
    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "toggle_timenick")
async def toggle_timenick(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    cfg = await db.get_config(user_id)
    new_status = not cfg.get("time_nick_active")
    await db.update_config(user_id, {"time_nick_active": new_status})
    
    data = get_user_state(user_id)
    data["time_nick_active"] = new_status
    if new_status and (not data.get("time_nick_task") or data["time_nick_task"].done()):
        data["time_nick_task"] = asyncio.create_task(time_nickname_loop(user_id))
    elif not new_status:
        if data.get("time_nick_task"):
            data["time_nick_task"].cancel()
            data["time_nick_task"] = None
        if data["client"] and data["client"].is_connected:
            try:
                me = await data["client"].get_me()
                clean_name = strip_time_nick(me.first_name)
                await data["client"].update_profile(first_name=clean_name)
            except Exception as e:
                logger.error(f"Error resetting nickname for {user_id}: {e}")
        
    await menu_timenick(cb)

@dp.callback_query(F.data == "select_tz")
async def select_tz(cb: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    for i, (name, offset) in enumerate(ZONES):
        builder.button(text=name, callback_data=f"tz_prev_{i}")
    builder.button(text=LANG["btn_back"], callback_data="menu_timenick")
    builder.adjust(2, 2, 2, 2, 2, 1)
    await edit_or_send(cb.from_user.id, "🌐 **Выберите ваш часовой пояс:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("tz_prev_"))
async def tz_prev(cb: types.CallbackQuery):
    idx = int(cb.data.split("_")[2])
    name, offset = ZONES[idx]
    
    tz_now = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=offset)).strftime('%H:%M')
    text = f"🌐 **Выбранный пояс**: {name}\n⏰ **Время сейчас**: {tz_now}\n\nУстановить данный часовой пояс?"
    
    builder = InlineKeyboardBuilder()
    builder.button(text=LANG["btn_confirm"], callback_data=f"tz_save_{idx}")
    builder.button(text=LANG["btn_back"], callback_data="select_tz")
    builder.adjust(1)
    await edit_or_send(cb.from_user.id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("tz_save_"))
async def tz_save(cb: types.CallbackQuery):
    idx = int(cb.data.split("_")[2])
    name, offset = ZONES[idx]
    await db.update_config(cb.from_user.id, {"timezone_offset": offset, "timezone_name": name})
    await menu_timenick(cb)

@dp.callback_query(F.data == "menu_custom_nick")
async def menu_custom_nick(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    builder = InlineKeyboardBuilder()
    for s_name, idx in STYLES:
        builder.button(text=s_name, callback_data=f"preview_nick_{idx}")
    builder.button(text=LANG["btn_back"], callback_data="menu_profile_settings")
    builder.adjust(2, 2, 2, 2, 1)
    
    text = "✨ **Кастомизация шрифта времени в никнейме**\n\nВыберите вариант стиля ниже для предпросмотра:"
    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("preview_nick_"))
async def preview_nick(cb: types.CallbackQuery):
    style_idx = int(cb.data.split("_")[2])
    data = get_user_state(cb.from_user.id)
    
    base_name = "Имя"
    if data["client"] and data["client"].is_connected:
        try:
            me = await data["client"].get_me()
            base_name = strip_time_nick(me.first_name)
        except Exception:
            pass
        
    prev = apply_custom_nick(base_name, "10:30", style_idx)
    text = f"✨ **Предпросмотр профиля:**\n👤 `{prev}`\n\nПрименить выбранный стиль оформления?"
    
    builder = InlineKeyboardBuilder()
    builder.button(text=LANG["btn_confirm"], callback_data=f"save_nick_{style_idx}")
    builder.button(text=LANG["btn_back"], callback_data="menu_custom_nick")
    builder.adjust(1)
    await edit_or_send(cb.from_user.id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("save_nick_"))
async def save_nick(cb: types.CallbackQuery):
    style_idx = int(cb.data.split("_")[2])
    await db.update_config(cb.from_user.id, {"custom_nick_style": style_idx})
    
    data = get_user_state(cb.from_user.id)
    if data["client"] and data["client"].is_connected and data.get("time_nick_active"):
        try:
            me = await data["client"].get_me()
            cfg = await db.get_config(cb.from_user.id)
            offset = float(cfg.get("timezone_offset", 5.0))
            tz_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=offset)
            base_name = strip_time_nick(me.first_name)
            final_name = apply_custom_nick(base_name, tz_now.strftime('%H:%M'), style_idx)
            await data["client"].update_profile(first_name=final_name)
        except Exception as e:
            logger.error(f"Error updating nick style for {cb.from_user.id}: {e}")
        
    await menu_custom_nick(cb)

@dp.callback_query(F.data == "menu_activity")
async def menu_activity(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    act = await db.get_activity_data(user_id)
    
    text = "📊 **Статистика вашей активности (в минутах):**\n\n"
    if not act:
        text += "За последние 7 дней активность не зафиксирована."
    else:
        sorted_act = sorted(act.items(), key=lambda x: datetime.datetime.strptime(x[0], "%d.%m.%Y"), reverse=True)
        max_val = max(act.values()) if act.values() else 1
        for day, mins in sorted_act:
            bars = "🟩" * int((mins / max_val) * 8 or 1)
            text += f"📅 **{day}**: {mins} мин. {bars}\n"
            
    builder = InlineKeyboardBuilder()
    builder.button(text=LANG["btn_refresh"], callback_data="menu_activity")
    builder.button(text=LANG["btn_back_menu"], callback_data="main_menu")
    builder.adjust(1)
    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_autoresponder")
async def menu_autoresponder(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    cfg = await db.get_config(user_id)
    
    status_txt = LANG["status_on"] if cfg.get("autoresponder_active") else LANG["status_off"]
    greeting = cfg.get("autoresponder_greeting", LANG["msg_autoresp_default"])
    replied_count = len(cfg.get("replied_users", []))
    
    text = f"🤖 **Автоответчик**\n\nСтатус: {status_txt}\nУникальных ответов: **{replied_count}**\n\nТекущий текст приветствия:\n_{greeting}_"
    
    builder = InlineKeyboardBuilder()
    builder.button(text=LANG["btn_turn_off"] if cfg.get("autoresponder_active") else LANG["btn_turn_on"], callback_data="toggle_autoresponder")
    builder.button(text=LANG["btn_autoresp_setup"], callback_data="setup_autoresp_text")
    builder.button(text=LANG["btn_reset_autoresp_users"], callback_data="reset_autoresp_users")
    builder.button(text=LANG["btn_back_menu"], callback_data="main_menu")
    builder.adjust(1)
    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "reset_autoresp_users")
async def reset_autoresp_users(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    await db.update_config(user_id, {"replied_users": []})
    await cb.answer("✅ Список отвеченных пользователей сброшен!", show_alert=True)
    await menu_autoresponder(cb)

@dp.callback_query(F.data == "toggle_autoresponder")
async def toggle_autoresponder(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    cfg = await db.get_config(user_id)
    new_st = not cfg.get("autoresponder_active")
    
    updates = {"autoresponder_active": new_st}
    if new_st:
        updates["replied_users"] = []
    await db.update_config(user_id, updates)
    await menu_autoresponder(cb)

@dp.callback_query(F.data == "setup_autoresp_text")
async def setup_autoresp_text(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    get_user_state(user_id)["state"] = "WAITING_AUTORESP_TEXT"
    builder = InlineKeyboardBuilder().button(text=LANG["btn_back"], callback_data="menu_autoresponder")
    await edit_or_send(user_id, "📝 **Введите новый текст автоответчика:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_AUTORESP_TEXT")
async def process_autoresp_text(message: types.Message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    text = message.text.strip()
    
    await db.update_config(user_id, {"autoresponder_greeting": text})
    get_user_state(user_id)["state"] = "MENU"
    
    builder = InlineKeyboardBuilder().button(text=LANG["btn_confirm"], callback_data="menu_autoresponder")
    await edit_or_send(user_id, "✅ **Текст автоответчика успешно сохранен!**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "toggle_247")
async def toggle_247(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    cfg = await db.get_config(user_id)
    new_st = not cfg.get("status_24_7")
    await db.update_config(user_id, {"status_24_7": new_st})
    
    data = get_user_state(user_id)
    data["status_24_7"] = new_st
    if new_st and (not data.get("task_24_7") or data["task_24_7"].done()):
        data["task_24_7"] = asyncio.create_task(keep_online_loop(user_id))
    elif not new_st and data.get("task_24_7"):
        data["task_24_7"].cancel()
        data["task_24_7"] = None
        
    status_txt = LANG["status_on"] if new_st else LANG["status_off"]
    text = LANG["msg_247_text"].format(status_txt) if "msg_247_text" in LANG else f"⚡️ **Режим 24/7 Онлайна**\n\nСтатус: {status_txt}\n\nЮзербот поддерживает ваш аккаунт в сети 24 часа в сутки."
    builder = InlineKeyboardBuilder()
    builder.button(text=LANG["btn_turn_off"] if new_st else LANG["btn_turn_on"], callback_data="toggle_247")
    builder.button(text=LANG["btn_back_menu"], callback_data="main_menu")
    builder.adjust(1)
    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_delete")
async def menu_delete(cb: types.CallbackQuery):
    builder = InlineKeyboardBuilder()
    builder.button(text="5 ✉️", callback_data="del_5")
    builder.button(text="10 ✉️", callback_data="del_10")
    builder.button(text="25 ✉️", callback_data="del_25")
    builder.button(text="50 ✉️", callback_data="del_50")
    builder.button(text="100 ✉️", callback_data="del_100")
    builder.button(text=LANG["btn_back_menu"], callback_data="main_menu")
    builder.adjust(2, 3, 1)
    await edit_or_send(cb.from_user.id, LANG["msg_del_text"], reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("del_"))
async def process_del(cb: types.CallbackQuery):
    count = int(cb.data.split("_")[1])
    user_id = cb.from_user.id
    data = get_user_state(user_id)
    
    deleted_count = 0
    if data["client"] and data["client"].is_connected:
        try:
            ids = []
            async for m in data["client"].get_chat_history(user_id, limit=count + 5):
                if m.id != data.get("msg_id"):
                    ids.append(m.id)
                if len(ids) >= count:
                    break
            if ids:
                await data["client"].delete_messages(user_id, ids)
                deleted_count = len(ids)
        except Exception as e:
            logger.error(f"Error deleting messages for {user_id}: {e}")
        
    builder = InlineKeyboardBuilder().button(text=LANG["btn_back_menu"], callback_data="main_menu")
    await edit_or_send(user_id, f"✅ **Удалено сообщений:** {deleted_count}", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "menu_block_settings")
async def menu_block_settings(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    cfg = await db.get_config(user_id)
    has_pin = bool(cfg.get("menu_lock_code"))
    
    text = f"🔒 **Блокировка меню управления (PIN)**\n\nТекущий статус: {LANG['status_on'] if has_pin else LANG['status_off']}"
    builder = InlineKeyboardBuilder()
    if has_pin:
        builder.button(text=LANG["btn_lock_now"], callback_data="lock_now")
        builder.button(text="Снять блокировку 🔓", callback_data="disable_block")
    else:
        builder.button(text="Установить PIN-код 🔐", callback_data="setup_block_pin")
    builder.button(text=LANG["btn_back_menu"], callback_data="main_menu")
    builder.adjust(1)
    await edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "lock_now")
async def lock_now(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    cfg = await db.get_config(user_id)
    if cfg.get("menu_lock_code"):
        get_user_state(user_id)["state"] = "WAITING_UNLOCK_CODE"
        await edit_or_send(user_id, LANG["msg_unlock_req"], parse_mode="Markdown")
    else:
        await cb.answer("❌ PIN-код не установлен!", show_alert=True)

@dp.callback_query(F.data == "setup_block_pin")
async def setup_block_pin(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    get_user_state(user_id)["state"] = "WAITING_SET_PIN"
    builder = InlineKeyboardBuilder().button(text=LANG["btn_back"], callback_data="menu_block_settings")
    await edit_or_send(user_id, LANG["msg_block_setup"], reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_SET_PIN")
async def process_set_pin(message: types.Message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    pin = message.text.strip()
    
    if len(pin) == 4 and pin.isdigit():
        await db.update_config(user_id, {"is_menu_locked": True, "menu_lock_code": pin})
        get_user_state(user_id)["state"] = "MENU"
        builder = InlineKeyboardBuilder().button(text=LANG["btn_confirm"], callback_data="menu_block_settings")
        await edit_or_send(user_id, "✅ **PIN-код успешно сохранен и меню заблокировано!**", reply_markup=builder.as_markup(), parse_mode="Markdown")
    else:
        msg_err = await message.answer("❌ PIN-код должен состоять ровно из 4 цифр!")
        asyncio.create_task(safe_delete_message(msg_err, 3))

@dp.callback_query(F.data == "disable_block")
async def disable_block(cb: types.CallbackQuery):
    user_id = cb.from_user.id
    await db.update_config(user_id, {"is_menu_locked": False, "menu_lock_code": None})
    get_user_state(user_id)["state"] = "MENU"
    await menu_block_settings(cb)

@dp.message(lambda msg: get_user_state(msg.from_user.id)["state"] == "WAITING_UNLOCK_CODE")
async def process_unlock_pin(message: types.Message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    pin = message.text.strip()
    
    cfg = await db.get_config(user_id)
    if pin == cfg.get("menu_lock_code"):
        get_user_state(user_id)["state"] = "MENU"
        await db.update_config(user_id, {"last_interaction_time": time.time()})
        await show_main_menu(user_id)
    else:
        await edit_or_send(user_id, LANG["msg_unlock_req"], parse_mode="Markdown")
        msg_err = await message.answer("❌ Неверный PIN-код!")
        asyncio.create_task(safe_delete_message(msg_err, 3))

async def handle_ping(request):
    return web.Response(text="OK - Userbot Controller Running")

async def handle_health(request):
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    active_users = len(USER_DATA)
    payload = {
        "status": "healthy",
        "cpu_usage": f"{cpu}%",
        "ram_usage": f"{ram}%",
        "active_users": active_users,
        "timestamp": time.time()
    }
    return web.json_response(payload)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    app.router.add_get('/health', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web server started on port {port}")

async def main():
    logger.info("Initializing Userbot Controller Application...")
    asyncio.create_task(start_web_server())
    asyncio.create_task(background_lock_monitor())
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("Shutting down userbot clients...")
        for uid, data in USER_DATA.items():
            if data.get("client"):
                try:
                    await data["client"].disconnect()
                except Exception:
                    pass

if __name__ == "__main__":
    asyncio.run(main())
