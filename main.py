import os
import re
import time
import asyncio
from typing import Optional

from dotenv import load_dotenv
from aiohttp import web
from supabase import AsyncClient, acreate_client

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message,
    ChatPermissions,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatMemberUpdated,
)
from aiogram.filters import CommandStart, ChatMemberUpdatedFilter, IS_NOT_MEMBER, MEMBER
from aiogram.enums import ChatMemberStatus
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State


# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
WEB_PORT = int(os.getenv("PORT", "10000"))

BOT_USERNAME = "QwertyGuard_Bot"
ADD_URL = (
    f"https://t.me/{BOT_USERNAME}?startgroup=true&admin="
    "restrict_members+delete_messages+ban_users+invite_users+pin_messages"
)

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")
if not SUPABASE_URL:
    raise RuntimeError("Не задан SUPABASE_URL")
if not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Не задан SUPABASE_SERVICE_ROLE_KEY")


# ==========================================
# ГЛОБАЛЬНЫЕ ОБЪЕКТЫ
# ==========================================
supabase: Optional[AsyncClient] = None
spam_cache = {}
invite_warnings = {}

UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)

MOD_RIGHT_COLUMNS = {
    "can_ban": "can_ban",
    "can_mute": "can_mute",
    "can_kick": "can_kick",
}


class BotConfig(StatesGroup):
    waiting_for_invite_count = State()


# ==========================================
# SUPABASE
# ==========================================
async def init_db():
    """Проверяем, что Supabase доступен и таблица groups существует."""
    global supabase
    supabase = await acreate_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

    result = await supabase.table("groups").select("group_id").limit(1).execute()
    if result is None:
        raise RuntimeError("Supabase не вернул ответ")


async def add_group(group_id: int, title: str, owner_id: int):
    await supabase.table("groups").upsert(
        {
            "group_id": group_id,
            "title": title,
            "owner_id": owner_id,
        },
        on_conflict="group_id",
    ).execute()


async def user_owns_group(user_id: int, group_id: int) -> bool:
    result = (
        await supabase.table("groups")
        .select("group_id")
        .eq("group_id", group_id)
        .eq("owner_id", user_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


async def get_user_groups(user_id: int):
    result = (
        await supabase.table("groups")
        .select("group_id, title")
        .eq("owner_id", user_id)
        .order("title")
        .execute()
    )
    return [(row["group_id"], row["title"]) for row in (result.data or [])]


async def get_group_settings(group_id: int):
    result = (
        await supabase.table("groups")
        .select("req_invites, spam_protect")
        .eq("group_id", group_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    row = result.data[0]
    return int(row.get("req_invites") or 0), bool(row.get("spam_protect"))


async def update_req_invites(group_id: int, count: int):
    count = max(0, int(count))
    await (
        supabase.table("groups")
        .update({"req_invites": count})
        .eq("group_id", group_id)
        .execute()
    )


async def toggle_spam(group_id: int):
    settings = await get_group_settings(group_id)
    current = bool(settings[1]) if settings else False
    await (
        supabase.table("groups")
        .update({"spam_protect": not current})
        .eq("group_id", group_id)
        .execute()
    )


async def get_user_invites(user_id: int, group_id: int):
    result = (
        await supabase.table("users")
        .select("invites_count, is_allowed")
        .eq("user_id", user_id)
        .eq("group_id", group_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return 0, False

    row = result.data[0]
    return int(row.get("invites_count") or 0), bool(row.get("is_allowed"))


async def add_user_invites(user_id: int, group_id: int, count: int = 1):
    """Сохраняем счётчик приглашений через upsert."""
    current, is_allowed = await get_user_invites(user_id, group_id)
    await supabase.table("users").upsert(
        {
            "user_id": user_id,
            "group_id": group_id,
            "invites_count": current + count,
            "is_allowed": is_allowed,
        },
        on_conflict="user_id,group_id",
    ).execute()


async def allow_user(user_id: int, group_id: int):
    await supabase.table("users").upsert(
        {
            "user_id": user_id,
            "group_id": group_id,
            "is_allowed": True,
        },
        on_conflict="user_id,group_id",
    ).execute()


async def track_user(group_id: int, user_id: int, first_name: str, username: Optional[str]):
    await supabase.table("group_users").upsert(
        {
            "group_id": group_id,
            "user_id": user_id,
            "first_name": first_name,
            "username": username,
        },
        on_conflict="group_id,user_id",
    ).execute()


async def get_user_by_username(group_id: int, username: str):
    username = username.replace("@", "")
    result = (
        await supabase.table("group_users")
        .select("user_id, first_name")
        .eq("group_id", group_id)
        .eq("username", username)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    row = result.data[0]
    return row["user_id"], row["first_name"]


async def get_available_users(group_id: int):
    # Функция сохранена для будущей панели.
    moderators = await get_moderators(group_id)
    moderator_ids = {row[0] for row in moderators}

    result = (
        await supabase.table("group_users")
        .select("user_id, first_name, username")
        .eq("group_id", group_id)
        .order("user_id", desc=True)
        .limit(50)
        .execute()
    )
    return [
        (row["user_id"], row["first_name"], row.get("username"))
        for row in (result.data or [])
        if row["user_id"] not in moderator_ids
    ][:30]


async def get_moderators(group_id: int):
    result = (
        await supabase.table("moderators")
        .select("user_id, can_ban, can_mute, can_kick")
        .eq("group_id", group_id)
        .execute()
    )
    if not result.data:
        return []

    rows = {row["user_id"]: row for row in result.data}

    ids = list(rows.keys())
    users_by_id = {}
    if ids:
        user_result = (
            await supabase.table("group_users")
            .select("user_id, first_name, username")
            .eq("group_id", group_id)
            .in_("user_id", ids)
            .execute()
        )
        users_by_id = {row["user_id"]: row for row in (user_result.data or [])}

    output = []
    for user_id, row in rows.items():
        user = users_by_id.get(user_id, {})
        output.append(
            (
                user_id,
                user.get("first_name", f"ID {user_id}"),
                user.get("username"),
                bool(row.get("can_ban")),
                bool(row.get("can_mute")),
                bool(row.get("can_kick")),
            )
        )
    return output


async def get_moderator_rights(group_id: int, user_id: int):
    result = (
        await supabase.table("moderators")
        .select("can_ban, can_mute, can_kick")
        .eq("group_id", group_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None

    row = result.data[0]
    return bool(row.get("can_ban")), bool(row.get("can_mute")), bool(row.get("can_kick"))


async def add_moderator(group_id: int, user_id: int):
    await supabase.table("moderators").upsert(
        {
            "group_id": group_id,
            "user_id": user_id,
        },
        on_conflict="group_id,user_id",
        ignore_duplicates=True,
    ).execute()


async def remove_moderator(group_id: int, user_id: int):
    await (
        supabase.table("moderators")
        .delete()
        .eq("group_id", group_id)
        .eq("user_id", user_id)
        .execute()
    )


async def toggle_mod_right(group_id: int, user_id: int, right_type: str):
    column = MOD_RIGHT_COLUMNS.get(right_type)
    if not column:
        raise ValueError("Неизвестное право модератора")

    result = (
        await supabase.table("moderators")
        .select(column)
        .eq("group_id", group_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return

    current = bool(result.data[0].get(column))
    await (
        supabase.table("moderators")
        .update({column: not current})
        .eq("group_id", group_id)
        .eq("user_id", user_id)
        .execute()
    )


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def plural_friends(n: int) -> str:
    if 1 <= n % 10 <= 4 and not (11 <= n % 100 <= 14):
        return "друга"
    return "друзей"


def format_time_text(seconds: int) -> str:
    if seconds <= 0:
        return "Навсегда"
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч"
    return f"{seconds // 86400} дн"


def parse_time(text: str):
    text = text.lower()
    if "навсегда" in text:
        return 0

    match = re.search(r"(\d+)\s*(час|ч|мин|м|день|дн|сек|с)?", text)
    if not match:
        return 0

    val = int(match.group(1))
    unit = match.group(2) or "м"

    if "ч" in unit:
        return val * 3600
    if "д" in unit:
        return val * 86400
    if "с" in unit:
        return val
    return val * 60


def parse_mod_command(text: str):
    lines = text.split("\n", 1)
    first_line = lines[0].strip()
    reason = lines[1].strip() if len(lines) > 1 else "Не указана"

    match = re.match(r"(?i)^(бан|мут|кик|разбан|размут)\b", first_line)
    if not match:
        return None

    cmd = match.group(1).lower()
    rest = first_line[match.end():].strip()

    target_username = None
    target_id = None
    time_str = rest

    user_match = re.match(r"(?:@([a-zA-Z0-9_]+)|(\d+))\b", rest)
    if user_match:
        target_username = user_match.group(1)
        target_id = int(user_match.group(2)) if user_match.group(2) else None
        time_str = rest[user_match.end():].strip()

    time_sec = parse_time(time_str) if time_str else 0
    return cmd, target_username, target_id, time_sec, reason


async def delete_msg_after(bot: Bot, chat_id: int, msg_id: int, delay: int = 15):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def check_mod_rights(bot: Bot, chat_id: int, user_id: int, action: str) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
            return True
    except Exception:
        pass

    rights = await get_moderator_rights(chat_id, user_id)
    if rights:
        c_ban, c_mute, c_kick = rights
        if action in ["бан", "разбан"]:
            return c_ban
        if action in ["мут", "размут"]:
            return c_mute
        if action == "кик":
            return c_kick

    return False


async def unban_unmute_task(
    bot: Bot,
    chat_id: int,
    user_id: int,
    first_name: str,
    delay: int,
    action: str,
):
    await asyncio.sleep(delay)
    try:
        if action == "бана":
            # Telegram сам завершает временный ban по until_date.
            text_msg = (
                f"[{first_name}](tg://user?id={user_id}) "
                "Время бана окончено, доступ снова открыт."
            )
        elif action == "мута":
            # Telegram также сам снимает временный restrict по until_date.
            text_msg = (
                f"[{first_name}](tg://user?id={user_id}) "
                "Время мута окончено, вы снова можете писать."
            )
        else:
            return

        msg = await bot.send_message(chat_id, text_msg, parse_mode="Markdown")
        asyncio.create_task(delete_msg_after(bot, chat_id, msg.message_id, 15))
    except Exception as e:
        print(f"Ошибка уведомления о снятии наказания: {e}")


async def captcha_timer(bot: Bot, chat_id: int, user_id: int, msg_id: int):
    await asyncio.sleep(12)
    u_cache = spam_cache.get(chat_id, {}).get(user_id)
    if u_cache and u_cache.get("pending"):
        try:
            await bot.ban_chat_member(chat_id, user_id)
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        spam_cache[chat_id].pop(user_id, None)


async def get_main_menu(user_id: int):
    groups = await get_user_groups(user_id)
    kb = []

    if groups:
        kb = [
            [InlineKeyboardButton(text=f"👥 {g_title}", callback_data=f"manage_{g_id}")]
            for g_id, g_title in groups
        ]
        kb.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="refresh_main")])
        kb.append([InlineKeyboardButton(text="➕ Добавить в группу", url=ADD_URL)])
        return "Ваши группы:", InlineKeyboardMarkup(inline_keyboard=kb)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить список", callback_data="refresh_main")],
            [InlineKeyboardButton(text="➕ Добавить в группу", url=ADD_URL)],
        ]
    )
    return "Я бот-модератор. Добавь меня в группу!", kb


# ==========================================
# РОУТЕРЫ
# ==========================================
private_router = Router()
group_router = Router()
group_router.message.filter(F.chat.type.in_({"group", "supergroup"}))


# ==========================================
# ОБРАБОТЧИКИ ГРУППЫ
# ==========================================
@group_router.message(CommandStart())
async def group_start_cmd(message: Message):
    try:
        await message.delete()
    except Exception:
        pass


@group_router.message(
    F.new_chat_members
    | F.left_chat_member
    | F.new_chat_title
    | F.new_chat_photo
    | F.delete_chat_photo
    | F.pinned_message
)
async def handle_system_messages(message: Message, bot: Bot):
    try:
        await message.delete()
    except Exception:
        pass

    if message.new_chat_members:
        for new_member in message.new_chat_members:
            if new_member.id == bot.id:
                # В Telegram это пользователь, совершивший добавление.
                # Сохраняем его как владельца панели.
                await add_group(message.chat.id, message.chat.title or "Группа", message.from_user.id)
                msg = await message.answer("Всем привет)")
                asyncio.create_task(delete_msg_after(bot, message.chat.id, msg.message_id, 15))
                return


@group_router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> MEMBER))
async def track_invites(event: ChatMemberUpdated, bot: Bot):
    adder_id = event.from_user.id
    new_user = event.new_chat_member.user
    chat_id = event.chat.id

    if new_user.id == bot.id:
        return

    await track_user(chat_id, new_user.id, new_user.first_name, new_user.username)

    if new_user.id != adder_id:
        await add_user_invites(adder_id, chat_id, 1)


@group_router.message(F.text | F.caption)
async def handle_group_msgs(message: Message, bot: Bot):
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = message.text or message.caption or ""

    await track_user(
        chat_id,
        user_id,
        message.from_user.first_name,
        message.from_user.username,
    )

    settings = await get_group_settings(chat_id)
    if not settings:
        return

    req_invites, spam_protect = settings

    cmd_info = parse_mod_command(text)
    if cmd_info:
        cmd, target_username, target_id_parsed, time_sec, reason = cmd_info
        target_id, target_name = None, None

        if message.reply_to_message and message.reply_to_message.from_user:
            target_id = message.reply_to_message.from_user.id
            target_name = message.reply_to_message.from_user.first_name
        elif target_id_parsed:
            target_id = target_id_parsed
            target_name = f"ID {target_id}"
        elif target_username:
            user_data = await get_user_by_username(chat_id, target_username)
            if user_data:
                target_id, target_name = user_data
            else:
                try:
                    await message.delete()
                except Exception:
                    pass
                msg = await message.answer(
                    f"Пользователь @{target_username} не найден. "
                    "Он должен написать хотя бы одно сообщение."
                )
                asyncio.create_task(delete_msg_after(bot, chat_id, msg.message_id, 15))
                return

        if target_id:
            has_rights = await check_mod_rights(bot, chat_id, user_id, cmd)
            if not has_rights:
                return

            # Нельзя управлять самим ботом.
            if target_id == bot.id:
                return

            try:
                await message.delete()
            except Exception:
                pass

            until = int(time.time()) + time_sec if time_sec > 0 else None
            mod_link = f"[{message.from_user.first_name}](tg://user?id={user_id})"
            target_link = f"[{target_name}](tg://user?id={target_id})"
            time_text = format_time_text(time_sec)

            msg = None

            try:
                if cmd == "бан":
                    await bot.ban_chat_member(
                        chat_id,
                        target_id,
                        until_date=until,
                        revoke_messages=True,
                    )
                    msg = await message.answer(
                        f"🔨 Участник {target_link} забанен\n"
                        f"Модератор: {mod_link}\n"
                        f"Время: {time_text}\n"
                        f"Причина: {reason}",
                        parse_mode="Markdown",
                    )
                    if time_sec > 0:
                        asyncio.create_task(
                            unban_unmute_task(bot, chat_id, target_id, target_name, time_sec, "бана")
                        )

                elif cmd == "мут":
                    await bot.restrict_chat_member(
                        chat_id,
                        target_id,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=until,
                    )
                    msg = await message.answer(
                        f"🤐 Участник {target_link} замучен\n"
                        f"Модератор: {mod_link}\n"
                        f"Время: {time_text}\n"
                        f"Причина: {reason}",
                        parse_mode="Markdown",
                    )
                    if time_sec > 0:
                        asyncio.create_task(
                            unban_unmute_task(bot, chat_id, target_id, target_name, time_sec, "мута")
                        )

                elif cmd == "кик":
                    await bot.ban_chat_member(chat_id, target_id)
                    await asyncio.sleep(0.5)
                    await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)

                    if message.reply_to_message:
                        try:
                            await message.reply_to_message.delete()
                        except Exception:
                            pass

                    msg = await message.answer(
                        f"👢 Участник {target_link} кикнут\n"
                        f"Модератор: {mod_link}\n"
                        f"Причина: {reason}",
                        parse_mode="Markdown",
                    )

                elif cmd == "разбан":
                    # Здесь был один из главных багов исходника:
                    # раньше вызывался restrict_chat_member вместо unban_chat_member.
                    await bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
                    msg = await message.answer(
                        f"✅ Участник {target_link} был разбанен\n"
                        f"Модератор: {mod_link}",
                        parse_mode="Markdown",
                    )

                elif cmd == "размут":
                    await bot.restrict_chat_member(
                        chat_id,
                        target_id,
                        permissions=UNMUTE_PERMS,
                    )
                    msg = await message.answer(
                        f"✅ Участник {target_link} был размучен\n"
                        f"Модератор: {mod_link}",
                        parse_mode="Markdown",
                    )

            except Exception as e:
                print(f"Ошибка модерации: {e}")
                msg = await message.answer(f"❌ Не удалось выполнить действие: {e}")

            if msg:
                asyncio.create_task(delete_msg_after(bot, chat_id, msg.message_id, 15))
            return

    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
            return
    except Exception:
        pass

    if req_invites > 0:
        current_invites, is_allowed = await get_user_invites(user_id, chat_id)
        if not is_allowed and current_invites < req_invites:
            try:
                await message.delete()
            except Exception:
                pass

            until_date = int(time.time()) + 60
            try:
                await bot.restrict_chat_member(
                    chat_id,
                    user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=until_date,
                )
            except Exception:
                pass

            invite_warnings.setdefault(chat_id, {})
            old_msg_id = invite_warnings[chat_id].get(user_id)
            if old_msg_id:
                try:
                    await bot.delete_message(chat_id, old_msg_id)
                except Exception:
                    pass

            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Я добавил", callback_data=f"check_{user_id}")],
                    [InlineKeyboardButton(text="🔓 Отпустить", callback_data=f"release_{user_id}")],
                ]
            )

            msg = await message.answer(
                f"[{message.from_user.first_name}](tg://user?id={user_id}), вам нельзя писать в группе!\n\n"
                f"Для получения доступа нужно добавить {req_invites} {plural_friends(req_invites)}.\n"
                "🤐 _Вы временно заглушены на 1 минуту, чтобы не спамить._",
                reply_markup=kb,
                parse_mode="Markdown",
            )

            invite_warnings[chat_id][user_id] = msg.message_id

            async def del_invite_warning(b, c, m, u):
                await asyncio.sleep(60)
                try:
                    await b.delete_message(c, m)
                except Exception:
                    pass
                if invite_warnings.get(c, {}).get(u) == m:
                    invite_warnings[c].pop(u, None)

            asyncio.create_task(del_invite_warning(bot, chat_id, msg.message_id, user_id))
            return

    if spam_protect:
        spam_cache.setdefault(chat_id, {})
        now = time.time()

        spam_cache[chat_id].setdefault(
            user_id,
            {
                "text": "",
                "dupes": 0,
                "history": [],
                "all_msgs": [],
                "verified": False,
                "pending": False,
                "banned": False,
            },
        )

        u_cache = spam_cache[chat_id][user_id]

        if u_cache.get("banned") or u_cache.get("pending"):
            try:
                await message.delete()
            except Exception:
                pass
            return

        u_cache["all_msgs"].append((message.message_id, now))
        u_cache["all_msgs"] = [
            (mid, t) for mid, t in u_cache["all_msgs"] if now - t <= 300
        ]

        u_cache["history"] = [t for t in u_cache["history"] if now - t <= 6]
        u_cache["history"].append(now)

        normalized = text.strip().lower()
        if normalized == u_cache["text"] and normalized:
            u_cache["dupes"] += 1
        else:
            u_cache["text"] = normalized
            u_cache["dupes"] = 1

        if u_cache["dupes"] >= 3 or len(u_cache["history"]) >= 10:
            try:
                await message.delete()
            except Exception:
                pass

            for mid, _ in u_cache["all_msgs"]:
                try:
                    await bot.delete_message(chat_id, mid)
                except Exception:
                    pass
            u_cache["all_msgs"] = []

            if u_cache["verified"]:
                if not u_cache.get("banned"):
                    u_cache["banned"] = True
                    try:
                        await bot.ban_chat_member(chat_id, user_id)
                    except Exception:
                        pass
                    spam_cache[chat_id].pop(user_id, None)
                    msg = await message.answer(
                        f"⛔️ Пользователь [{message.from_user.first_name}]"
                        f"(tg://user?id={user_id}) заблокирован за продолжение спама.",
                        parse_mode="Markdown",
                    )
                    asyncio.create_task(delete_msg_after(bot, chat_id, msg.message_id, 15))
            else:
                if not u_cache["pending"]:
                    u_cache["pending"] = True
                    try:
                        await bot.restrict_chat_member(
                            chat_id,
                            user_id,
                            permissions=ChatPermissions(can_send_messages=False),
                        )
                    except Exception:
                        pass

                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🤖 Я не бот", callback_data=f"captcha_{user_id}")]
                        ]
                    )
                    msg = await message.answer(
                        f"⚠️ [{message.from_user.first_name}](tg://user?id={user_id}), "
                        "сработала защита от спама.\n\n"
                        "Подтвердите, что вы живой человек. У вас есть 12 секунд до **БАНА**.",
                        reply_markup=kb,
                        parse_mode="Markdown",
                    )
                    asyncio.create_task(captcha_timer(bot, chat_id, user_id, msg.message_id))
            return


@group_router.callback_query(F.data.startswith("captcha_"))
async def captcha_verify(call: CallbackQuery, bot: Bot):
    target_id = int(call.data.split("_", 1)[1])
    if call.from_user.id != target_id:
        return await call.answer("Эта кнопка не для вас!", show_alert=True)

    chat_id = call.message.chat.id
    u_cache = spam_cache.get(chat_id, {}).get(target_id)

    if u_cache and u_cache.get("pending"):
        u_cache["verified"] = True
        u_cache["pending"] = False
        u_cache["dupes"] = 0
        u_cache["history"] = []

        await bot.restrict_chat_member(chat_id, target_id, permissions=UNMUTE_PERMS)
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer("Проверка пройдена. Можете писать!", show_alert=True)
    else:
        await call.answer("Проверка уже завершена или истекла.", show_alert=True)


@group_router.callback_query(F.data.startswith("check_"))
async def check_invites(call: CallbackQuery, bot: Bot):
    target_id = int(call.data.split("_", 1)[1])
    if call.from_user.id != target_id:
        return await call.answer("Это не ваша кнопка!", show_alert=True)

    settings = await get_group_settings(call.message.chat.id)
    req = settings[0] if settings else 0
    current, is_allowed = await get_user_invites(target_id, call.message.chat.id)

    if is_allowed or current >= req:
        await allow_user(target_id, call.message.chat.id)
        try:
            await bot.restrict_chat_member(
                call.message.chat.id,
                target_id,
                permissions=UNMUTE_PERMS,
            )
        except Exception:
            pass
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer("Доступ разрешен! Можете писать.", show_alert=True)
    else:
        await call.answer(f"Вы добавили только {current} из {req}!", show_alert=True)


@group_router.callback_query(F.data.startswith("release_"))
async def release_user(call: CallbackQuery, bot: Bot):
    try:
        member = await bot.get_chat_member(call.message.chat.id, call.from_user.id)
        if member.status not in [ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR]:
            return await call.answer(
                "Эта кнопка доступна только Владельцу и Админам!",
                show_alert=True,
            )
    except Exception:
        return

    target_id = int(call.data.split("_", 1)[1])
    await allow_user(target_id, call.message.chat.id)
    try:
        await bot.restrict_chat_member(
            call.message.chat.id,
            target_id,
            permissions=UNMUTE_PERMS,
        )
    except Exception:
        pass
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Пользователь отпущен! Теперь он может писать.", show_alert=True)


# ==========================================
# ЛИЧНЫЕ СООБЩЕНИЯ БОТУ
# ==========================================
@private_router.message(CommandStart(), F.chat.type == "private")
async def start_cmd(message: Message, state: FSMContext):
    data = await state.get_data()

    try:
        await message.delete()
    except Exception:
        pass

    last_bot_msg = data.get("last_bot_msg")
    if last_bot_msg:
        try:
            await message.chat.delete_message(last_bot_msg)
        except Exception:
            pass

    await state.clear()
    text, markup = await get_main_menu(message.from_user.id)
    msg = await message.answer(text, reply_markup=markup)
    await state.update_data(last_bot_msg=msg.message_id)


@private_router.callback_query(F.data == "refresh_main")
async def refresh_main(call: CallbackQuery):
    text, markup = await get_main_menu(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=markup)
        await call.answer("Список групп успешно обновлен!")
    except Exception:
        await call.answer("Изменений в списке групп нет.", show_alert=False)


@private_router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    last_bot_msg = data.get("last_bot_msg")

    await state.clear()
    if last_bot_msg:
        await state.update_data(last_bot_msg=last_bot_msg)

    text, markup = await get_main_menu(call.from_user.id)
    await call.message.edit_text(text, reply_markup=markup)


@private_router.callback_query(F.data.startswith("manage_"))
async def manage_group(call: CallbackQuery, state: FSMContext):
    group_id = int(call.data.split("_", 1)[1])

    # Дополнительная защита от старых/поддельных callback-кнопок.
    if not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("У вас нет доступа к этой группе.", show_alert=True)

    await state.update_data(current_group=group_id)
    clean_id = str(group_id).replace("-100", "", 1)

    groups = await get_user_groups(call.from_user.id)
    group_title = "Группа"
    for g_id, g_title in groups:
        if g_id == group_id:
            group_title = g_title
            break

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔗 Колл. обяз. приглашений",
                    callback_data="settings_invites",
                )
            ],
            [InlineKeyboardButton(text="🛡 Защита от спама", callback_data="settings_spam")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")],
        ]
    )
    await call.message.edit_text(
        f"⚙️ Управление группой\n[{group_title}](https://t.me/c/{clean_id}/1)",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@private_router.callback_query(F.data == "settings_invites")
async def settings_invites(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    group_id = data.get("current_group")
    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа к этой группе.", show_alert=True)

    settings = await get_group_settings(group_id)
    req_invites = settings[0] if settings else 0

    status_text = "Включено" if req_invites > 0 else "Выключено"
    btn_text = "Выключить" if req_invites > 0 else "Включить"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"toggle_invites_{req_invites}",
                )
            ],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"manage_{group_id}")],
        ]
    )

    await call.message.edit_text(
        "Количество обязательных приглашений\n"
        f"Состояние: **{status_text}**\n"
        f"Сейчас: {req_invites} человек\n\n"
        "Напишите сколько человек надо добавить (или переключите статус):",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await state.set_state(BotConfig.waiting_for_invite_count)
    await state.update_data(msg_to_edit=call.message)


@private_router.callback_query(F.data.startswith("toggle_invites_"))
async def toggle_invites(call: CallbackQuery, state: FSMContext):
    current_val = int(call.data.split("_")[2])
    group_id = (await state.get_data()).get("current_group")

    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа.", show_alert=True)

    await update_req_invites(group_id, 0 if current_val > 0 else 1)
    await settings_invites(call, state)


@private_router.message(BotConfig.waiting_for_invite_count, F.chat.type == "private")
async def process_invite_count(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        return

    count = int(message.text)
    if count < 0 or count > 1000:
        return

    data = await state.get_data()
    group_id = data.get("current_group")
    msg_to_edit = data.get("msg_to_edit")

    if not group_id or not msg_to_edit:
        return

    if not await user_owns_group(message.from_user.id, group_id):
        await state.clear()
        return

    await asyncio.sleep(0.5)

    try:
        await message.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_invites_{count}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"manage_{group_id}")],
        ]
    )

    try:
        await msg_to_edit.edit_text(
            f"Количество обязательных приглашений\nВыбрано: {count} человек\n\n"
            "Подтвердите сохранение:",
            reply_markup=kb,
        )
    except Exception:
        pass

    await state.set_state(None)


@private_router.callback_query(F.data.startswith("confirm_invites_"))
async def confirm_invites(call: CallbackQuery, state: FSMContext):
    count = int(call.data.split("_")[2])
    group_id = (await state.get_data()).get("current_group")

    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа.", show_alert=True)

    await update_req_invites(group_id, count)

    status_text = "Включено" if count > 0 else "Выключено"
    btn_text = "Выключить" if count > 0 else "Включить"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"toggle_invites_{count}",
                )
            ],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"manage_{group_id}")],
        ]
    )

    await call.message.edit_text(
        "Количество обязательных приглашений\n"
        f"Состояние: **{status_text}**\n"
        f"Сейчас: {count} человек",
        reply_markup=kb,
        parse_mode="Markdown",
    )


@private_router.callback_query(F.data == "settings_spam")
async def settings_spam(call: CallbackQuery, state: FSMContext):
    group_id = (await state.get_data()).get("current_group")
    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа.", show_alert=True)

    settings = await get_group_settings(group_id)
    spam_status = settings[1] if settings else False
    status_text = "Включено" if spam_status else "Выключено"
    btn_text = "Выключить" if spam_status else "Включить"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=btn_text, callback_data="toggle_spam")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"manage_{group_id}")],
        ]
    )

    text = (
        "🛡 **Защита от спама** поможет удалять повторяющиеся сообщения.\n\n"
        "**Как это работает:**\n"
        "• **10 сообщений за 6 секунд** ➔ Проверка (Капча).\n"
        "• **3 одинаковых сообщения подряд** ➔ Проверка (Капча).\n"
        "• **Не прошел проверку (12 сек)** ➔ Бан и удаление спама.\n"
        "• **Прошел, но снова спамит** ➔ Бан и удаление спама.\n\n"
        f"Состояние: **{status_text}**"
    )
    await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@private_router.callback_query(F.data == "toggle_spam")
async def toggle_spam_handler(call: CallbackQuery, state: FSMContext):
    group_id = (await state.get_data()).get("current_group")
    if not group_id or not await user_owns_group(call.from_user.id, group_id):
        return await call.answer("Нет доступа.", show_alert=True)

    await toggle_spam(group_id)
    await settings_spam(call, state)


# ==========================================
# WEB SERVER ДЛЯ RENDER
# ==========================================
async def handle_health(request):
    return web.Response(text="ok")


async def handle_root(request):
    return web.Response(text="QwertyGuard_Bot is running")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()

    print(f"Web server started on 0.0.0.0:{WEB_PORT}")
    return runner


# ==========================================
# ЗАПУСК
# ==========================================
async def main():
    global supabase

    # Запускаем HTTP раньше polling, чтобы Render сразу увидел порт.
    runner = await start_web_server()

    try:
        await init_db()
        print("Supabase connection: OK")

        bot = Bot(token=BOT_TOKEN)
        dp = Dispatcher()

        dp.include_router(private_router)
        dp.include_router(group_router)

        print("Bot polling started")
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        if supabase is not None:
            try:
                await supabase.auth.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(main())
