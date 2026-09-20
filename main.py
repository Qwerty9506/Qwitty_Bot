import asyncio
import logging
import os
import sys

from aiohttp import web

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

        await asyncio.gather(*tasks, return_exceptions=True)

        for task in (recovery_task, db_task, ntp_task):
            task.cancel()
        await asyncio.gather(recovery_task, db_task, ntp_task, return_exceptions=True)

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
