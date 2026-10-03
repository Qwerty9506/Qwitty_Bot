"""Admin screens, live statistics, account inspection and server restart."""
import asyncio
import datetime
import html
import json
import logging
import os
import time
import psutil
import aiohttp
from aiogram import F, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
import userbot
import guard
from userbot import bot, dp, MEMORY_DB, async_db_save
from userbot import SUPABASE_DB_SIZE_RPC, SUPABASE_DB_LIMIT_MB, supabase, format_remaining_time

ADMIN_ID = userbot.ADMIN_ID


def _html(value):
    return html.escape(str(value), quote=False)


RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
RENDER_INSTANCE_ID = os.getenv("RENDER_INSTANCE_ID", "").strip()
RESTART_PENDING_KEY = "server_restart_pending"
RESTART_SUCCESS_AFTER_SECONDS = 45
RESTART_ANIMATION_TASKS = {}
RESTART_FINALIZE_TASKS = set()


def build_admin_menu_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text=userbot.get_text(ADMIN_ID, "btn_server_stats"), callback_data="admin_server_stats")
    builder.button(text="Перезапуск сервера ♻️", callback_data="admin_restart_server")
    builder.button(text="Активные Сессии 🟢", callback_data="admin_users_1")
    builder.button(text="Не-входящие 🔴", callback_data="admin_entries_1")
    builder.button(text="Глобальные паблики 🗂", callback_data="admin_public_groups")
    builder.button(text="Глобальные жалобы 🌐", callback_data="admin_global_reports")
    builder.button(text="Назад в главное меню 🏠", callback_data="root_menu")
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup()


def build_restart_confirm_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="Да, я уверен", callback_data="admin_restart_confirm")
    builder.button(text="Назад", callback_data="admin_menu")
    builder.adjust(1)
    return builder.as_markup()


def build_restart_done_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="OK", callback_data="admin_restart_ok")
    builder.adjust(1)
    return builder.as_markup()


async def open_admin_menu_for(user_id: int):
    userbot.stop_admin_server_stats_loop(user_id)
    data = userbot.get_user_state(user_id)
    data["state"] = "ADMIN"
    data.pop("admin_gban_target", None)
    data.pop("admin_gban_mode", None)
    data.pop("admin_gban_return_gid", None)
    await userbot.edit_or_send(user_id, "<b>👑 Админ-меню</b>\n<i>Управление сервером и пользователями</i>", reply_markup=build_admin_menu_markup(), parse_mode="HTML")


@dp.message(F.chat.type == "private", F.text.casefold() == "admin")
async def admin_command(message: types.Message):
    if not userbot.is_admin(message.from_user):
        return
    await open_admin_menu_for(message.from_user.id)


@dp.callback_query(F.data.in_(["admin_menu", "admin_users_back"]))
async def admin_menu(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    await open_admin_menu_for(callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass


@dp.callback_query(F.data.in_(["admin_server_stats", "admin_server_stats_refresh"]))
async def admin_server_stats(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    user_id = callback.from_user.id
    userbot.stop_admin_server_stats_loop(user_id)
    data = userbot.get_user_state(user_id)
    data["state"] = "ADMIN_STATS"
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    cache = await refresh_server_stats_cache()
    if data.get("state") != "ADMIN_STATS":
        return
    await userbot.edit_or_send(
        user_id,
        _build_server_stats_text(cache),
        reply_markup=build_admin_stats_markup(),
        parse_mode="HTML",
    )
    data["admin_stats_active"] = True
    data["admin_stats_task"] = asyncio.create_task(_admin_server_stats_loop(user_id))


@dp.callback_query(F.data == "admin_server_stats_back")
async def admin_server_stats_back(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    await open_admin_menu_for(callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("admin_entries_"))
async def admin_entries_list(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    userbot.stop_admin_server_stats_loop(callback.from_user.id)

    try:
        page = int(callback.data.split("_")[-1])
    except (ValueError, TypeError):
        page = 1

    entries_by_id = {}
    for uid, cfg in MEMORY_DB["config"].items():
        if cfg.get("logged_in", False):
            continue
        if not cfg.get("last_entry_at") and not cfg.get("last_entry_ts"):
            continue
        try:
            normalized_uid = str(int(uid))
        except (TypeError, ValueError):
            normalized_uid = str(uid)
        entries_by_id[normalized_uid] = (normalized_uid, cfg)
    entries = list(entries_by_id.values())

    entries.sort(
        key=lambda item: userbot.entry_time_text(item[1])[6:10]
        + userbot.entry_time_text(item[1])[3:5]
        + userbot.entry_time_text(item[1])[:2]
        + userbot.entry_time_text(item[1])[11:],
        reverse=True,
    )

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

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"admin_entries_{page-1}"))
        nav_buttons.append(types.InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_entries_{page+1}"))
        builder.row(*nav_buttons)
    builder.row(types.InlineKeyboardButton(text="⬅️ В админ меню", callback_data="admin_users_back"))

    await userbot.edit_or_send(
        callback.from_user.id,
        f"<b>🔴 Не-входящие</b>\n<i>Всего: {total_entries}</i>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("admin_entry_"))
async def admin_entry_view(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    userbot.stop_admin_server_stats_loop(callback.from_user.id)
    target_uid = callback.data.split("_")[-1]
    cfg = userbot.cached_config(target_uid)
    if cfg.get("logged_in", False):
        await callback.answer("Аккаунт уже подключён. Обновите список.", show_alert=True)
        return

    first_name = cfg.get("entry_first_name") or cfg.get("first_name") or "User"
    username = cfg.get("entry_username") or cfg.get("username") or "N/A"
    username_str = f"@{username}" if username != "N/A" else "Не указан"
    entry_time = userbot.entry_time_text(cfg)

    text = (
        "<b>👤 Профиль пользователя</b>\n\n"
        f"<b>Никнейм:</b> {guard.user_link(target_uid, first_name)}\n"
        f"<b>Юзернейм:</b> {guard.user_link(target_uid, username_str) if username and username != 'N/A' else _html(username_str)}\n"
        f"<b>Последний вход:</b> <i>{_html(entry_time)}</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_entries_1")
    builder.adjust(1)
    await userbot.edit_or_send(callback.from_user.id, text, reply_markup=builder.as_markup(), parse_mode="HTML")
    try:
        await callback.answer()
    except Exception:
        pass


def _admin_page_nav(builder, page, pages, prefix, total=None):
    if pages <= 1 and (total is None or total < 5):
        return
    buttons = []
    if page > 1:
        buttons.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}{page-1}"))
    buttons.append(types.InlineKeyboardButton(text=f"{page}/{pages}", callback_data="ignore"))
    if page < pages:
        buttons.append(types.InlineKeyboardButton(text="➡️", callback_data=f"{prefix}{page+1}"))
    builder.row(*buttons)


def _active_guard_groups():
    return sorted(
        [group for group in guard.STORE.groups.values() if group.get("active")],
        key=lambda group: (float(group.get("last_activity_at") or group.get("joined_at") or 0), int(group.get("id", 0))),
        reverse=True,
    )


async def _render_admin_public_groups(user_id: int, page: int = 1):
    groups = _active_guard_groups()
    per_page = 5
    pages = max(1, (len(groups) + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    builder = InlineKeyboardBuilder()
    for group in groups[(page - 1) * per_page:page * per_page]:
        builder.button(text=guard.clip(group.get("title") or str(group["id"]), 55), callback_data=f"admin_public_group_{group['id']}")
    builder.adjust(1)
    _admin_page_nav(builder, page, pages, "admin_public_groups_", len(groups))
    builder.row(types.InlineKeyboardButton(text="Назад в меню ⬅️", callback_data="admin_menu"))
    text = "<b>Глобальные группы:</b>"
    if not groups:
        text += "\n\nГрупп пока нет."
    await userbot.edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _render_admin_public_group(user_id: int, gid: int):
    group = guard.STORE.groups.get(int(gid))
    if not group or not group.get("active"):
        raise ValueError("Группа больше недоступна.")
    title = await guard.group_title_link(group)
    try:
        admins = await guard.admin_ids(group["id"], force=True)
        count = await bot.get_chat_member_count(group["id"])
        group["member_count"] = count
    except Exception:
        admins = {}
        count = group.get("member_count") or "?"
    admin_links = [guard.user_link(member.user.id, member.user.full_name) for member in admins.values() if not member.user.is_bot]
    settings = group.get("settings", {})
    protection_active = any(settings.get(mode) for mode in guard.MODES)
    text = (
        f"<b>{title}</b>\n\n"
        f"<b>Участники:</b> {count}\n\n"
        f"<b>Админы:</b> {', '.join(admin_links) if admin_links else 'Нет данных'}\n\n"
        f"<b>Защита группы</b> - {'активна' if protection_active else 'неактивна'}\n\n"
        f"<b>АнтиСпам</b> - {'вкл' if settings.get('spam') else 'выкл'}\n"
        f"<b>АнтиВирус</b> - {'вкл' if settings.get('files') else 'выкл'}\n"
        f"<b>АнтиНакрутка</b> - {'вкл' if settings.get('raid') else 'выкл'}\n"
        f"<b>Очистка вх/изм</b> - {'вкл' if settings.get('service') else 'выкл'}"
    )
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Назад в меню ⬅️", callback_data="admin_public_groups_1"))
    builder.adjust(1)
    await userbot.edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _report_detail_text(group, target: int, item: dict):
    if group:
        group_link = await guard.group_title_link(group)
        heading = f"<b>Жалобы из чата {group_link}:</b>"
    else:
        heading = "<b>Глобальная жалоба</b>"
    username = f"@{item.get('username')}" if item.get("username") else "Не указан"
    messages_text = await guard.reported_messages_html(group or {"id": 0, "title": "Группа"}, item)
    reasons = [guard.clip(reason.get("text") or "", 240) for reason in guard.report_reasons(item)]
    return (
        heading + "\n\n"
        + guard.user_link(target, item.get("name") or "Пользователь") + "\n"
        + (guard.user_link(target, username) if item.get("username") else _html(username)) + "\n\n"
        + "<b>Последние сообщения:</b>\n" + messages_text + "\n\n"
        + "<b>Причины:</b> " + (", ".join(_html(reason) for reason in reasons) if reasons else "Причины не указаны")
    )


async def _render_admin_public_reports(user_id: int, gid: int, page: int = 1):
    group = guard.STORE.groups.get(int(gid))
    if not group or not group.get("active"):
        raise ValueError("Группа больше недоступна.")
    rows = sorted(group.get("reports", {}).items(), key=lambda row: row[1].get("updated", 0), reverse=True)
    per_page = 5
    pages = max(1, (len(rows) + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    builder = InlineKeyboardBuilder()
    for target, item in rows[(page - 1) * per_page:page * per_page]:
        builder.button(text=guard.clip(item.get("name") or "Пользователь", 50), callback_data=f"admin_public_report_{gid}_{target}")
    builder.adjust(1)
    _admin_page_nav(builder, page, pages, f"admin_public_reports_{gid}_")
    builder.row(types.InlineKeyboardButton(text="Назад в меню ⬅️", callback_data=f"admin_public_group_{gid}"))
    title = await guard.group_title_link(group)
    text = f"<b>Жалобы из чата {title}:</b>"
    if not rows:
        text += "\n\nОткрытых жалоб нет."
    await userbot.edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _render_admin_report_detail(user_id: int, gid: int, target: int, source: str):
    group = guard.STORE.groups.get(int(gid))
    if source == "global":
        if str(int(target)) in guard.STORE.global_bans:
            await _render_admin_banned_detail(user_id, int(target))
            return
        group, item = guard.aggregate_local_bans(target)
    else:
        item = group.get("reports", {}).get(str(int(target))) if group else None
    if not item:
        raise ValueError("Пользователь уже разбанен или запись обработана.")
    builder = InlineKeyboardBuilder()
    if source == "global":
        builder.button(text="Глобально забанить", callback_data=f"admin_gban:g:{int(target)}")
        builder.button(text="Игнорить", callback_data="admin_global_new_1")
        builder.button(text="Назад в меню ⬅️", callback_data="admin_global_new_1")
    else:
        builder.button(text="Глобально забанить", callback_data=f"admin_gban:p:{int(gid)}:{int(target)}")
        builder.button(text="Игнорить", callback_data=f"admin_public_reports_{int(gid)}_1")
        builder.button(text="Назад в меню ⬅️", callback_data=f"admin_public_reports_{int(gid)}_1")
    builder.adjust(1)
    await userbot.edit_or_send(user_id, await _report_detail_text(group, int(target), item), reply_markup=builder.as_markup(), parse_mode="HTML")


async def _render_admin_global_reports(user_id: int, page: int = 1):
    builder = InlineKeyboardBuilder()
    builder.row(types.InlineKeyboardButton(text="Новые", callback_data="admin_global_new_1"),
                types.InlineKeyboardButton(text="Забаненные", callback_data="admin_global_banned_1"))
    builder.row(types.InlineKeyboardButton(text="Назад в меню ⬅️", callback_data="admin_menu"))
    await userbot.edit_or_send(user_id, "<b>🌐 Глобальные жалобы</b>\n<i>Выберите список пользователей.</i>",
                              reply_markup=builder.as_markup(), parse_mode="HTML")


async def _render_admin_new_reports(user_id: int, page: int = 1):
    rows = guard.global_reports_rows()
    pages = max(1, (len(rows) + 4) // 5)
    page = max(1, min(page, pages))
    builder = InlineKeyboardBuilder()
    for group, target, item in rows[(page - 1) * 5:page * 5]:
        builder.button(text=guard.clip(item.get("name") or "Пользователь", 50), callback_data=f"admin_global_report_{group['id']}_{target}")
    builder.adjust(1)
    _admin_page_nav(builder, page, pages, "admin_global_new_", len(rows))
    builder.row(types.InlineKeyboardButton(text="Назад в меню ⬅️", callback_data="admin_global_reports"))
    text = "<b>🆕 Забаненные в группах</b>\n<i>Кандидаты на глобальную блокировку.</i>"
    if not rows:
        text += "\n\n<i>Новых забаненных пользователей нет.</i>"
    await userbot.edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _render_admin_banned_list(user_id: int, page: int = 1):
    rows = guard.global_banned_rows()
    pages = max(1, (len(rows) + 4) // 5)
    page = max(1, min(page, pages))
    builder = InlineKeyboardBuilder()
    for uid, entry in rows[(page - 1) * 5:page * 5]:
        builder.button(text=guard.clip(entry.get("name") or "Пользователь", 50), callback_data=f"admin_banned_user_{uid}")
    builder.adjust(1)
    _admin_page_nav(builder, page, pages, "admin_global_banned_", len(rows))
    builder.row(types.InlineKeyboardButton(text="Назад в меню ⬅️", callback_data="admin_global_reports"))
    text = "<b>🚫 Забаненные</b>"
    if not rows:
        text += "\n\n<i>Глобально забаненных пользователей нет.</i>"
    await userbot.edit_or_send(user_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _render_admin_banned_detail(user_id: int, target: int):
    entry = guard.global_ban_entry(target)
    if not entry or entry.get("unbanning"):
        raise ValueError("Глобальный бан уже снят.")
    item = dict(entry.get("report") or {})
    item.setdefault("name", entry.get("name") or "Пользователь")
    item.setdefault("username", entry.get("username"))
    item.setdefault("updated", entry.get("updated", 0))
    item.setdefault("messages", [])
    global_reason = str(entry.get("global_reason") or "").strip()
    if not global_reason:
        stored = entry.get("reasons") or []
        global_reason = str(stored[0]).strip() if stored else "Без описания"
    item["reasons"] = [{"text": global_reason, "at": entry.get("updated", 0)}]
    group = entry.get("group")
    if group and group.get("id") is not None:
        group = guard.STORE.groups.get(int(group["id"])) or group
    builder = InlineKeyboardBuilder()
    builder.button(text="Глобально разбанить", callback_data=f"admin_gunban:{target}")
    builder.button(text="Игнорить", callback_data="admin_global_banned_1")
    builder.button(text="Назад в меню ⬅️", callback_data="admin_global_banned_1")
    builder.adjust(1)
    await userbot.edit_or_send(user_id, await _report_detail_text(group, target, item), reply_markup=builder.as_markup(), parse_mode="HTML")


@dp.callback_query(F.data == "admin_public_groups")
@dp.callback_query(F.data.startswith("admin_public_groups_"))
async def admin_public_groups(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    userbot.stop_admin_server_stats_loop(callback.from_user.id)
    try:
        page = int(callback.data.rsplit("_", 1)[1]) if callback.data != "admin_public_groups" else 1
        await _render_admin_public_groups(callback.from_user.id, page)
        await callback.answer()
    except (ValueError, TypeError):
        await callback.answer("Не удалось открыть список групп.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_public_group_"))
async def admin_public_group(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    try:
        gid = int(callback.data.removeprefix("admin_public_group_"))
        await _render_admin_public_group(callback.from_user.id, gid)
        await callback.answer()
    except (ValueError, TelegramBadRequest) as exc:
        await callback.answer(str(exc) or "Группа недоступна.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_public_reports_"))
async def admin_public_reports(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    try:
        rest = callback.data.removeprefix("admin_public_reports_")
        gid_text, page_text = rest.rsplit("_", 1)
        await _render_admin_public_reports(callback.from_user.id, int(gid_text), int(page_text))
        await callback.answer()
    except (ValueError, TelegramBadRequest) as exc:
        await callback.answer(str(exc) or "Жалобы недоступны.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_public_report_"))
async def admin_public_report(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    try:
        rest = callback.data.removeprefix("admin_public_report_")
        gid_text, target_text = rest.rsplit("_", 1)
        await _render_admin_report_detail(callback.from_user.id, int(gid_text), int(target_text), "public")
        await callback.answer()
    except (ValueError, TelegramBadRequest) as exc:
        await callback.answer(str(exc) or "Жалоба недоступна.", show_alert=True)


@dp.callback_query(F.data == "admin_global_reports")
@dp.callback_query(F.data.startswith("admin_global_reports_"))
async def admin_global_reports(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    userbot.stop_admin_server_stats_loop(callback.from_user.id)
    try:
        page = int(callback.data.rsplit("_", 1)[1]) if callback.data != "admin_global_reports" else 1
        await _render_admin_global_reports(callback.from_user.id, page)
        await callback.answer()
    except (ValueError, TypeError):
        await callback.answer("Не удалось открыть глобальные жалобы.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_global_report_"))
async def admin_global_report(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    try:
        rest = callback.data.removeprefix("admin_global_report_")
        gid_text, target_text = rest.rsplit("_", 1)
        await _render_admin_report_detail(callback.from_user.id, int(gid_text), int(target_text), "global")
        await callback.answer()
    except (ValueError, TelegramBadRequest) as exc:
        await callback.answer(str(exc) or "Жалоба недоступна.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_gban:"))
async def admin_global_ban(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    parts = callback.data.split(":")
    try:
        mode = parts[1]
        if mode == "g":
            target = int(parts[2])
            return_gid = None
        elif mode == "p":
            return_gid = int(parts[2])
            target = int(parts[3])
        else:
            raise ValueError("Кнопка устарела.")

                                                                               
                                                                                   
        data = userbot.get_user_state(callback.from_user.id)
        data["state"] = "ADMIN_GBAN_REASON"
        data["admin_gban_target"] = target
        data["admin_gban_mode"] = mode
        data["admin_gban_return_gid"] = return_gid

        builder = InlineKeyboardBuilder()
        builder.button(text="Отмена ⬅️", callback_data="admin_gban_cancel")
        builder.adjust(1)
        await userbot.edit_or_send(
            callback.from_user.id,
            f"<b>🌐 Причина глобального бана</b>\n\n"
            f"Пользователь: {guard.user_link(target, 'Открыть профиль')}\n\n"
            "Напишите причину следующим сообщением. Она будет сохранена как причина глобального бана "
            "и показана во всех группах, где Qwitty применит эту блокировку.\n\n"
            "<i>Максимум 240 символов.</i>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        await callback.answer("Введите причину глобального бана", cache_time=0)
    except (ValueError, IndexError, TelegramBadRequest) as exc:
        try:
            await callback.answer(str(exc) or "Не удалось открыть ввод причины.", show_alert=True)
        except TelegramBadRequest:
            pass


@dp.callback_query(F.data == "admin_gban_cancel")
async def admin_global_ban_cancel(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    data = userbot.get_user_state(callback.from_user.id)
    mode = data.pop("admin_gban_mode", None)
    return_gid = data.pop("admin_gban_return_gid", None)
    data.pop("admin_gban_target", None)
    data["state"] = "ADMIN"
    try:
        await callback.answer("Глобальный бан отменён", cache_time=0)
    except TelegramBadRequest:
        pass
    if mode == "p" and return_gid in guard.STORE.groups:
        await _render_admin_public_reports(callback.from_user.id, int(return_gid), 1)
    else:
        await _render_admin_new_reports(callback.from_user.id, 1)


async def _delete_private_message_later(chat_id: int, message_id: int, delay: float = 3.0):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramRetryAfter):
        pass
    except Exception:
        pass


@dp.message(
    F.chat.type == "private",
    F.from_user,
    F.text,
    lambda msg: userbot.is_admin(msg.from_user)
    and userbot.get_user_state(msg.from_user.id).get("state") == "ADMIN_GBAN_REASON",
)
async def admin_global_ban_reason(message: types.Message):
    data = userbot.get_user_state(message.from_user.id)
    reason = (message.text or "").strip()
    target = data.get("admin_gban_target")
    mode = data.get("admin_gban_mode")
    return_gid = data.get("admin_gban_return_gid")

    if target is None or mode not in {"g", "p"}:
        data["state"] = "ADMIN"
        data.pop("admin_gban_target", None)
        data.pop("admin_gban_mode", None)
        data.pop("admin_gban_return_gid", None)
        await userbot.edit_or_send(message.from_user.id, "⚠️ Запрос глобального бана устарел. Откройте жалобу заново.", parse_mode="HTML")
        return
    if not reason:
        builder = InlineKeyboardBuilder()
        builder.button(text="Отмена ⬅️", callback_data="admin_gban_cancel")
        await userbot.edit_or_send(
            message.from_user.id,
            "⚠️ Причина не может быть пустой. Напишите причину глобального бана.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        return
    if len(reason) > 240:
        builder = InlineKeyboardBuilder()
        builder.button(text="Отмена ⬅️", callback_data="admin_gban_cancel")
        await userbot.edit_or_send(
            message.from_user.id,
            f"⚠️ Причина слишком длинная: {len(reason)}/240 символов. Сократите текст и отправьте ещё раз.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        return

    data["state"] = "ADMIN_GBAN_APPLYING"
    asyncio.create_task(_delete_private_message_later(message.chat.id, message.message_id, 3.0))
    try:
        await userbot.edit_or_send(
            message.from_user.id,
            f"<b>🌐 Применяю глобальный бан…</b>\n\n<b>Причина:</b> {_html(reason)}",
            parse_mode="HTML",
        )
        banned_count = await guard.global_ban_user(int(target), reason=reason)
        data.pop("admin_gban_target", None)
        data.pop("admin_gban_mode", None)
        data.pop("admin_gban_return_gid", None)
        data["state"] = "ADMIN"
        if mode == "p" and return_gid in guard.STORE.groups:
            await _render_admin_public_reports(message.from_user.id, int(return_gid), 1)
        else:
            await _render_admin_new_reports(message.from_user.id, 1)
        logging.info("Global ban %s applied to %s managed groups", target, banned_count)
    except (ValueError, TelegramBadRequest) as exc:
        data["state"] = "ADMIN_GBAN_REASON"
        builder = InlineKeyboardBuilder()
        builder.button(text="Отмена ⬅️", callback_data="admin_gban_cancel")
        await userbot.edit_or_send(
            message.from_user.id,
            f"⚠️ {_html(str(exc) or 'Не удалось применить глобальный бан.')}\n\n"
            "Причина ещё не потеряна. Исправьте её или нажмите отмену.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    except Exception:
        data["state"] = "ADMIN_GBAN_REASON"
        logging.exception("Не удалось применить глобальный бан")
        builder = InlineKeyboardBuilder()
        builder.button(text="Отмена ⬅️", callback_data="admin_gban_cancel")
        await userbot.edit_or_send(
            message.from_user.id,
            "⚠️ Не удалось применить глобальный бан. Попробуйте отправить причину ещё раз или отмените действие.",
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )


@dp.callback_query(F.data.startswith("admin_global_new_"))
async def admin_global_new(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    try:
        await callback.answer()
        await _render_admin_new_reports(callback.from_user.id, int(callback.data.rsplit("_", 1)[1]))
    except (ValueError, TelegramBadRequest):
        await callback.answer("Список жалоб недоступен.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_global_banned_"))
async def admin_global_banned(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    try:
        await callback.answer()
        await _render_admin_banned_list(callback.from_user.id, int(callback.data.rsplit("_", 1)[1]))
    except (ValueError, TelegramBadRequest):
        await callback.answer("Список забаненных недоступен.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_banned_user_"))
async def admin_banned_user(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    try:
        await _render_admin_banned_detail(callback.from_user.id, int(callback.data.rsplit("_", 1)[1]))
        await callback.answer()
    except (ValueError, TelegramBadRequest) as exc:
        await callback.answer(str(exc) or "Карточка недоступна.", show_alert=True)


@dp.callback_query(F.data.startswith("admin_gunban:"))
async def admin_global_unban(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    try:
        target = int(callback.data.split(":", 1)[1])
        await callback.answer("Глобальный разбан…", cache_time=0)
        await guard.global_unban_user(target)
        await _render_admin_banned_list(callback.from_user.id, 1)
    except (ValueError, TelegramBadRequest) as exc:
        await callback.answer(str(exc) or "Не удалось снять глобальный бан.", show_alert=True)


async def refresh_admin_user_snapshot(user_id: int, include_devices: bool = False):
    if not await userbot.ensure_client_connected(user_id, force_check=True):
        return None, None
    cfg = userbot.cached_config(user_id)
    client = userbot.get_user_state(user_id).get("client")
    if client:
        try:
            me = await client.get_me()
            cfg.update(profile_display_name=" ".join(filter(None, [me.first_name, me.last_name])),
                       username=me.username, phone_number=me.phone_number)
            await userbot.persist_user_config_now(user_id, cfg)
        except Exception:
            logging.debug("Profile refresh failed for %s", user_id, exc_info=True)
    phone = cfg.get("phone_number") or cfg.get("phone")
    return cfg, ("+" + str(phone).strip().lstrip("+") if phone else None)


async def admin_validate_session(user_id, cfg, semaphore=None):
    if semaphore is not None:
        async with semaphore:
            return await _admin_validate_session(user_id, cfg)
    return await _admin_validate_session(user_id, cfg)


async def _admin_validate_session(user_id, cfg):
    if not cfg.get("logged_in"):
        return False
    fresh_cfg, _ = await refresh_admin_user_snapshot(user_id, include_devices=False)
    return bool(fresh_cfg and fresh_cfg.get("logged_in"))


@dp.callback_query(F.data.startswith("admin_users_"))
async def admin_users_list(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    userbot.stop_admin_server_stats_loop(callback.from_user.id)

    try:
        page = int(callback.data.split("_")[-1])
    except (ValueError, TypeError):
        page = 1

    config_by_id = {}
    for uid, cfg in MEMORY_DB["config"].items():
        try:
            normalized_uid = str(int(uid))
        except (TypeError, ValueError):
            normalized_uid = str(uid)
        config_by_id[normalized_uid] = (normalized_uid, cfg)
    all_configs = list(config_by_id.values())
    validation_semaphore = asyncio.Semaphore(5)
    candidates = [(uid, cfg) for uid, cfg in all_configs if cfg.get("logged_in", False)]
    validation_tasks = [
        admin_validate_session(int(uid), cfg, validation_semaphore)
        for uid, cfg in all_configs
        if cfg.get("logged_in", False)
    ]
    validation_results = await asyncio.gather(*validation_tasks, return_exceptions=True)

    active_configs = []
    for (uid, cfg), result in zip(candidates, validation_results):
        if result is True:
            active_configs.append((uid, userbot.cached_config(uid)))
        elif isinstance(result, Exception):
            logging.warning("Ошибка проверки активности %s: %s", uid, type(result).__name__)

    active_configs.sort(key=lambda item: int(item[1].get("logged_in", False)), reverse=True)

    per_page = 5
    total_users = len(active_configs)
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    current_page_users = active_configs[(page - 1) * per_page:page * per_page]

    builder = InlineKeyboardBuilder()
    for uid, cfg in current_page_users:
        first_name = cfg.get("profile_display_name") or cfg.get("first_name") or cfg.get("profile_base_first_name") or "User"
        builder.button(text=f"👤 {first_name}", callback_data=f"admin_user_{uid}")
    builder.adjust(1)

    if total_pages > 1:
        nav_buttons = []
        if page > 1:
            nav_buttons.append(types.InlineKeyboardButton(text="⬅️", callback_data=f"admin_users_{page-1}"))
        nav_buttons.append(types.InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data="ignore"))
        if page < total_pages:
            nav_buttons.append(types.InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_users_{page+1}"))
        builder.row(*nav_buttons)
    builder.row(types.InlineKeyboardButton(text="⬅️ В админ меню", callback_data="admin_users_back"))

    await userbot.edit_or_send(callback.from_user.id, f"<b>🟢 Активные пользователи</b>\n<i>Всего: {total_users}</i>", reply_markup=builder.as_markup(), parse_mode="HTML")
    visible_ids = [uid for uid, _ in current_page_users]
    async def refresh():
        markup = builder.as_markup()
        for row in markup.inline_keyboard:
            for btn in row:
                if (btn.callback_data or "").startswith("admin_user_"):
                    uid = btn.callback_data.removeprefix("admin_user_")
                    cfg = userbot.cached_config(uid)
                    btn.text = "👤 " + (cfg.get("profile_display_name") or cfg.get("first_name") or "User")
        await userbot.edit_or_send(callback.from_user.id,
            f"<b>🟢 Активные пользователи</b>\n<i>Всего: {total_users}</i>",
            reply_markup=markup, parse_mode="HTML")
    start_live_profile(callback.from_user.id, refresh,
        lambda: any(userbot.cached_config(uid).get("time_nick_active") for uid in visible_ids))

    try:
        await callback.answer()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("admin_user_"))
async def admin_user_view(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    userbot.stop_admin_server_stats_loop(callback.from_user.id)
    target_uid = callback.data.split("_")[-1]

    fresh_cfg, phone_number = await refresh_admin_user_snapshot(int(target_uid), include_devices=True)
    if fresh_cfg is None:
        await callback.answer("Сессия пользователя сейчас недоступна. Обновите список.", show_alert=True)
        return

    await render_active_profile(callback.from_user.id, target_uid, fresh_cfg, phone_number)
    async def refresh():
        cfg, phone = await refresh_admin_user_snapshot(int(target_uid), include_devices=True)
        if cfg:
            await render_active_profile(callback.from_user.id, target_uid, cfg, phone)
    start_live_profile(callback.from_user.id, refresh, lambda: bool(userbot.cached_config(target_uid).get("time_nick_active")))
    await callback.answer()


async def render_active_profile(viewer_id, target_uid, fresh_cfg, phone_number):
    cfg = fresh_cfg
    first_name = cfg.get("profile_display_name") or cfg.get("first_name") or cfg.get("profile_base_first_name") or "Qwitty"
    username = cfg.get("username")
    username_str = f"@{username}" if username else "Отсутствует"

    timezone_offset = int(cfg.get("timezone_offset", 5) or 5)
    timezone_name = userbot.TIMEZONE_NAMES.get(timezone_offset, f"UTC{timezone_offset:+d}")
    time_status = userbot.get_text(viewer_id, "status_on") if cfg.get("time_nick_active", False) else userbot.get_text(viewer_id, "status_off")
    autoresponder_status = userbot.get_text(viewer_id, "status_on") if cfg.get("autoresponder_active", False) else userbot.get_text(viewer_id, "status_off")
    antivirus_status = userbot.get_text(viewer_id, "status_on") if cfg.get("antivirus_enabled", False) else userbot.get_text(viewer_id, "status_off")
    phone_str = phone_number or "Недоступен"

    text = (
        "<b>👤 Профиль</b>\n"
        f"<b>Никнейм:</b> {guard.user_link(target_uid, first_name)}\n"
        f"<b>Юзернейм:</b> {guard.user_link(target_uid, username_str) if username and username != 'N/A' else _html(username_str)}\n"
        f"<b>Номер:</b> {_html(phone_str)}\n\n"
        "<b>⚙️ Функции</b>\n"
        f"<b>Время в профиль:</b> {_html(time_status)}\n"
        f"<i>{_html(timezone_name)}</i>\n\n"
        f"<b>Автоответчик:</b> {_html(autoresponder_status)}\n"
        f"<b>АнтиВирус:</b> {_html(antivirus_status)}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text=userbot.get_text(viewer_id, "btn_back"), callback_data="admin_users_1")
    builder.adjust(1)
    await userbot.edit_or_send(viewer_id, text, reply_markup=builder.as_markup(), parse_mode="HTML")


async def _save_restart_pending(chat_id: int, message_id: int):
    uid = str(ADMIN_ID)
    cfg = userbot.cached_config(uid)
    cfg[RESTART_PENDING_KEY] = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "requested_at": userbot.get_world_utc_datetime().isoformat(),
        "instance_id": RENDER_INSTANCE_ID or None,
    }
    MEMORY_DB["config"][uid] = cfg
    ok = await async_db_save("config", uid, cfg, max_attempts=3, background_on_fail=False)
    if not ok:
        cfg.pop(RESTART_PENDING_KEY, None)
        raise RuntimeError("Не удалось сохранить состояние перезапуска в Supabase.")


async def _clear_restart_pending():
    uid = str(ADMIN_ID)
    cfg = userbot.cached_config(uid)
    if RESTART_PENDING_KEY not in cfg:
        return
    cfg.pop(RESTART_PENDING_KEY, None)
    MEMORY_DB["config"][uid] = cfg
    await async_db_save("config", uid, cfg, max_attempts=3, background_on_fail=False)


async def _render_clean_deploy():
    if not RENDER_API_KEY:
        raise RuntimeError("Не задан RENDER_API_KEY в Environment Render.")
    if not RENDER_SERVICE_ID:
        raise RuntimeError("Render не передал RENDER_SERVICE_ID этому сервису.")

                                                                                 
                                                                                  
    url = f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/deploys"
    headers = {
        "Authorization": f"Bearer {RENDER_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload = {"clearCache": "clear"}
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            body = await response.text()
            if response.status not in (201, 202):
                body = body.strip().replace("\n", " ")[:500]
                raise RuntimeError(
                    f"Render Deploy API вернул HTTP {response.status}: {body or 'без описания'}"
                )

            deploy_id = None
            if body.strip():
                try:
                    data = json.loads(body)
                    if isinstance(data, dict):
                        deploy_id = data.get("id") or data.get("deploy", {}).get("id")
                except Exception:
                    pass

            logging.info(
                "♻️ Render clean deploy запрошен%s",
                f" (deploy_id={deploy_id})" if deploy_id else "",
            )
            return deploy_id


async def _restart_wait_animation(chat_id: int, message_id: int):
    dots = (".", "..", "...")
    index = 0
    try:
        while True:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=f"<b>♻️ Перезапуск сервера</b>\n\n<i>Подождите{dots[index]}</i>",
                    parse_mode="HTML",
                )
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 0.2)
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e).lower():
                    logging.debug("Анимация перезапуска: %s", e)
            index = (index + 1) % len(dots)
            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logging.debug("Анимация перезапуска завершена: %s", e)


def _cancel_restart_animation(user_id: int):
    task = RESTART_ANIMATION_TASKS.pop(user_id, None)
    if task and not task.done():
        task.cancel()


@dp.callback_query(F.data == "admin_restart_server")
async def admin_restart_server(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    userbot.stop_admin_server_stats_loop(callback.from_user.id)
    userbot.get_user_state(callback.from_user.id)["state"] = "ADMIN_RESTART_CONFIRM"
    await userbot.edit_or_send(
        callback.from_user.id,
        "<b>♻️ Перезапуск сервера</b>\n\n<i>Вы уверены, что хотите перезапустить сервер?</i>",
        reply_markup=build_restart_confirm_markup(),
        parse_mode="HTML",
    )
    try:
        await callback.answer()
    except Exception:
        pass


@dp.callback_query(F.data == "admin_restart_confirm")
async def admin_restart_confirm(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return

    user_id = callback.from_user.id
    userbot.stop_admin_server_stats_loop(user_id)
    data = userbot.get_user_state(user_id)
    data["state"] = "ADMIN_RESTARTING"

    try:
        await callback.answer()
    except Exception:
        pass

    await userbot.edit_or_send(user_id, "<b>♻️ Перезапуск сервера</b>\n\n<i>Подождите…</i>", reply_markup=None, parse_mode="HTML")
    message_id = userbot.get_user_state(user_id).get("msg_id")
    if not message_id:
        await open_admin_menu_for(user_id)
        return

    try:
        await _save_restart_pending(user_id, message_id)
    except Exception as e:
        builder = InlineKeyboardBuilder()
        builder.button(text="Назад", callback_data="admin_menu")
        await userbot.edit_or_send(user_id, f"Не удалось подготовить перезапуск.\n\n{e}", reply_markup=builder.as_markup())
        return

    _cancel_restart_animation(user_id)
    RESTART_ANIMATION_TASKS[user_id] = asyncio.create_task(_restart_wait_animation(user_id, message_id))

    try:
        await _render_clean_deploy()
                                                                         
                                                                        
        _start_restart_finalizer()
    except Exception as e:
        _cancel_restart_animation(user_id)
        await _clear_restart_pending()
        builder = InlineKeyboardBuilder()
        builder.button(text="Назад", callback_data="admin_menu")
        await userbot.edit_or_send(
            user_id,
            f"Не удалось перезапустить сервер.\n\n{e}",
            reply_markup=builder.as_markup(),
        )


@dp.callback_query(F.data == "admin_restart_ok")
async def admin_restart_ok(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    await open_admin_menu_for(callback.from_user.id)
    try:
        await callback.answer()
    except Exception:
        pass


async def finalize_pending_server_restart():
    """
    Завершает экран перезапуска через фиксированные ~45 секунд от момента запроса.

    Важно: задача может стартовать как в старом процессе сразу после запроса deploy,
    так и в новом процессе после запуска. В обоих случаях requested_at из Supabase
    позволяет досчитать только оставшееся время и не оставлять "Подождите." навсегда.
    """
    uid = str(ADMIN_ID)
    cfg = MEMORY_DB["config"].get(uid) or {}
    pending = cfg.get(RESTART_PENDING_KEY)
    if not isinstance(pending, dict):
        return

    try:
        chat_id = int(pending.get("chat_id") or ADMIN_ID)
        message_id = int(pending.get("message_id"))
    except (TypeError, ValueError):
        await _clear_restart_pending()
        return

    remaining = float(RESTART_SUCCESS_AFTER_SECONDS)
    requested_at = pending.get("requested_at")
    if requested_at:
        try:
            requested_dt = userbot.datetime.datetime.fromisoformat(str(requested_at))
            if requested_dt.tzinfo is None:
                requested_dt = requested_dt.replace(tzinfo=userbot.datetime.timezone.utc)
            elapsed = (userbot.get_world_utc_datetime() - requested_dt).total_seconds()
            remaining = max(0.0, float(RESTART_SUCCESS_AFTER_SECONDS) - elapsed)
        except Exception:
            logging.debug("Не удалось вычислить оставшееся время рестарта", exc_info=True)

    if remaining > 0:
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            raise

                                                                                     
    _cancel_restart_animation(ADMIN_ID)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="<b>✅ Сервер успешно перезапущен</b>",
            parse_mode="HTML",
            reply_markup=build_restart_done_markup(),
        )
        state = userbot.get_user_state(ADMIN_ID)
        state["msg_id"] = message_id
        state["state"] = "ADMIN_RESTART_DONE"
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("Не удалось показать результат перезапуска: %s", e)
    except Exception as e:
        logging.warning("Не удалось завершить экран перезапуска: %s", e)
    finally:
        await _clear_restart_pending()


def _start_restart_finalizer():
    task = asyncio.create_task(finalize_pending_server_restart())
    RESTART_FINALIZE_TASKS.add(task)
    task.add_done_callback(RESTART_FINALIZE_TASKS.discard)
    return task


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
        if force or time.monotonic() - SERVER_STATS_CACHE["updated_at"] >= 300 or not SERVER_STATS_CACHE["updated_at"]:
            value, source = await _get_supabase_db_mb()
            SERVER_STATS_CACHE.update(supabase_db_mb=value, supabase_source=source, updated_at=time.monotonic())
    return SERVER_STATS_CACHE


def _build_server_stats_text(cache):
    now = datetime.datetime.now(datetime.timezone.utc)
    reset = _next_render_reset_utc(now)
    size = cache.get("supabase_db_mb")
    db_line = (
        f"<b>База:</b> {size:.1f} / {SUPABASE_DB_LIMIT_MB:g} MB"
        if size is not None else "<b>База:</b> данные недоступны"
    )
    return (
        "<b>🖥 Статистика сервера</b>\n\n"
        "<b>🟣 Render</b>\n"
        f"<b>Сброс лимита:</b> {reset.strftime('%d.%m.%Y %H:%M UTC')}\n"
        f"<b>До сброса:</b> {format_remaining_time((reset - now).total_seconds())}\n"
        f"<b>Аптайм процесса:</b> {format_remaining_time(time.monotonic() - PROCESS_STARTED_AT)}\n\n"
        "<b>🟢 Supabase</b>\n"
        + db_line
    )


def build_admin_stats_markup():
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_server_stats_back")
    builder.adjust(1)
    return builder.as_markup()


async def _admin_server_stats_loop(user_id):
    data = userbot.get_user_state(user_id)
    message_id = data.get("msg_id")
    next_tick = time.monotonic()
    while data.get("admin_stats_active"):
        next_tick += 1
        await asyncio.sleep(max(0, next_tick - time.monotonic()))
        try:
            async with data.setdefault("ui_lock", asyncio.Lock()):
                if not data.get("admin_stats_active") or data.get("msg_id") != message_id or data.get("state") != "ADMIN_STATS":
                    return
                await bot.edit_message_text(chat_id=user_id, message_id=message_id,
                    text=_build_server_stats_text(SERVER_STATS_CACHE), reply_markup=build_admin_stats_markup(),
                    parse_mode="HTML")
            if time.monotonic() - SERVER_STATS_CACHE["updated_at"] >= 300:
                await refresh_server_stats_cache()
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Статистика сервера")
            await asyncio.sleep(3)
        next_tick = max(next_tick, time.monotonic())


def start_live_profile(viewer_id, refresh, enabled):
    state = userbot.get_user_state(viewer_id)
    previous = state.get("admin_live_task")
    if previous and not previous.done():
        previous.cancel()
    message_id = state.get("msg_id")
    async def run():
        while True:
            await userbot.sleep_until_next_world_minute()
            await asyncio.sleep(2)                                           
            async with state.setdefault("ui_lock", asyncio.Lock()):
                if state.get("msg_id") != message_id:
                    return
                if enabled():
                    try:
                        await refresh()
                    except TelegramRetryAfter as exc:
                        await asyncio.sleep(exc.retry_after)
                    except Exception:
                        logging.exception("Не удалось обновить открытый профиль")
    state["admin_live_task"] = asyncio.create_task(run())
