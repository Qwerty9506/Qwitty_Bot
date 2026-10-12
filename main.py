"""Run python main.py. All imports, keys and database connections live here."""
import sys
import importlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import psutil
import aiohttp
from aiohttp import web
if __name__ == "__main__":
    sys.modules["main"] = sys.modules[__name__]
import asyncio
import logging
import os



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


import copy
import io
from contextvars import ContextVar
import struct
import binascii
import hashlib
import html
import json
import sqlite3
import threading
import uuid
import zlib
import datetime
import time
import glob
import re
import random
import unicodedata
import ntplib
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from pyrogram import Client, enums, filters
from pyrogram.handlers import MessageHandler, EditedMessageHandler, RawUpdateHandler
from pyrogram.raw import functions, types as raw_types
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, Unauthorized, FloodWait, UserNotParticipant, ChannelPrivate

from supabase import create_client

from aiogram.exceptions import TelegramForbiddenError
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

SUPABASE_DB_SIZE_RPC = os.getenv("SUPABASE_DB_SIZE_RPC", "get_database_size_bytes")


ADMIN_ID = 8845929618
ADMIN_USERNAME = "Qwitty_Cc"

logging.basicConfig(level=logging.INFO)
logging.getLogger("aiogram").setLevel(logging.WARNING)
logging.getLogger("pyrogram").setLevel(logging.WARNING)


SUPABASE_URL_2 = os.getenv("SUPABASE_URL_2", "").strip().rstrip("/")
SUPABASE_KEY_2 = os.getenv("SUPABASE_KEY_2", "").strip()
SUPABASE_DB_LIMIT_MB = 1000.0
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
RENDER_INSTANCE_ID = os.getenv("RENDER_INSTANCE_ID", "").strip()
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "sessions")
CONFIG_OUTBOX_SQLITE = os.getenv("CONFIG_OUTBOX_SQLITE", os.path.join(SESSIONS_DIR, "config_outbox.sqlite3"))
GUARD_SQLITE = os.getenv("GUARD_SQLITE", os.path.join(SESSIONS_DIR, "guard.sqlite3"))
SAVED_MESSAGES_SQLITE = os.getenv("SAVED_MESSAGES_SQLITE", os.path.join(SESSIONS_DIR, "saved_messages.sqlite3"))
DB_WRITE_SEMAPHORE = asyncio.Semaphore(max(1, int(os.getenv("SUPABASE_MAX_PARALLEL_WRITES", "4") or 4)))
DB_CONFIG_SAVE_DEBOUNCE_SECONDS = max(0.05, float(os.getenv("SUPABASE_CONFIG_DEBOUNCE", "0.20") or 0.20))
DB_LOG_SAVE_DEBOUNCE_SECONDS = max(0.20, float(os.getenv("SUPABASE_LOG_DEBOUNCE", "0.80") or 0.80))


class SupabasePool:
    """Each account/group belongs to one database; failed writes retry there."""
    def __init__(self, clients):
        if len(clients) != 2:
            raise ValueError("Нужны две отдельные Supabase.")
        self.clients = tuple(clients)
        self.routes = {}
        self.counts = [[0, 0], [0, 0]]
        self.lock = threading.RLock()
        self.ready = False

    @staticmethod
    def uid(value):
        return str(int(value))

    @staticmethod
    def _read_configs(client):
        rows, offset = [], 0
        while True:
            batch = client.table("config").select("id,data").order("id").range(offset, offset + 199).execute().data or []
            rows.extend(batch)
            if len(batch) < 200:
                return rows
            offset += 200

    def _scan(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self._read_configs, client) for client in self.clients]
            batches = [future.result() for future in futures]
        found, duplicates = {}, []
        for slot, rows in enumerate(batches):
            for row in rows:
                uid = self.uid(row["id"])
                if uid in found:
                    previous_slot, previous = found[uid]
                    if previous.get("data") != row.get("data"):
                        raise RuntimeError(f"ID {uid} уже есть в двух базах с разными данными. Записи не изменены.")
                    duplicates.append((slot, uid, previous_slot, previous.get("data")))
                else:
                    found[uid] = (slot, row)
        # Only exact pre-existing duplicates can be removed without losing data.
        for slot, uid, owner, expected in duplicates:
            canonical = self.clients[owner].table("config").select("data").eq("id", uid).execute().data or []
            if not canonical or canonical[0].get("data") != expected:
                raise RuntimeError("Данные изменились при проверке дубликатов; повторите запуск.")
            self.clients[slot].table("config").delete().eq("id", uid).execute()
        return found

    def initialize(self):
        with self.lock:
            if self.ready:
                return
            found = self._scan()
            for uid, (slot, _) in found.items():
                self.routes[uid] = slot
                self.counts[slot][0 if int(uid) > 0 else 1] += 1
            self.ready = True
            logging.info("Supabase 1: %d аккаунтов; Supabase 2: %d аккаунтов. Данные не копируются.",
                         self.counts[0][0], self.counts[1][0])

    def owner(self, uid):
        uid = self.uid(uid)
        with self.lock:
            if not self.ready:
                self.initialize()
            if uid not in self.routes:
                kind = 0 if int(uid) > 0 else 1
                slot = min(range(2), key=lambda n: (self.counts[n][kind], n))
                self.routes[uid] = slot
                self.counts[slot][kind] += 1
            return self.clients[self.routes[uid]]

    def get_data(self, table, uid):
        uid = self.uid(uid)
        rows = self.owner(uid).table(table).select("data").eq("id", uid).execute().data or []
        return rows[0].get("data", {}) if rows else {}

    def save_data(self, table, uid, data):
        uid = self.uid(uid)
        payload = {"id": uid, "data": copy.deepcopy(data)}
        query = self.owner(uid).table(table)
        try:
            query.upsert(payload, on_conflict="id").execute()
        except TypeError:
            query.upsert(payload).execute()

    def delete_data(self, table, uid):
        uid = self.uid(uid)
        self.owner(uid).table(table).delete().eq("id", uid).execute()

    def list_configs(self, groups=False):
        self.initialize()
        found = self._scan()
        with self.lock:
            for uid, (slot, _) in found.items():
                pinned = self.routes.get(uid)
                if pinned is not None and pinned != slot:
                    raise RuntimeError("Аккаунт обнаружен вне закреплённой базы; записи не изменены.")
                if pinned is None:
                    self.routes[uid] = slot
                    self.counts[slot][0 if int(uid) > 0 else 1] += 1
        return [{"id": uid, "data": copy.deepcopy(row.get("data") or {})}
                for uid, (_, row) in found.items() if (int(uid) < 0 if groups else int(uid) > 0)]


DATABASES = None
supabase = None
supabase_2 = None
bot = None
dp = None
userbot = None
guard = None
admin = None


def get_guard():
    return guard


def initialize_runtime():
    global DATABASES, supabase, supabase_2, bot, dp, userbot, guard, admin
    required = {"BOT_TOKEN": BOT_TOKEN, "API_ID": API_ID, "API_HASH": API_HASH,
                "SUPABASE_URL": SUPABASE_URL, "SUPABASE_KEY": SUPABASE_KEY,
                "SUPABASE_URL_2": SUPABASE_URL_2, "SUPABASE_KEY_2": SUPABASE_KEY_2}
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError("Заполните Environment Render: " + ", ".join(missing))
    if SUPABASE_URL.rstrip("/") == SUPABASE_URL_2:
        raise RuntimeError("SUPABASE_URL_2 должен указывать на отдельный второй проект.")
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    supabase_2 = create_client(SUPABASE_URL_2, SUPABASE_KEY_2)
    DATABASES = SupabasePool((supabase, supabase_2))
    os.makedirs(SESSIONS_DIR, mode=0o700, exist_ok=True)
    os.chmod(SESSIONS_DIR, 0o700)
    bot, dp = Bot(token=BOT_TOKEN), Dispatcher()
    userbot = importlib.import_module("userbot")
    guard = importlib.import_module("guard")
    admin = importlib.import_module("admin")
    for name in ("DB_TASKS", "MEMORY_DB", "USER_DATA", "SAVED", "start_saved_service",
                 "stop_saved_service", "async_db_save", "close_pyrogram_client", "db_retry_loop",
                 "ntp_sync_loop", "restore_saved_sessions", "session_recovery_loop", "sync_world_clock"):
        globals()[name] = getattr(userbot, name)


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
    initialize_runtime()
    if not DATABASES:
        raise RuntimeError(
            "Для сохранения настроек задайте рабочие SUPABASE_URL и SUPABASE_KEY."
        )

    web_runner = await start_web_server()
    await asyncio.to_thread(DATABASES.initialize)

    await sync_world_clock(force=True)
    ntp_task = asyncio.create_task(ntp_sync_loop())
    db_task = asyncio.create_task(db_retry_loop())

    await SAVED.open()
    start_saved_service()
    await restore_saved_sessions()
    await guard.start_guard()
    recovery_task = asyncio.create_task(session_recovery_loop())

                                                                             
    admin._start_restart_finalizer()
    logging.info("🚀 Бот успешно запущен!")

    try:
        allowed_updates = sorted(set(dp.resolve_used_update_types()) | {
            "message", "edited_message", "my_chat_member", "chat_member",
        })
        await dp.start_polling(bot, allowed_updates=allowed_updates)
    finally:
        await guard.stop_guard()
        SAVED.closing = True
        recovery_task.cancel()
        await asyncio.gather(recovery_task, return_exceptions=True)
        tasks = []

        for data in USER_DATA.values():
            for key in (
                "session_repair_task",
                "saved_history_task",
                "antivirus_history_task",
                "time_nick_task",
                "ui_refresh_task",
                "admin_stats_task",
                "admin_live_task",
            ):
                task = data.get(key)
                if task and not task.done():
                    task.cancel()
                    tasks.append(task)

        for task in list(admin.RESTART_ANIMATION_TASKS.values()):
            if task and not task.done():
                task.cancel()
                tasks.append(task)
        admin.RESTART_ANIMATION_TASKS.clear()

        await asyncio.gather(*tasks, return_exceptions=True)

        for task in (recovery_task, db_task, ntp_task):
            task.cancel()
        await asyncio.gather(recovery_task, db_task, ntp_task, return_exceptions=True)

        restart_finalize_tasks = list(admin.RESTART_FINALIZE_TASKS)
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
