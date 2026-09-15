import asyncio
import datetime
import logging
import os
import re
import time
from typing import Any

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from supabase import create_client, Client as SupabaseClient

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Optional health-check endpoint for Render/Web Service.
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

supabase: SupabaseClient | None = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logging.info("✅ Supabase подключен")
    except Exception as e:
        logging.error("❌ Ошибка подключения к Supabase: %s", e)

# Exactly the 12 zones used by the original project.
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

BOLD_DIGITS = {
    "0": "𝟬", "1": "𝟭", "2": "𝟮", "3": "𝟯", "4": "𝟰",
    "5": "𝟱", "6": "𝟲", "7": "𝟳", "8": "𝟴", "9": "𝟵",
}

# In-memory runtime state. Persistent settings are stored in Supabase when available.
USERS: dict[str, dict[str, Any]] = {}


def format_bold_time(value: str) -> str:
    return "".join(BOLD_DIGITS.get(ch, ch) for ch in value)


def user_key(user_id: int | str) -> str:
    return str(user_id)


def db_get(user_id: int | str) -> dict[str, Any]:
    if not supabase:
        return {}
    try:
        res = supabase.table("config").select("data").eq("id", user_key(user_id)).execute()
        if res.data:
            data = res.data[0].get("data")
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.error("Supabase read error: %s", e)
    return {}


def db_save(user_id: int | str, data: dict[str, Any]) -> None:
    if not supabase:
        return
    try:
        # The same `config` table shape as the original project can be reused.
        supabase.table("config").upsert({"id": user_key(user_id), "data": data}).execute()
    except Exception as e:
        logging.error("Supabase write error: %s", e)


async def async_db_get(user_id: int | str) -> dict[str, Any]:
    return await asyncio.to_thread(db_get, user_id)


async def async_db_save(user_id: int | str, data: dict[str, Any]) -> None:
    await asyncio.to_thread(db_save, user_id, data)


def ensure_user(user_id: int, *, from_user: types.User | None = None) -> dict[str, Any]:
    key = user_key(user_id)
    if key not in USERS:
        cfg = db_get(user_id)
        if not cfg:
            cfg = {
                "logged_in": False,
                "business_connection_id": None,
                "time_nick_active": False,
                "timezone_offset": 5,
                "profile_base_first_name": (from_user.first_name if from_user else "User") or "User",
                "profile_base_last_name": (from_user.last_name if from_user else "") or "",
                "user_id": user_id,
                "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        USERS[key] = cfg
    return USERS[key]


def get_preview(cfg: dict[str, Any]) -> str:
    first = (cfg.get("profile_base_first_name") or "User").strip() or "User"
    last = (cfg.get("profile_base_last_name") or "").strip()

    if cfg.get("time_nick_active", False):
        offset = int(cfg.get("timezone_offset", 5))
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_local = now_utc + datetime.timedelta(hours=offset)
        marker = f"[{format_bold_time(now_local.strftime('%H:%M'))}]"
        if last:
            last = f"{last} {marker}"
        else:
            first = f"{first} {marker}"

    return f"{first}\n{last}" if last else first


def menu_markup() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Время в профиль ⏰", callback_data="menu_time")
    builder.adjust(1)
    return builder.as_markup()


def time_menu_markup(cfg: dict[str, Any]) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    is_active = bool(cfg.get("time_nick_active", False))
    builder.button(
        text="Выключить 🔴" if is_active else "Включить 🟢",
        callback_data="toggle_time",
    )
    builder.button(text="Выбрать часовой пояс 🌐", callback_data="tz_select")
    builder.button(text="Назад 🏠", callback_data="main_menu")
    builder.adjust(2, 1)
    return builder.as_markup()


async def send_or_edit(user_id: int, text: str, markup=None) -> None:
    state = ensure_user(user_id)
    message_id = state.get("message_id")
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
            )
            return
        except Exception:
            pass

    msg = await bot.send_message(user_id, text, reply_markup=markup)
    state["message_id"] = msg.message_id
    asyncio.create_task(async_db_save(user_id, state))


async def apply_profile_time(user_id: int, cfg: dict[str, Any]) -> bool:
    """Change only the first/last name of the managed Business account.

    Requires the Business bot right `can_change_name` (Telegram names it
    `edit_name` internally). No message rights are needed here.
    """
    connection_id = cfg.get("business_connection_id")
    if not cfg.get("logged_in") or not cfg.get("time_nick_active") or not connection_id:
        return False

    first = (cfg.get("profile_base_first_name") or "User").strip() or "User"
    last = (cfg.get("profile_base_last_name") or "").strip()
    offset = int(cfg.get("timezone_offset", 5))

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    now_local = now_utc + datetime.timedelta(hours=offset)
    marker = f"[{format_bold_time(now_local.strftime('%H:%M'))}]"

    if last:
        new_first = first
        new_last = f"{last} {marker}"
    else:
        new_first = f"{first} {marker}"
        new_last = ""

    try:
        await bot.set_business_account_name(
            business_connection_id=connection_id,
            first_name=new_first,
            last_name=new_last,
        )
        cfg["last_profile_key"] = [new_first, new_last]
        return True
    except Exception as e:
        logging.warning("Не удалось изменить Business-профиль user=%s: %s", user_id, e)
        return False


async def restore_profile_name(user_id: int, cfg: dict[str, Any]) -> None:
    connection_id = cfg.get("business_connection_id")
    if not connection_id:
        return
    try:
        await bot.set_business_account_name(
            business_connection_id=connection_id,
            first_name=((cfg.get("profile_base_first_name") or "User").strip() or "User"),
            last_name=((cfg.get("profile_base_last_name") or "").strip()),
        )
    except Exception as e:
        logging.warning("Не удалось восстановить имя Business-профиля user=%s: %s", user_id, e)


async def clock_loop() -> None:
    # Update right after every minute boundary, preserving the original minute-based behavior.
    while True:
        try:
            now = time.time()
            delay = 60.0 - (now % 60.0)
            await asyncio.sleep(max(0.05, delay))

            for key, cfg in list(USERS.items()):
                if not cfg.get("logged_in") or not cfg.get("time_nick_active"):
                    continue
                await apply_profile_time(int(key), cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error("Ошибка глобального цикла времени: %s", e)


@dp.message(CommandStart())
async def cmd_start(message: types.Message) -> None:
    user_id = message.from_user.id
    cfg = ensure_user(user_id, from_user=message.from_user)
    connected = bool(cfg.get("logged_in")) and bool(cfg.get("business_connection_id"))

    if connected:
        status = "Включено 🟢" if cfg.get("time_nick_active") else "Выключено 🔴"
        offset = int(cfg.get("timezone_offset", 5))
        preview = get_preview(cfg)
        text = (
            "🕐 **Qwitty Time**\n\n"
            f"Время в профиль: {status}\n"
            f"Часовой пояс: UTC{offset:+d}\n"
            f"Профиль:\n{preview}"
        )
        await send_or_edit(user_id, text, time_menu_markup(cfg))
        return

    text = (
        "🕐 **Qwitty Time**\n\n"
        "Показывает текущее время в вашем профиле Telegram.\n\n"
        "Подключите Qwitty Time к вашему Telegram Business-аккаунту.\n"
        "Боту достаточно права на изменение имени профиля — права на чтение, отправку или удаление сообщений не требуются."
    )
    await send_or_edit(user_id, text, menu_markup())


@dp.callback_query(F.data == "main_menu")
async def main_menu(callback: types.CallbackQuery) -> None:
    await cmd_start(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "menu_time")
async def menu_time(callback: types.CallbackQuery) -> None:
    user_id = callback.from_user.id
    cfg = ensure_user(user_id, from_user=callback.from_user)
    if not cfg.get("logged_in"):
        await send_or_edit(
            user_id,
            "Сначала подключите Qwitty Time к Telegram Business.",
            menu_markup(),
        )
        await callback.answer()
        return

    status = "Включено 🟢" if cfg.get("time_nick_active") else "Выключено 🔴"
    offset = int(cfg.get("timezone_offset", 5))
    preview = get_preview(cfg)
    text = (
        "🕐 **Время в профиль**\n\n"
        f"Статус: {status}\n"
        f"Часовой пояс: UTC{offset:+d}\n\n"
        f"Профиль:\n{preview}"
    )
    await send_or_edit(user_id, text, time_menu_markup(cfg))
    await callback.answer()


@dp.callback_query(F.data == "toggle_time")
async def toggle_time(callback: types.CallbackQuery) -> None:
    user_id = callback.from_user.id
    cfg = ensure_user(user_id, from_user=callback.from_user)
    if not cfg.get("logged_in") or not cfg.get("business_connection_id"):
        await callback.answer("Сначала подключите Qwitty Time в Telegram Business.", show_alert=True)
        return

    cfg["time_nick_active"] = not bool(cfg.get("time_nick_active"))
    USERS[user_key(user_id)] = cfg
    await async_db_save(user_id, cfg)

    if cfg["time_nick_active"]:
        await apply_profile_time(user_id, cfg)
    else:
        await restore_profile_name(user_id, cfg)

    await menu_time(callback)


@dp.callback_query(F.data == "tz_select")
async def tz_select(callback: types.CallbackQuery) -> None:
    builder = InlineKeyboardBuilder()
    for value, name in TIMEZONE_NAMES.items():
        builder.button(text=name, callback_data=f"set_tz_{value}")
    builder.button(text="Назад ⬅️", callback_data="menu_time")
    builder.adjust(2)
    await send_or_edit(callback.from_user.id, "Выберите ваш часовой пояс 🌐", builder.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("set_tz_"))
async def set_timezone(callback: types.CallbackQuery) -> None:
    user_id = callback.from_user.id
    value = int(callback.data.split("_")[-1])
    cfg = ensure_user(user_id, from_user=callback.from_user)
    cfg["timezone_offset"] = value
    USERS[user_key(user_id)] = cfg
    await async_db_save(user_id, cfg)

    if cfg.get("logged_in") and cfg.get("time_nick_active"):
        await apply_profile_time(user_id, cfg)

    await menu_time(callback)


@dp.business_connection()
async def on_business_connection(connection: types.BusinessConnection) -> None:
    # BusinessConnection is emitted when the user connects, edits, or disconnects this bot.
    user = connection.user
    user_id = user.id
    cfg = ensure_user(user_id, from_user=user)

    if not connection.is_enabled:
        cfg["logged_in"] = False
        cfg["business_connection_id"] = None
        cfg["time_nick_active"] = False
        USERS[user_key(user_id)] = cfg
        await async_db_save(user_id, cfg)
        logging.info("🔌 Business connection disabled for %s", user_id)
        return

    rights = connection.rights
    # IMPORTANT: only edit_name/can_change_name is needed.
    can_change_name = bool(getattr(rights, "can_change_name", False)) if rights else False
    if not can_change_name:
        logging.warning("Business connection from %s has no can_change_name right", user_id)
        cfg["logged_in"] = False
        cfg["business_connection_id"] = None
        cfg["time_nick_active"] = False
        USERS[user_key(user_id)] = cfg
        await async_db_save(user_id, cfg)
        try:
            await bot.send_message(
                user_id,
                "⚠️ Для Qwitty Time необходимо только право **изменения имени профиля**.\n"
                "Подключение без этого права не будет использовано.",
            )
        except Exception:
            pass
        return

    cfg["logged_in"] = True
    cfg["business_connection_id"] = connection.id
    cfg["user_id"] = user_id
    cfg["profile_base_first_name"] = (user.first_name or cfg.get("profile_base_first_name") or "User").strip() or "User"
    cfg["profile_base_last_name"] = (user.last_name or cfg.get("profile_base_last_name") or "").strip()
    USERS[user_key(user_id)] = cfg
    await async_db_save(user_id, cfg)

    logging.info("✅ Business connection enabled for %s (name-only right)", user_id)
    await send_or_edit(
        user_id,
        "✅ **Qwitty Time подключён!**\n\n"
        "Бот использует только изменение имени профиля для вывода времени.\n"
        "Управление сообщениями не требуется.",
        time_menu_markup(cfg),
    )


async def restore_saved_users() -> None:
    if not supabase:
        return
    try:
        rows = await asyncio.to_thread(lambda: supabase.table("config").select("id, data").execute().data or [])
    except Exception as e:
        logging.error("Ошибка загрузки конфигураций: %s", e)
        return

    loaded = 0
    for row in rows:
        uid = str(row.get("id", "")).strip()
        cfg = row.get("data") or {}
        if not uid or not isinstance(cfg, dict):
            continue
        # Only restore Business-mode users; legacy MTProto fields are ignored.
        if cfg.get("business_connection_id") and cfg.get("logged_in"):
            cfg.setdefault("time_nick_active", False)
            cfg.setdefault("timezone_offset", 5)
            USERS[uid] = cfg
            loaded += 1

    logging.info("🔄 Загружено Business-подключений: %s", loaded)


async def health_handler(_: web.Request) -> web.Response:
    return web.Response(text="Qwitty Time is running")


async def run_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("🌐 Health server started on port %s", PORT)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    await restore_saved_users()
    await run_health_server()
    asyncio.create_task(clock_loop())

    logging.info("🚀 Qwitty Time Business запускается")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
