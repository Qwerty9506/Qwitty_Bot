import asyncio
import json
import logging
import os
import sys

import aiohttp
from aiohttp import web

from aiogram import F, types
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from pyrogram.raw import functions

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


import userbot
import guard

from userbot import (
    DB_TASKS,
    MEMORY_DB,
    USER_DATA,
    SAVED,
    start_saved_service,
    stop_saved_service,
    async_db_save,
    bot,
    close_pyrogram_client,
    db_retry_loop,
    dp,
    ntp_sync_loop,
    restore_saved_sessions,
    session_recovery_loop,
    supabase,
    sync_world_clock,
)

ADMIN_ID = userbot.ADMIN_ID
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
    builder.button(text="Активнные🟢", callback_data="admin_users_1")
    builder.button(text="Не-входящие🔴", callback_data="admin_entries_1")
    builder.button(text="Перезапуск сервера♻️", callback_data="admin_restart_server")
    builder.button(text="Назад в главное меню 🏠", callback_data="root_menu")
    builder.adjust(1)
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
    await userbot.edit_or_send(user_id, "Админ меню:", reply_markup=build_admin_menu_markup())


@dp.message(F.text.casefold() == "admin")
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
        await callback.answer("Обновляю…")
    except TelegramBadRequest:
        pass
    cache = await userbot.refresh_server_stats_cache(force=True)
    if data.get("state") != "ADMIN_STATS":
        return
    await userbot.edit_or_send(
        user_id,
        userbot._build_server_stats_text(cache),
        reply_markup=userbot.build_admin_stats_markup(),
    )
    data["admin_stats_active"] = True
    data["admin_stats_task"] = asyncio.create_task(userbot._admin_server_stats_loop(user_id))


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

    entries = []
    for uid, cfg in MEMORY_DB["config"].items():
        if cfg.get("logged_in", False):
            continue
        if not cfg.get("last_entry_at") and not cfg.get("last_entry_ts"):
            continue
        entries.append((uid, cfg))

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

    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_entries_{page-1}"))
    nav_buttons.append(types.InlineKeyboardButton(text=f"📖 {page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        nav_buttons.append(types.InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_entries_{page+1}"))
    builder.row(*nav_buttons)
    builder.button(text="⬅️ В админ меню", callback_data="admin_users_back")

    await userbot.edit_or_send(
        callback.from_user.id,
        f"Список не-входящих ({total_entries}):",
        reply_markup=builder.as_markup(),
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
    phone = cfg.get("phone") or cfg.get("entry_phone") or "Не виден"
    entry_time = userbot.entry_time_text(cfg)

    text = (
        f"Никнейм: {first_name}\n"
        f"Юзернейм: {username_str}\n"
        f"Номер: {phone}\n"
        f"Последний вход: {entry_time}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data="admin_entries_1")
    builder.adjust(1)
    await userbot.edit_or_send(callback.from_user.id, text, reply_markup=builder.as_markup())
    try:
        await callback.answer()
    except Exception:
        pass


async def admin_validate_session(user_id, cfg, semaphore=None):
    if semaphore is not None:
        async with semaphore:
            return await _admin_validate_session(user_id, cfg)
    return await _admin_validate_session(user_id, cfg)


async def _admin_validate_session(user_id, cfg):
    if not cfg.get("logged_in"):
        return False
    await userbot.ensure_client_connected(user_id)
    return bool(userbot.cached_config(user_id).get("logged_in"))


@dp.callback_query(F.data.startswith("admin_users_"))
async def admin_users_list(callback: types.CallbackQuery):
    if not userbot.is_admin(callback.from_user):
        return
    userbot.stop_admin_server_stats_loop(callback.from_user.id)

    try:
        page = int(callback.data.split("_")[-1])
    except (ValueError, TypeError):
        page = 1

    all_configs = list(MEMORY_DB["config"].items())
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
            logging.warning("Ошибка проверки активности %s: %s", uid, result)

    active_configs.sort(key=lambda item: int(item[1].get("logged_in", False)), reverse=True)

    per_page = 5
    total_users = len(active_configs)
    total_pages = max(1, (total_users + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    current_page_users = active_configs[(page - 1) * per_page:page * per_page]

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

    await userbot.edit_or_send(callback.from_user.id, "Активные пользователи:", reply_markup=builder.as_markup())
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

    cfg = userbot.cached_config(target_uid)
    first_name = cfg.get("first_name") or cfg.get("profile_base_first_name") or "Qwitty"
    username = cfg.get("username", "N/A")
    username_str = f"@{username}" if username != "N/A" else "Отсутствует"
    phone = cfg.get("phone", "Не указан")

    devices_str = "Неизвестно"
    target_state = userbot.get_user_state(int(target_uid))
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
            devices_str = ", ".join(device_names) if device_names else "Не найдено"
        except Exception as e:
            logging.error("Ошибка получения устройств: %s", e)
            devices_str = "Ошибка получения"

    timezone_offset = int(cfg.get("timezone_offset", 5) or 5)
    timezone_name = userbot.TIMEZONE_NAMES.get(timezone_offset, f"UTC{timezone_offset:+d}")
    time_status = userbot.get_text(callback.from_user.id, "status_on") if cfg.get("time_nick_active", False) else userbot.get_text(callback.from_user.id, "status_off")
    autoresponder_status = userbot.get_text(callback.from_user.id, "status_on") if cfg.get("autoresponder_active", False) else userbot.get_text(callback.from_user.id, "status_off")
    online_247_status = userbot.get_text(callback.from_user.id, "status_on") if cfg.get("online_247", False) else userbot.get_text(callback.from_user.id, "status_off")
    auto_read_status = userbot.get_text(callback.from_user.id, "status_on") if cfg.get("auto_read", False) else userbot.get_text(callback.from_user.id, "status_off")
    autoresponder_greeting = cfg.get("autoresponder_greeting", userbot.get_text(int(target_uid), "msg_autoresp_default"))

    text = (
        f"Никнейм: {first_name}\n"
        f"Юзернейм: {username_str}\n"
        f"Номер: {phone}\n"
        f"Устройства: {devices_str}\n\n"
        f"Время в профиль: {time_status}\n"
        f"{timezone_name}\n\n"
        f"Автоответчик: {autoresponder_status}\n"
        f"{autoresponder_greeting}\n\n"
        f"Вечный онлайн: {online_247_status}\n\n"
        f"Автопрочтение: {auto_read_status}"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text=userbot.get_text(callback.from_user.id, "btn_back"), callback_data="admin_users_1")
    builder.adjust(1)
    await userbot.edit_or_send(callback.from_user.id, text, reply_markup=builder.as_markup())
    try:
        await callback.answer()
    except Exception:
        pass


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

    # Это именно аналог Dashboard -> Manual Deploy -> Clear build cache & deploy.
    # /restart лишь перезапускает уже существующий deploy и build-cache не чистит.
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
                    text=f"Подождите{dots[index]}",
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
        "Вы уверены что хотите перезапустить сервер?",
        reply_markup=build_restart_confirm_markup(),
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

    await userbot.edit_or_send(user_id, "Подождите.", reply_markup=None)
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
        # Фолбэк в текущем процессе. Если Render успеет убить его раньше,
        # новый процесс подхватит тот же pending из Supabase при старте.
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

    # Если старый процесс всё ещё жив, не даём анимации перезаписать финальный текст.
    _cancel_restart_animation(ADMIN_ID)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text="Сервер успешно перезапущен✅",
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
    return runner


async def main():
    if not supabase:
        raise RuntimeError(
            "Для сохранения настроек задайте рабочие SUPABASE_URL и SUPABASE_KEY."
        )

    web_runner = await start_web_server()

    await sync_world_clock(force=True)
    ntp_task = asyncio.create_task(ntp_sync_loop())
    db_task = asyncio.create_task(db_retry_loop())

    await SAVED.open()
    start_saved_service()
    await restore_saved_sessions()
    recovery_task = asyncio.create_task(session_recovery_loop())

    # Не блокируем старт polling на 45 секунд: финализатор работает отдельно.
    _start_restart_finalizer()
    logging.info("🚀 Бот успешно запущен!")

    try:
        await dp.start_polling(bot)
    finally:
        SAVED.closing = True
        recovery_task.cancel()
        await asyncio.gather(recovery_task, return_exceptions=True)
        tasks = []

        for data in USER_DATA.values():
            for key in (
                "session_repair_task",
                "saved_history_task",
                "online_task",
                "time_nick_task",
                "ui_refresh_task",
                "admin_stats_task",
                "auto_read_offline_task",
            ):
                task = data.get(key)
                if task and not task.done():
                    task.cancel()
                    tasks.append(task)

        for task in list(RESTART_ANIMATION_TASKS.values()):
            if task and not task.done():
                task.cancel()
                tasks.append(task)
        RESTART_ANIMATION_TASKS.clear()

        await asyncio.gather(*tasks, return_exceptions=True)

        for task in (recovery_task, db_task, ntp_task):
            task.cancel()
        await asyncio.gather(recovery_task, db_task, ntp_task, return_exceptions=True)

        restart_finalize_tasks = list(RESTART_FINALIZE_TASKS)
        for task in restart_finalize_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*restart_finalize_tasks, return_exceptions=True)

        for data in USER_DATA.values():
            if data.get("client"):
                await close_pyrogram_client(data["client"])
        await stop_saved_service()

        pending_db_tasks = list(DB_TASKS)
        for task in pending_db_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending_db_tasks, return_exceptions=True)

        for uid, cfg in MEMORY_DB["config"].items():
            await async_db_save(
                "config", uid, cfg, max_attempts=3, background_on_fail=False
            )

        await bot.session.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    loop.run_until_complete(main())
