import asyncio
import datetime
import html
import logging
import os
import re

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import CopyTextButton, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_COPY_USERNAME = "@Qwitty_Time_Bot"
TELEGRAM_SETTINGS_URL = "tg://settings/edit"
DEFAULT_TIMEZONE = 5
START_DELETE_DELAY = 1.0

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

BOLD_DIGITS = str.maketrans("0123456789", "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵")

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# In-memory only: settings live only while this process is running.
USERS: dict[int, dict] = {}


def user_data(user_id: int) -> dict:
    if user_id not in USERS:
        USERS[user_id] = {
            "connection_id": None,
            "connection_enabled": False,
            "can_change_name": False,
            "consent_accepted": False,
            "time_active": False,
            "timezone": DEFAULT_TIMEZONE,
            "base_first_name": "User",
            "base_last_name": "",
            "ui_message_id": None,
            "last_profile_key": None,
            "time_task": None,
        }
    return USERS[user_id]


def bold_time(value: str) -> str:
    return value.translate(BOLD_DIGITS)


def strip_time_marker(value: str | None) -> str:
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", (value or "").strip()).strip()


def local_now(offset: int) -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=offset)


def current_time_text(offset: int) -> str:
    return bold_time(local_now(offset).strftime("%H:%M"))


def profile_preview(data: dict) -> str:
    first = strip_time_marker(data.get("base_first_name")) or "User"
    last = strip_time_marker(data.get("base_last_name"))
    marker = f"[{current_time_text(int(data.get('timezone', DEFAULT_TIMEZONE)))}]"
    if last:
        return f"{html.escape(first)}\n{html.escape(last)} {marker}"
    return f"{html.escape(first)} {marker}"


def is_ready(data: dict) -> bool:
    return bool(
        data.get("connection_id")
        and data.get("connection_enabled")
        and data.get("can_change_name")
    )


def welcome_text(user: types.User) -> str:
    name = html.escape(user.first_name or "Пользователь")
    return (
        "🛡 <b>Прежде чем начать</b>\n\n"
        f"𝗤ᴡɪᴛᴛʏ 𝗧ɪᴍᴇ создаст время в ваш профиль, например:\n"
        f"<b>{name} [𝟭𝟲:𝟯𝟬]</b>\n\n"
        "Коротко о правилах:\n"
        "• бот обновляет имя пользователя раз в минуту\n"
        "• содержимое не сохраняется в базе\n"
        "• отключить функцию можно в любой момент"
    )


def welcome_markup() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Я принимаю✅", callback_data="consent")
    return builder.as_markup()


def setup_text() -> str:
    return (
        "📲 <b>Осталось одно действие</b>\n\n"
        "Нажми кнопку ниже, откроются настройки Telegram. Там:\n\n"
        "1️⃣ пункт <b>Автоматизация чатов</b>\n"
        "2️⃣ вставьте <code>@Qwitty_Time_Bot</code>\n"
        "3️⃣ нажмите <b>Только выбранные чаты</b> и выберите Qwitty Time из списка\n\n"
        "Username при нажатии будет скопирован — если не вставился, жмите «Скопировать»."
    )


def setup_markup() -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📲Открыть настройки Telegram",
            url=TELEGRAM_SETTINGS_URL,
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📋Скопировать @Qwitty_Time_Bot",
            copy_text=CopyTextButton(text=BOT_COPY_USERNAME),
        )
    )
    return builder.as_markup()


def requirements_text() -> str:
    return (
        "⚠️ <b>Почти готово — не хватает несколько разрешении</b>\n\n"
        "🔴 Telegram не дал ему права <b>управление с профилем</b>. Без него бот не сможет добавлять время.\n"
        "<b>Где включается:</b>\n"
        "1️⃣ Настройки Telegram → <b>Автоматизация чатов</b>\n"
        "2️⃣ Пролистайте до раздела <b>«Разрешение для бота»</b>\n"
        "3️⃣ В группе <b>«Управление профилем»</b> включите «Изменение имени»\n\n"
        "🔴 Вы не выбрали 𝗤ᴡɪᴛᴛʏ 𝗧ɪᴍᴇ через «Только выбранные чаты»\n"
        "<b>Где включается:</b>\n"
        "1️⃣ Настройки Telegram → <b>Автоматизация чатов</b>\n"
        "2️⃣ Пролистайте до раздела <b>«Чаты, доступные боту»</b>\n"
        "3️⃣ В группе <b>«Только выбранные чаты»</b> выберите «𝗤ᴡɪᴛᴛʏ 𝗧ɪᴍᴇ»\n\n"
        "Как все сделаете — бот заработает, ничего писать мне не нужно.\n"
        "Если не сработало, нажмите /start ещё раз."
    )


def time_menu_text(user_id: int) -> str:
    data = user_data(user_id)
    status = "Включено🟢" if data["time_active"] else "Выключено🔴"
    offset = int(data["timezone"])
    preview = profile_preview(data)
    return (
        "<b>Время в профиль⏰</b>\n\n"
        f"Статус: {status}\n"
        f"Часовой пояс: <b>{html.escape(TIMEZONE_NAMES[offset])}</b>\n\n"
        "<b>Профиль:</b>\n"
        f"{preview}"
    )


def time_menu_markup(user_id: int) -> types.InlineKeyboardMarkup:
    data = user_data(user_id)
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Выключить🔴" if data["time_active"] else "Включить🟢",
        callback_data="toggle_time",
    )
    builder.button(text="Часовой пояс🌐", callback_data="timezone")
    builder.adjust(1)
    return builder.as_markup()


def timezone_markup(user_id: int) -> types.InlineKeyboardMarkup:
    current = int(user_data(user_id)["timezone"])
    builder = InlineKeyboardBuilder()
    for offset, name in TIMEZONE_NAMES.items():
        prefix = "✅ " if offset == current else ""
        builder.button(text=f"{prefix}{name}", callback_data=f"tz:{offset}")
    builder.button(text="⬅️ Назад", callback_data="time_menu")
    builder.adjust(1)
    return builder.as_markup()


def requirements_or_ready_text(user_id: int) -> tuple[str, types.InlineKeyboardMarkup | None]:
    data = user_data(user_id)
    if is_ready(data):
        return time_menu_text(user_id), time_menu_markup(user_id)
    return requirements_text(), None


async def delete_later(message: types.Message) -> None:
    await asyncio.sleep(START_DELETE_DELAY)
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass


async def render(user_id: int, text: str, markup=None) -> None:
    data = user_data(user_id)
    message_id = data.get("ui_message_id")

    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
        except Exception:
            pass

    try:
        sent = await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
        data["ui_message_id"] = sent.message_id
    except Exception as exc:
        logging.error("Не удалось отправить UI %s: %s", user_id, exc)


async def replace_ui(user_id: int) -> None:
    data = user_data(user_id)
    old_id = data.get("ui_message_id")
    if old_id:
        try:
            await bot.delete_message(user_id, old_id)
        except Exception:
            pass
        data["ui_message_id"] = None


async def set_business_profile_name(user_id: int, first_name: str, last_name: str = "") -> bool:
    data = user_data(user_id)
    connection_id = data.get("connection_id")
    if not connection_id:
        return False
    try:
        await bot.set_business_account_name(
            business_connection_id=connection_id,
            first_name=(first_name or "User")[:64],
            last_name=(last_name or "")[:64],
        )
        return True
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logging.warning("Не удалось изменить имя %s: %s", user_id, exc)
        return False
    except Exception as exc:
        logging.error("Ошибка set_business_account_name %s: %s", user_id, exc)
        return False


async def update_profile_time(user_id: int, force: bool = False) -> None:
    data = user_data(user_id)
    if not is_ready(data) or not data.get("time_active"):
        return

    offset = int(data.get("timezone", DEFAULT_TIMEZONE))
    now = local_now(offset)
    time_value = now.strftime("%H:%M")
    marker = f"[{bold_time(time_value)}]"
    first = strip_time_marker(data.get("base_first_name")) or "User"
    last = strip_time_marker(data.get("base_last_name"))
    new_first = first
    new_last = f"{last} {marker}".strip() if last else ""
    if not last:
        new_first = f"{first} {marker}"

    profile_key = (new_first, new_last)
    if not force and data.get("last_profile_key") == profile_key:
        return

    if await set_business_profile_name(user_id, new_first, new_last):
        data["last_profile_key"] = profile_key


async def time_loop(user_id: int) -> None:
    try:
        while True:
            data = user_data(user_id)
            if not is_ready(data) or not data.get("time_active"):
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            delay = 60 - now.second - now.microsecond / 1_000_000
            await asyncio.sleep(max(0.05, delay))
            await update_profile_time(user_id, force=False)
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logging.error("Ошибка time_loop %s: %s", user_id, exc)


def start_time_loop(user_id: int) -> None:
    data = user_data(user_id)
    task = data.get("time_task")
    if task and not task.done():
        return
    data["time_task"] = asyncio.create_task(time_loop(user_id))


def stop_time_loop(user_id: int) -> None:
    data = user_data(user_id)
    task = data.get("time_task")
    if task and not task.done():
        task.cancel()
    data["time_task"] = None


async def show_current_state(user_id: int) -> None:
    data = user_data(user_id)
    if is_ready(data):
        await render(user_id, time_menu_text(user_id), time_menu_markup(user_id))
    else:
        await render(user_id, requirements_text(), None)


@dp.message(CommandStart())
async def command_start(message: types.Message):
    user_id = message.from_user.id
    asyncio.create_task(delete_later(message))

    data = user_data(user_id)
    await replace_ui(user_id)

    if is_ready(data):
        await render(user_id, time_menu_text(user_id), time_menu_markup(user_id))
    elif data.get("connection_enabled"):
        await render(user_id, requirements_text(), None)
    else:
        await render(user_id, welcome_text(message.from_user), welcome_markup())


@dp.callback_query(F.data == "consent")
async def consent(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = user_data(user_id)
    data["consent_accepted"] = True

    if data.get("connection_enabled"):
        await show_current_state(user_id)
    else:
        await render(user_id, setup_text(), setup_markup())

    await callback.answer()


@dp.business_connection()
async def business_connection(connection: types.BusinessConnection):
    user_id = connection.user.id
    data = user_data(user_id)

    if not connection.is_enabled:
        stop_time_loop(user_id)
        data.update({
            "connection_id": None,
            "connection_enabled": False,
            "can_change_name": False,
            "consent_accepted": False,
            "time_active": False,
            "last_profile_key": None,
        })
        await render(
            user_id,
            "<i>⚠️</i> <b>Бот отключён в настройках Telegram.</b>\n\n"
            "Время в профиль больше не обновится.\n\n"
            "Чтобы снова включить: <b>Настройки → Автоматизация чатов</b>.",
            None,
        )
        return

    rights = connection.rights
    data["connection_id"] = connection.id
    data["connection_enabled"] = True
    data["can_change_name"] = bool(getattr(rights, "can_change_name", False)) if rights else False
    data["base_first_name"] = strip_time_marker(connection.user.first_name) or "User"
    data["base_last_name"] = strip_time_marker(connection.user.last_name)
    data["last_profile_key"] = None

    # User requested both red blocks to disappear together as soon as
    # the profile-name permission is granted. The second block is guidance only;
    # Telegram Bot API does not expose the selected-chat list directly.
    if is_ready(data):
        await render(user_id, time_menu_text(user_id), time_menu_markup(user_id))
    elif data.get("consent_accepted"):
        await render(user_id, requirements_text(), None)
    else:
        await render(user_id, welcome_text(connection.user), welcome_markup())


@dp.callback_query(F.data == "toggle_time")
async def toggle_time(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = user_data(user_id)

    if not is_ready(data):
        await show_current_state(user_id)
        await callback.answer("Сначала подключите бота и выдайте разрешение", show_alert=False)
        return

    data["time_active"] = not data["time_active"]
    data["last_profile_key"] = None

    if data["time_active"]:
        start_time_loop(user_id)
        await update_profile_time(user_id, force=True)
    else:
        stop_time_loop(user_id)
        first = strip_time_marker(data.get("base_first_name")) or "User"
        last = strip_time_marker(data.get("base_last_name"))
        await set_business_profile_name(user_id, first, last)

    await render(user_id, time_menu_text(user_id), time_menu_markup(user_id))
    await callback.answer()


@dp.callback_query(F.data == "timezone")
async def timezone(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await render(user_id, "🌍 <b>Выберите часовой пояс</b>", timezone_markup(user_id))
    await callback.answer()


@dp.callback_query(F.data.startswith("tz:"))
async def set_timezone(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        offset = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer()
        return

    if offset not in TIMEZONE_NAMES:
        await callback.answer()
        return

    data = user_data(user_id)
    data["timezone"] = offset
    data["last_profile_key"] = None

    if data.get("time_active") and is_ready(data):
        await update_profile_time(user_id, force=True)

    await render(user_id, time_menu_text(user_id), time_menu_markup(user_id))
    await callback.answer("Часовой пояс изменён")


@dp.callback_query(F.data == "time_menu")
async def back_time_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    data = user_data(user_id)
    if is_ready(data):
        await render(user_id, time_menu_text(user_id), time_menu_markup(user_id))
    else:
        await show_current_state(user_id)
    await callback.answer()


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    me = await bot.get_me()
    logging.info("🤖 Qwitty Time запущен: @%s", me.username or "Qwitty_Time_Bot")

    health_runner = await start_health_server()
    try:
        await dp.start_polling(bot)
    finally:
        for user_id in list(USERS):
            stop_time_loop(user_id)
        await health_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
