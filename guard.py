"""Group Guard for Qwitty. One running polling instance per bot token.

Group state is committed to SQLite first. Small snapshots are coalesced into
negative-ID rows of the existing Supabase config(id, data) table. Private
account configuration keeps positive IDs and is not used for group evidence.
"""
import asyncio
import copy
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from collections import OrderedDict

from aiogram import F, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.utils.keyboard import InlineKeyboardBuilder
import userbot as ub

bot, dp = ub.bot, ub.dp
FORMAT = "qwitty.guard.v1"
GLOBAL_FORMAT = "qwitty.guard.global.v1"
GLOBAL_STATE_ID = -9223372036854775000
GLOBAL_META_KEY = "global_bans"
GROUP_TYPES = {"group", "supergroup"}
ADMIN_STATUSES = {"creator", "administrator"}
PRESENT_STATUSES = {"creator", "administrator", "member", "restricted"}
PAGE_SIZE = 5
REPORT_TTL = 30 * 86400
MEMBER_TTL = 7 * 86400
SPAM_WINDOW = 1.0
MAX_TARGETS = 500
MAX_GROUP_BYTES = 1024 * 1024
DANGEROUS_EXTENSIONS = frozenset("""
.exe .com .scr .msi .msix .msp .cpl .pif .gadget .application .appref-ms
.bat .cmd .ps1 .ps1xml .psc1 .psd1 .psm1 .vbs .vbe .wsf .wsh .hta .js .jse
.lnk .scf .apk .xapk .apks .apkm .aab .jar .jnlp .sh .bash .zsh .run .bin
.app .appimage .command .pkg .dmg .reg .inf .ins .isp .py .pyw .rb .pl .cgi
""".split())
MODES = {
    "spam": ("🚯АнтиСпам", "Удаляет второе и последующие сообщения одного пользователя, если между сообщениями прошло не больше 1 секунды. Работает и для администраторов."),
    "files": ("⚠️АнтиВирус", "Удаляет потенциально опасные исполняемые файлы и скрипты по расширению. Это фильтр файлов, а не проверка содержимого антивирусом."),
    "raid": ("📛 АнтиНакрутка", "Защищает от массовых входов. При 50 и более новых участниках за 60 секунд по окончании этой минуты блокирует всех участников волны."),
    "service": ("🧹Очистка вх/изм", "Убирает уведомления о входе и выходе участников, закреплении сообщений, изменении названия и фотографии группы."),
}
SERVICE_FIELDS = ("new_chat_members", "left_chat_member", "pinned_message",
                  "new_chat_title", "new_chat_photo", "delete_chat_photo")
BOT_NAME = None
TASKS = set()
WELCOME_TASKS = {}
GROUP_LOCKS = {}
MEMBER_PENDING = OrderedDict()
ADMIN_CACHE = {}
SPAM_LAST = OrderedDict()
GLOBAL_USER_LOCKS = {}
GLOBAL_UNBAN_RUNNING = set()
GLOBAL_ENFORCEMENT_LOCKS = {}
GLOBAL_BAN_PENDING = {}
GLOBAL_BAN_RUNNING = set()
KICK_EVENTS = {}
REPORT_RATE = OrderedDict()
REPORT_QUEUE = asyncio.Queue(maxsize=256)
REPORT_QUEUED = set()


def esc(value):
    return html.escape(str(value or ""), quote=False)


def clip(value, n=80):
    return ub.saved_clip(value, n)


def user_link(uid, name):
    return f'<a href="tg://user?id={int(uid)}">{esc(clip(name, 100))}</a>'


def lock_for(gid):
    return GROUP_LOCKS.setdefault(int(gid), asyncio.Lock())


def spawn(coro):
    task = asyncio.create_task(coro)
    TASKS.add(task)
    def done(t):
        TASKS.discard(t)
        if not t.cancelled() and t.exception():
            logging.warning("Guard background task: %s", type(t.exception()).__name__)
    task.add_done_callback(done)
    return task


def fresh_group(gid, title):
    return {"format": FORMAT, "id": int(gid), "title": clip(title or str(gid), 128),
            "active": True, "added_by": None, "member_count": None,
            "settings": {"spam": False, "files": False, "raid": False, "service": False},
            "admins": {}, "hidden": [], "seen": {}, "reports": {}, "local_bans": {}, "dedup": {},
            "sequence": 0, "welcome_pending": True, "joined_at": time.time(),
            "revision": 0}


class GuardStore:
    def __init__(self, path=None):
        self.path = path or os.getenv("GUARD_SQLITE", os.path.join(ub.SESSIONS_DIR, "guard.sqlite3"))
        self.db = None
        self.groups = {}
        self.dirty = set()
        self.due = {}
        self.first_dirty = {}
        self.lock = asyncio.Lock()
        self.ready = False
        self.errors = set()
        self.global_bans = {}
        self.global_dirty = False
        self.global_due = 0.0
        self.global_revision = 0
        self.remote_lock = asyncio.Lock()
        self.global_lock = asyncio.Lock()

    async def sql(self, fn, *args):
        async with self.lock:
            task = asyncio.create_task(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    def _open(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        os.chmod(self.path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA cache_size=-1024")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS groups_state(
                gid INTEGER PRIMARY KEY, payload TEXT NOT NULL, dirty INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS members(
                gid INTEGER, uid INTEGER, username TEXT, name TEXT, seen REAL,
                PRIMARY KEY(gid,uid));
            CREATE INDEX IF NOT EXISTS members_name ON members(gid,username);
            CREATE TABLE IF NOT EXISTS guard_meta(
                key TEXT PRIMARY KEY, payload TEXT NOT NULL, dirty INTEGER NOT NULL);
        ''')
        self.db.commit()
        groups = self.db.execute("SELECT gid,payload,dirty FROM groups_state").fetchall()
        meta = self.db.execute("SELECT key,payload,dirty FROM guard_meta").fetchall()
        return groups, meta

    @staticmethod
    def _remote_groups():
        rows, offset = [], 0
        while True:
            result = (ub.supabase.table("config").select("id,data").lt("id", "0")
                      .order("id").range(offset, offset + 99).execute())
            batch = result.data or []
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            offset += 100

    async def open(self):
        local_groups, local_meta = await self.sql(self._open)
                                                                                          
        remote = await asyncio.to_thread(self._remote_groups)
        remote_global = None
        for row in remote:
            payload = row.get("data") or {}
            rid = int(row["id"])
            if rid == GLOBAL_STATE_ID and isinstance(payload, dict) and payload.get("format") == GLOBAL_FORMAT:
                remote_global = payload
                continue
            if isinstance(payload, dict) and payload.get("format") == FORMAT:
                if rid < 0 and int(payload.get("id", 0)) == rid:
                    self.groups[rid] = payload
        for gid, payload, dirty in local_groups:
            group = json.loads(payload)
            if dirty or gid not in self.groups:
                self.groups[gid] = group
            if dirty:
                self.dirty.add(gid)
                self.due[gid] = time.monotonic()

        local_global = None
        local_global_dirty = False
        for key, payload, dirty in local_meta:
            if key != GLOBAL_META_KEY:
                continue
            try:
                local_global = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                local_global = None
            local_global_dirty = bool(dirty)
        if local_global_dirty and isinstance(local_global, dict):
            self.global_bans = local_global.get("bans", {}) if local_global.get("format") == GLOBAL_FORMAT else {}
            self.global_dirty = True
            self.global_due = time.monotonic()
        elif isinstance(remote_global, dict):
            self.global_bans = remote_global.get("bans", {}) if isinstance(remote_global.get("bans"), dict) else {}
        elif isinstance(local_global, dict) and local_global.get("format") == GLOBAL_FORMAT:
            self.global_bans = local_global.get("bans", {}) if isinstance(local_global.get("bans"), dict) else {}

        chosen_global = local_global if local_global_dirty and isinstance(local_global, dict) else (remote_global or local_global or {})
        self.global_revision = int(chosen_global.get("revision", 0) or 0)
        for gid, group in self.groups.items():
            await self.sql(self._write, gid, json.dumps(group, ensure_ascii=False), int(gid in self.dirty))
        await self.sql(self._write_meta, GLOBAL_META_KEY, json.dumps(self._global_payload(), ensure_ascii=False), int(self.global_dirty))
        self.ready = True

    def _write(self, gid, payload, dirty):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO groups_state VALUES(?,?,?)", (gid, payload, dirty))

    def _write_meta(self, key, payload, dirty):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO guard_meta VALUES(?,?,?)", (key, payload, dirty))

    def _global_payload(self):
        return {"format": GLOBAL_FORMAT, "id": GLOBAL_STATE_ID, "bans": copy.deepcopy(self.global_bans), "revision": self.global_revision, "updated": time.time()}

    async def save_global_bans(self, urgent=False):
        self.global_revision += 1
        payload = self._global_payload()
        await self.sql(self._write_meta, GLOBAL_META_KEY, json.dumps(payload, ensure_ascii=False), 1)
        self.global_dirty = True
        self.global_due = time.monotonic()
        await self.flush_global_bans()

    def _mark_global_clean(self, revision):
        with self.db:
            self.db.execute("UPDATE guard_meta SET dirty=0 WHERE key=? AND json_extract(payload, '$.revision')=?", (GLOBAL_META_KEY, revision))

    async def flush_global_bans(self):
        async with self.global_lock:
            if not self.global_dirty:
                return True
            snapshot = self._global_payload()
            async with ub.DB_WRITE_SEMAPHORE:
                request = asyncio.create_task(asyncio.to_thread(ub.db_save_data, "config", str(GLOBAL_STATE_ID), snapshot))
                try:
                    ok = await asyncio.shield(request)
                except asyncio.CancelledError:
                    await request
                    raise
            if ok:
                await self.sql(self._mark_global_clean, snapshot["revision"])
                if snapshot["revision"] == self.global_revision and snapshot["bans"] == self.global_bans:
                    self.global_dirty = False
                else:
                    self.global_due = time.monotonic()
            else:
                self.global_due = time.monotonic() + 10
            return ok

    async def save(self, group, urgent=False, defer_remote=False):
        group["revision"] = int(group.get("revision", 0)) + 1
        encoded = json.dumps(group, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > MAX_GROUP_BYTES:
            raise ValueError("Достигнут лимит данных группы")
        gid = group["id"]
        await self.sql(self._write, gid, encoded, 1)
        self.groups[gid] = group
        self.dirty.add(gid)
        self.first_dirty.setdefault(gid, time.monotonic())
        self.due[gid] = min(self.first_dirty[gid] + 5, time.monotonic() + 2) if defer_remote else time.monotonic()
        if not defer_remote:
            await self.flush(gid)

    def _mark_clean(self, gid, revision):
        with self.db:
            self.db.execute("UPDATE groups_state SET dirty=0 WHERE gid=? AND json_extract(payload, '$.revision')=?", (gid, revision))

    async def flush(self, gid):
                                                                         
        async with self.remote_lock:
            group = self.groups.get(gid)
            if group is None or gid not in self.dirty:
                return True
            snapshot = copy.deepcopy(group)
            async with ub.DB_WRITE_SEMAPHORE:
                request = asyncio.create_task(asyncio.to_thread(ub.db_save_data, "config", str(gid), snapshot))
                try:
                    ok = await asyncio.shield(request)
                except asyncio.CancelledError:
                    await request
                    raise
            if ok:
                self.errors.discard(gid)
                await self.sql(self._mark_clean, gid, snapshot["revision"])
                if group["revision"] == snapshot["revision"]:
                    self.dirty.discard(gid)
                    self.first_dirty.pop(gid, None)
            else:
                self.errors.add(gid)
                self.due[gid] = time.monotonic() + 10
            return ok

    def _write_members(self, rows):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO members VALUES(?,?,?,?,?)", rows)

    async def flush_members(self):
        rows = list(MEMBER_PENDING.values())
        MEMBER_PENDING.clear()
        if rows:
            await self.sql(self._write_members, rows)

    def _find_member(self, gid, username):
        return self.db.execute(
            "SELECT uid FROM members WHERE gid=? AND username=? AND seen>? ORDER BY seen DESC LIMIT 1",
            (gid, username.casefold(), time.time() - MEMBER_TTL)).fetchone()

    async def find_member(self, gid, username):
        await self.flush_members()
        row = await self.sql(self._find_member, gid, username)
        return int(row[0]) if row else None

    def _prune_members(self):
        with self.db:
            self.db.execute("DELETE FROM members WHERE seen<?", (time.time() - MEMBER_TTL,))
            for (gid,) in self.db.execute("SELECT DISTINCT gid FROM members").fetchall():
                self.db.execute("DELETE FROM members WHERE gid=? AND uid NOT IN (SELECT uid FROM members WHERE gid=? ORDER BY seen DESC LIMIT 5000)", (gid, gid))
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


STORE = GuardStore()


def remember_member(gid, user):
    if not user or user.is_bot:
        return
    key = (gid, user.id)
    MEMBER_PENDING[key] = (gid, user.id, (user.username or "").casefold(), clip(user.full_name, 100), time.time())
    MEMBER_PENDING.move_to_end(key)
    while len(MEMBER_PENDING) > 2000:
        MEMBER_PENDING.popitem(last=False)


async def bot_name():
    global BOT_NAME
    if not BOT_NAME:
        BOT_NAME = (await bot.get_me()).username
    return BOT_NAME


def hidden(group, uid):
    return int(uid) in group.get("hidden", [])


async def require_admin(gid, uid, permission=None):
    group = STORE.groups.get(gid)
    if not STORE.ready or not group or not group.get("active"):
        raise ValueError("Группа недоступна. Добавьте бота и восстановите список.")
                                                                                 
    actor = await bot.get_chat_member(gid, uid)
    own = await bot.get_chat_member(gid, bot.id)
    if actor.status not in ADMIN_STATUSES:
        raise ValueError("Управлять группой могут только её администраторы.")
    if own.status not in PRESENT_STATUSES:
        raise ValueError("Бота больше нет в этой группе.")
    if permission:
        if actor.status != "creator" and not getattr(actor, permission, False):
            raise ValueError("У вас нет нужных прав администратора.")
        if own.status != "administrator" or not getattr(own, permission, False):
            raise ValueError("Выдайте боту необходимые права администратора.")
    return group, actor, own


async def admin_ids(gid, force=False):
    cached = ADMIN_CACHE.get(gid)
    if not force and cached and time.monotonic() < cached[0]:
        return cached[1]
    admins = await bot.get_chat_administrators(gid)
    members = {m.user.id: m for m in admins}
    ADMIN_CACHE[gid] = (time.monotonic() + 45, members)
    return members


async def sync_admins(group):
    admins = await admin_ids(group["id"], force=True)
    mapping = {str(uid): clip(m.user.full_name, 100) for uid, m in admins.items() if not m.user.is_bot}
    changed = mapping != group.get("admins")
    group["admins"] = mapping
    if changed:
        await STORE.save(group)
    return admins


async def show(uid, text, builder, screen="GROUP"):
    state = ub.get_user_state(uid)
    state["state"] = "GUARD_" + screen
    if any(is_group_owner(group, uid) and gid in STORE.errors for gid, group in STORE.groups.items()):
        text += "\n\n<i>⚠️ Есть изменения, ещё не подтверждённые Supabase. Они сохранены локально; повторяем отправку.</i>"
    await ub.edit_or_send(uid, text, reply_markup=builder.as_markup(), parse_mode="HTML")


def button(builder, text, data):
    builder.row(types.InlineKeyboardButton(text=text, callback_data=data))


def page_buttons(builder, page, pages, prefix):
    if pages <= 1:
        return
    builder.row(types.InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}{page-1}" if page else "gg:noop"),
                types.InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="gg:noop"),
                types.InlineKeyboardButton(text="➡️", callback_data=f"{prefix}{page+1}" if page+1 < pages else "gg:noop"))


def is_group_owner(group, uid):
    return int(group.get("added_by") or 0) == int(uid)


def has_hidden_group(uid):
    return any(int(uid) in g.get("hidden", []) and is_group_owner(g, uid) for g in STORE.groups.values())


GROUP_LINK_CACHE = {}


async def group_title_link(group):
    gid = int(group["id"])
    cached = GROUP_LINK_CACHE.get(gid)
    if cached and time.monotonic() - cached[0] < 60:
        url = cached[1]
    else:
        url = group.get("link")
        try:
            chat = await bot.get_chat(gid)
            if getattr(chat, "username", None):
                url = f"https://t.me/{chat.username}"
            elif getattr(chat, "invite_link", None):
                url = chat.invite_link
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        if not url:
            raw = str(abs(gid))
            url = f"https://t.me/c/{raw[3:]}" if raw.startswith("100") else f"tg://openmessage?chat_id={gid}"
        GROUP_LINK_CACHE[gid] = (time.monotonic(), url)
    return f'<a href="{html.escape(url, quote=True)}">{esc(group["title"])}</a>'


async def message_link(group, message_id, label="сообщение"):
    gid = int(group["id"])
    username = group.get("username")
    try:
        chat = await bot.get_chat(gid)
        username = getattr(chat, "username", None)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    if username:
        url = f"https://t.me/{username}/{int(message_id)}"
    else:
        raw = str(abs(gid))
        url = (f"https://t.me/c/{raw[3:]}/{int(message_id)}" if raw.startswith("100")
               else f"tg://openmessage?chat_id={gid}&message_id={int(message_id)}")
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def reported_message_text(message):
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    if text:
        return clip(text, 4096)
    for field, label in (("photo", "Фото"), ("video", "Видео"), ("document", "Файл"),
                         ("sticker", "Стикер"), ("animation", "Анимация"), ("voice", "Голосовое сообщение"),
                         ("video_note", "Видеосообщение"), ("audio", "Аудио"), ("poll", "Опрос")):
        media = getattr(message, field, None)
        if media:
            suffix = getattr(media, "file_name", None) or getattr(media, "question", None) or ""
            return clip(f"[{label}] {suffix}".strip(), 4096)
    return "[Сообщение без текста]"


async def reported_messages_html(group, item):
    lines = []
    for message in item.get("messages", [])[-3:]:
        try:
            mid = int(message["message_id"])
        except (KeyError, TypeError, ValueError):
            continue
        gid = int(message.get("group_id") or group["id"])
        source_group = STORE.groups.get(gid) or {"id": gid, "title": message.get("group_title") or group.get("title", "Группа"), "username": message.get("group_username")}
        body = clip(message.get("text") or f"Сообщение №{mid} (текст старой жалобы не сохранён)", 650)
        lines.append("• " + await message_link(source_group, mid, body))
    return "\n".join(lines) if lines else "<i>История старой жалобы не сохранена.</i>"


def _aggregate_evidence(uid, field):
    evidence = [(group, item) for group in STORE.groups.values()
                if (field == "reports" or group.get("active"))
                and (item := group.get(field, {}).get(str(int(uid))))
                and (field != "local_bans" or not item.get("until_date") or float(item["until_date"]) > time.time())]
    if not evidence:
        return None, None
    evidence.sort(key=lambda row: float(row[1].get("updated", 0)), reverse=True)
    group, latest = evidence[0]
    result = copy.deepcopy(latest)
    result["count"] = sum(int(item.get("count", 0)) for _, item in evidence)
    reasons, messages = [], {}
    for source_group, item in evidence:
        reasons.extend(copy.deepcopy(report_reasons(item)))
        for message in item.get("messages", []):
            record = copy.deepcopy(message)
            record.setdefault("group_id", source_group["id"])
            record.setdefault("group_title", source_group.get("title", "Группа"))
            record.setdefault("group_username", source_group.get("username"))
            key = (record["group_id"], record.get("message_id"))
            if key not in messages or float(record.get("at", 0)) >= float(messages[key].get("at", 0)):
                messages[key] = record
    result["reasons"] = sorted(reasons, key=lambda r: float(r.get("at", 0)))[-3:]
    result["messages"] = sorted(messages.values(), key=lambda m: float(m.get("at", 0)))[-3:]
    return group, result


def aggregate_user_reports(uid):
    return _aggregate_evidence(uid, "reports")


def aggregate_local_bans(uid):
    return _aggregate_evidence(uid, "local_bans")


async def record_local_ban(group, user, until_date=0, source=None):
    """Archive evidence only AFTER Telegram confirms a group ban. Caller holds the group lock."""
    uid = str(user.id)
    if user.is_bot or global_ban_entry(user.id) is not None:
        return
    old = group.get("local_bans", {}).get(uid) or {}
    item = copy.deepcopy(group.get("reports", {}).get(uid) or old)
    item.update(name=clip(user.full_name, 100), username=user.username,
                updated=time.time(), until_date=int(until_date or 0))
    item.setdefault("count", 0)
    item.setdefault("reasons", [])
    item.setdefault("messages", [])
    if source and source.from_user and source.from_user.id == user.id:
        record = {"message_id": int(source.message_id), "text": reported_message_text(source),
                  "at": time.time(), "group_id": group["id"], "group_title": group["title"],
                  "group_username": group.get("username")}
        item["messages"] = ([m for m in item["messages"] if m.get("message_id") != record["message_id"]] + [record])[-3:]
    group.setdefault("local_bans", {})[uid] = item
    await STORE.save(group, urgent=True)


def global_reports_rows():
    targets = {int(target) for group in STORE.groups.values() if group.get("active")
               for target in group.get("local_bans", {}) if str(target) not in STORE.global_bans}
    rows = []
    for target in targets:
        group, item = aggregate_local_bans(target)
        if item:
            rows.append((group, target, item))
    rows.sort(key=lambda row: float(row[2].get("updated", 0)), reverse=True)
    return rows


def global_banned_rows():
    rows = [(int(uid), entry) for uid, entry in STORE.global_bans.items() if not entry.get("unbanning")]
    return sorted(rows, key=lambda row: float(row[1].get("updated", 0)), reverse=True)


def is_global_banned(uid):
    entry = STORE.global_bans.get(str(int(uid)))
    return bool(entry is not None and not entry.get("unbanning"))


def global_ban_entry(uid):
    return STORE.global_bans.get(str(int(uid)))


def _global_reasons_for(uid):
    collected = []
    for group in STORE.groups.values():
        item = group.get("reports", {}).get(str(int(uid)))
        if not item:
            continue
        for reason in report_reasons(item):
            collected.append((float(reason.get("at", 0)), clip(reason.get("text") or "Без описания", 240)))
    collected.sort(key=lambda row: row[0], reverse=True)
    result = []
    seen = set()
    for _, reason in collected:
        key = reason.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(reason)
        if len(result) == 3:
            break
    return result or ["Без описания"]


async def global_ban_notice(group, uid, name, reasons):
    reason_text = ", ".join(esc(clip(reason, 240)) for reason in reasons[:3]) or "Без описания"
    await bot.send_message(
        group["id"],
        f"Пользователь {user_link(uid, name)} был глобально забанен\n\n<b>Причина:</b> {reason_text}",
        parse_mode="HTML",
    )


def queue_global_ban(gid, uid, joining=False, delay=0):
    key = (int(gid), int(uid))
    old = GLOBAL_BAN_PENDING.get(key, {})
    GLOBAL_BAN_PENDING[key] = {"joining": joining or old.get("joining", False),
                               "retry_at": time.monotonic() + delay}


async def enforce_global_ban_in_group(group, uid, user=None, announce=True, joining=False):
    if not group:
        return False
    key = (int(group["id"]), int(uid))
    async with GLOBAL_ENFORCEMENT_LOCKS.setdefault(key, asyncio.Lock()):
        try:
            return await _enforce_global_ban_in_group(group, uid, user, announce, joining)
        except TelegramRetryAfter as exc:
            queue_global_ban(*key, joining=joining, delay=exc.retry_after + 1)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            queue_global_ban(*key, joining=joining, delay=15)
            logging.warning("Global ban retry in %s: %s", key[0], type(exc).__name__)
            return False


async def _enforce_global_ban_in_group(group, uid, user, announce, joining):
    key = (int(group["id"]), int(uid))
    entry = global_ban_entry(uid)
    if not entry or entry.get("unbanning") or not group or not group.get("active"):
        GLOBAL_BAN_PENDING.pop(key, None)
        return False
    gid = group["id"]
    try:
                                                                                  
                                                                         
        own = await bot.get_chat_member(gid, bot.id)
        if own.status != "administrator" or not getattr(own, "can_restrict_members", False):
            queue_global_ban(gid, uid, joining=joining, delay=60)
            return False

        member = await bot.get_chat_member(gid, int(uid))
        if member.status in ADMIN_STATUSES:
                                                                                
                                                                                  
                                                       
            GLOBAL_BAN_PENDING.pop(key, None)
            return False

        was_present = member_present(member) or joining
        if not is_global_banned(uid):
            GLOBAL_BAN_PENDING.pop(key, None)
            return False

        if member.status == "kicked":
                                                                                   
                                                            
            entry = global_ban_entry(uid)
            entry["banned_groups"] = sorted(set(entry.get("banned_groups", [])) | {gid})
            await STORE.save_global_bans(urgent=True)
            GLOBAL_BAN_PENDING.pop(key, None)
            return True

                                                                                   
                                                                                
        await bot.ban_chat_member(
            gid, int(uid), revoke_messages=True, request_timeout=20
        )
        if not is_global_banned(uid):
            await bot.unban_chat_member(gid, int(uid), only_if_banned=True, request_timeout=20)
            return False

        entry = global_ban_entry(uid)
        entry["banned_groups"] = sorted(set(entry.get("banned_groups", [])) | {gid})
        await STORE.save_global_bans(urgent=True)
        GLOBAL_BAN_PENDING.pop(key, None)

                                                                                  
        if announce and was_present:
            name = entry.get("name") or (getattr(user, "full_name", None) if user else None) or member.user.full_name or "Пользователь"
            try:
                await global_ban_notice(group, uid, name, entry.get("reasons") or ["Без описания"])
            except (TelegramBadRequest, TelegramForbiddenError):
                logging.info("Global-ban notice could not be sent to group %s", gid)
        return True
    except TelegramRetryAfter:
        raise
    except (TelegramBadRequest, TelegramForbiddenError):
                                                                                   
                                                                             
                                                                       
        queue_global_ban(gid, uid, joining=joining, delay=60)
        return False


async def _retry_global_ban(key, joining):
    try:
        group = STORE.groups.get(key[0])
        if group:
            await enforce_global_ban_in_group(group, key[1], joining=joining)
        else:
            GLOBAL_BAN_PENDING.pop(key, None)
    finally:
        GLOBAL_BAN_RUNNING.discard(key)


async def global_ban_worker():
    while True:
        for key, job in list(GLOBAL_BAN_PENDING.items()):
            if not is_global_banned(key[1]):
                GLOBAL_BAN_PENDING.pop(key, None)
                continue
            if key not in GLOBAL_BAN_RUNNING and job["retry_at"] <= time.monotonic():
                if len(GLOBAL_BAN_RUNNING) >= 4:
                    break
                GLOBAL_BAN_RUNNING.add(key)
                spawn(_retry_global_ban(key, job["joining"]))
                await asyncio.sleep(0.1)
        await asyncio.sleep(2)


async def close_user_reports(uid):
    for group in list(STORE.groups.values()):
        if str(uid) in group.get("reports", {}) or str(uid) in group.get("local_bans", {}):
            async with lock_for(group["id"]):
                group.get("reports", {}).pop(str(uid), None)
                group.get("local_bans", {}).pop(str(uid), None)
                await STORE.save(group, urgent=True)


async def global_ban_user(uid, reason=None):
    uid = int(uid)
    global_reason = clip((reason or "").strip(), 240)
    async with GLOBAL_USER_LOCKS.setdefault(uid, asyncio.Lock()):
        group, item = aggregate_local_bans(uid)
        old = global_ban_entry(uid) or {}
        if not item and not old:
            raise ValueError("Глобальный бан доступен только после бана пользователя в группе.")
        if not global_reason:
            raise ValueError("Укажите причину глобального бана.")
        snapshot = copy.deepcopy(item or old.get("report") or {})
        name = snapshot.get("name") or old.get("name") or "Пользователь"
        username = snapshot.get("username") or old.get("username")
        source = ({k: group.get(k) for k in ("id", "title", "username", "link")} if group else old.get("group"))
        STORE.global_bans[str(uid)] = {
            "name": clip(name, 100), "username": username,
            "global_reason": global_reason,
            "reasons": [global_reason], "report": snapshot,
            "group": source, "updated": time.time(),
            "banned_groups": list(old.get("banned_groups", [])),
        }
        await STORE.save_global_bans(urgent=True)
                                                                                  
        await close_user_reports(uid)
        banned_count = 0
        for group in list(STORE.groups.values()):
            if not group.get("active"):
                continue
            applied = await enforce_global_ban_in_group(group, uid, announce=True)
            if applied:
                banned_count += 1
            await asyncio.sleep(0.05)
        return banned_count


async def _complete_global_unban(uid):
    entry = global_ban_entry(uid)
    if not entry or not entry.get("unbanning"):
        return 0
    completed, failed = 0, []
    retry_delay = 30
    for gid in list(entry.get("unban_pending", [])):
        try:
            await bot.unban_chat_member(int(gid), int(uid), only_if_banned=True, request_timeout=20)
            completed += 1
            entry["unban_pending"] = [g for g in entry.get("unban_pending", []) if int(g) != int(gid)]
            await STORE.save_global_bans(urgent=True)
        except TelegramRetryAfter as exc:
            failed.append(int(gid))
            retry_delay = max(retry_delay, exc.retry_after + 1)
        except (TelegramBadRequest, TelegramForbiddenError):
            failed.append(int(gid))
            retry_delay = max(retry_delay, 300)
        except Exception:
            failed.append(int(gid))
        await asyncio.sleep(0.05)
    if not failed:
        STORE.global_bans.pop(str(uid), None)
    else:
        entry["unban_pending"] = failed
        entry["retry_at"] = time.time() + retry_delay
    await STORE.save_global_bans(urgent=True)
    return completed


async def global_unban_user(uid):
    uid = int(uid)
    async with GLOBAL_USER_LOCKS.setdefault(uid, asyncio.Lock()):
        entry = global_ban_entry(uid)
        if not entry:
            raise ValueError("Глобальный бан уже снят.")
        if not entry.get("unbanning"):
                                                                                  
                                                                               
            groups = set(entry.get("banned_groups", []))
            groups.update(g["id"] for g in STORE.groups.values() if g.get("active"))
            entry.update(unbanning=True, unban_pending=sorted(groups), retry_at=0)
            await STORE.save_global_bans(urgent=True)
        await close_user_reports(uid)
        return await _complete_global_unban(uid)


async def _retry_global_unban(uid):
    try:
        async with GLOBAL_USER_LOCKS.setdefault(uid, asyncio.Lock()):
            await _complete_global_unban(uid)
    finally:
        GLOBAL_UNBAN_RUNNING.discard(uid)


async def global_unban_worker():
    while True:
        for uid_text, entry in list(STORE.global_bans.items()):
            uid = int(uid_text)
            if entry.get("unbanning") and float(entry.get("retry_at", 0)) <= time.time() and uid not in GLOBAL_UNBAN_RUNNING:
                GLOBAL_UNBAN_RUNNING.add(uid)
                spawn(_retry_global_unban(uid))
        await asyncio.sleep(2)


async def visible_groups(uid):
    found = []
    for group in list(STORE.groups.values()):
        if not group.get("active") or hidden(group, uid) or not is_group_owner(group, uid):
            continue
        try:
            await require_admin(group["id"], uid)
            found.append(group)
        except (ValueError, TelegramBadRequest, TelegramForbiddenError):
            continue
    return sorted(found, key=lambda g: (g["title"].casefold(), g["id"]))


async def render_groups(uid, page=0, banner=""):
    if not STORE.ready:
        raise ValueError("Group Guard запускается. Попробуйте через несколько секунд.")
    groups = await visible_groups(uid)
    pages = max(1, (len(groups) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    builder = InlineKeyboardBuilder()
    for group in groups[page*PAGE_SIZE:(page+1)*PAGE_SIZE]:
        button(builder, "👥 " + clip(group["title"], 55), f"gg:group:{group['id']}")
    username = await bot_name()
    builder.row(types.InlineKeyboardButton(text="➕ Добавить группу", url=f"https://t.me/{username}?startgroup=guard&admin=delete_messages+restrict_members"))
    if has_hidden_group(uid):
        button(builder, "🔄 Восстановить группы", "gg:restore")
    page_buttons(builder, page, pages, "gg:list:")
    button(builder, "Назад в главное меню 🏠", "root_menu")
    text = "<b>🔰 Group Guard</b>\n<i>Управление группами</i>"
    if not groups:
        text += "\n\nДобавьте бота в группу и выдайте ему права администратора."
    if banner:
        text += "\n\n" + esc(banner)
    ub.get_user_state(uid)["guard_page"] = page
    await show(uid, text, builder, "LIST")


async def restore_groups(uid):
    failures = 0
    for group in list(STORE.groups.values()):
        try:
            own = await bot.get_chat_member(group["id"], bot.id)
            actor = await bot.get_chat_member(group["id"], uid)
            if own.status not in PRESENT_STATUSES or actor.status not in ADMIN_STATUSES:
                continue
            if not group.get("added_by") and actor.status == "creator":
                group["added_by"] = uid
            if not is_group_owner(group, uid) or uid not in group.get("hidden", []):
                continue
            chat = await bot.get_chat(group["id"])
            async with lock_for(group["id"]):
                group["active"] = True
                group["title"] = clip(chat.title, 128)
                group["hidden"] = [x for x in group.get("hidden", []) if x != uid]
                await STORE.save(group)
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
        except TelegramRetryAfter:
            failures += 1
            break
        await asyncio.sleep(0.05)
    return failures


async def render_group(uid, group):
    gid = group["id"]
    active = any(group["settings"].get(mode) for mode in MODES)
    try:
        admins = await sync_admins(group)
        count = await bot.get_chat_member_count(gid)
        group["member_count"] = count
    except (TelegramBadRequest, TelegramForbiddenError):
        admins = {}
        count = group.get("member_count") or "?"
    names = [user_link(m.user.id, m.user.full_name) for m in admins.values() if not m.user.is_bot]
    title = await group_title_link(group)

    builder = InlineKeyboardBuilder()
    builder.row(
        types.InlineKeyboardButton(text=MODES["spam"][0], callback_data=f"gg:mode:{gid}:spam"),
        types.InlineKeyboardButton(text=MODES["files"][0], callback_data=f"gg:mode:{gid}:files"),
    )
    builder.row(
        types.InlineKeyboardButton(text="📛АнтиНакрутка", callback_data=f"gg:mode:{gid}:raid"),
        types.InlineKeyboardButton(text=MODES["service"][0], callback_data=f"gg:mode:{gid}:service"),
    )
    builder.row(
        types.InlineKeyboardButton(text="📑 Команды", callback_data=f"gg:commands:{gid}"),
        types.InlineKeyboardButton(text="🔖 Жалобы", callback_data=f"gg:targets:{gid}:0"),
    )
    button(builder, "🗑 Удалить из списка", f"gg:remove:{gid}")
    button(builder, "Назад в меню ⬅️", "gg:list:0")

    text = (f"<b>👥 {title}</b>\n\n"
            f"Защита группы — {'активна 🟢' if active else 'пока неактивна ⚪'}\n\n"
            f"<b>Участников:</b> {count}\n\n"
            f"<b>Админы:</b> {', '.join(names[:20]) if names else 'Нет данных'}")
    if len(names) > 20:
        text += f" и ещё {len(names)-20}"
    if gid in STORE.errors:
        text += "\n\n<i>Резервная копия настроек ещё не записана. Повторяем автоматически.</i>"
    await show(uid, text, builder)


async def render_mode(uid, group, mode):
    title, description = MODES[mode]
    enabled = bool(group["settings"].get(mode))
    builder = InlineKeyboardBuilder()
    button(builder, "Выключить 🔴" if enabled else "Включить 🟢", f"gg:toggle:{group['id']}:{mode}")
    button(builder, "Назад в меню ⬅️", f"gg:group:{group['id']}")
    await show(uid, f"<b>{title}</b>\n👥 <b>{await group_title_link(group)}</b>\n\n<i>{description}</i>\n\n<b>Статус:</b> {'Включён 🟢' if enabled else 'Выключен 🔴'}", builder)


async def render_info(uid, group):
    gid = group["id"]
    admins = await sync_admins(group)
    count = await bot.get_chat_member_count(gid)
    if group.get("member_count") != count:
        group["member_count"] = count
        await STORE.save(group)
    names = [user_link(m.user.id, m.user.full_name) for m in admins.values()]
    if bot.id not in admins:
        own = await bot.get_chat_member(gid, bot.id)
        if own.status == "administrator":
            names.append(user_link(bot.id, own.user.full_name))
    text = f"<b>ℹ️ Информация о группе</b>\n👥 <b>{await group_title_link(group)}</b>\n\n<b>Участников:</b> {count}\n<b>Администраторы:</b>\n"
                                                                
    text += "\n".join(names[:20])
    if len(names) > 20:
        text += f"\n<i>И ещё {len(names)-20}</i>"
    builder = InlineKeyboardBuilder()
    button(builder, "Назад в меню ⬅️", f"gg:group:{gid}")
    await show(uid, text, builder)


async def render_commands(uid, group):
    builder = InlineKeyboardBuilder()
    button(builder, "Назад в меню ⬅️", f"gg:group:{group['id']}")
    await show(uid, "<b>📑 Список команд</b>\n\n"
               "<code>/ban 1 day 2 hours @username</code>\n"
               "<code>/mute 5 мин 2 часа 1 день @username</code>\n"
               "<code>/kick @username</code>\n"
               "<code>/admin причина</code>\n\n"
               "<i>Бан, мут и кик работают также ответом на сообщение, без @username. "
               "Для жалобы ответьте командой /admin на сообщение участника; причина необязательна.</i>\n\n"
               "Срок: число + <b>sec, min, hour, day</b> или <b>сек, мин, час, дн</b>. "
               "Единицы можно смешивать и писать в любом порядке. Без срока — навсегда. "
               "Допустимый временный срок: от 60 секунд до 365 дней.\n\n"
               "<i>Для @username участник должен недавно написать в группе. "
               "Если бот его ещё не видел, используйте ответ на сообщение. "
               "Мут доступен в супергруппах. Команды управления требуют права блокировки участников.</i>", builder)


def unread_reports(group, uid):
    seen = int(group.get("seen", {}).get(str(uid), 0))
    return sum(int(r.get("sequence", 0)) > seen for r in group.get("reports", {}).values())


async def render_reports_home(uid, group):
    unread = unread_reports(group, uid)
    builder = InlineKeyboardBuilder()
    button(builder, f"📨 Жалобы ({unread})" if unread else "📨 Жалобы", f"gg:targets:{group['id']}:0")
    button(builder, "Назад в меню ⬅️", f"gg:group:{group['id']}")
    await show(uid, f"<b>📣 Жалобы пользователей</b>\n👥 <b>{await group_title_link(group)}</b>\n\n"
               f"Участников с открытыми жалобами: <b>{len(group['reports'])}</b>", builder)


async def render_targets(uid, group, page=0):
    rows = sorted(group["reports"].items(), key=lambda x: x[1].get("updated", 0), reverse=True)
    pages = max(1, (len(rows) + 4) // 5)
    page = max(0, min(page, pages - 1))
    group["seen"][str(uid)] = group.get("sequence", 0)
    await STORE.save(group)
    builder = InlineKeyboardBuilder()
    for target, item in rows[page*5:(page+1)*5]:
        button(builder, f"{clip(item['name'], 42)} ({item['count']})", f"gg:target:{group['id']}:{target}")
    page_buttons(builder, page, pages, f"gg:targets:{group['id']}:")
    button(builder, "Назад в меню ⬅️", f"gg:group:{group['id']}")
    await show(uid, f"<b>📨 Жалобы</b>\n👥 <b>{await group_title_link(group)}</b>" + ("\n\nОткрытых жалоб пока нет." if not rows else ""), builder)


def report_reasons(item):
    return [r for r in item.get("reasons", [])
            if str(r.get("text") or "").strip().casefold() not in {"", "без описания", "без причины"}][-3:]


async def report_text(group, target, item, notification=False):
    stamp = dt.datetime.fromtimestamp(item["updated"], dt.timezone(dt.timedelta(hours=5))).strftime("%d.%m.%Y %H:%M")
    username = "@" + item["username"] if item.get("username") else "Юзернейм не указан"
    reasons = "\n".join("• " + esc(clip(r["text"], 240)) for r in reversed(report_reasons(item)))
    group_link = await group_title_link(group)
    messages_text = await reported_messages_html(group, item)
    return (f"<b>{'📣 Новая жалоба' if notification else '📣 Жалобы на участника'}</b>\n"
            f"👥 <b>{group_link}</b>\n\n"
            f"<b>Пользователь:</b> {user_link(target, item['name'])}\n"
            f"<b>Юзернейм:</b> {user_link(target, username) if item.get('username') else esc(username)}\n<b>Число жалоб:</b> {item['count']}\n"
            f"<b>Жалоба:</b> {stamp} (UTC +5)\n"
            f"<b>Последние сообщения:</b>\n{messages_text}\n\n"
            f"<b>Последние причины:</b>\n{reasons or 'Причины не указаны'}")


async def render_target(uid, group, target):
    item = group["reports"].get(str(target))
    if not item:
        await render_targets(uid, group)
        return
    builder = InlineKeyboardBuilder()
    button(builder, "Забанить 🚫", f"gg:ban:{group['id']}:{target}")
    button(builder, "Назад в меню ⬅️", f"gg:targets:{group['id']}:0")
    await show(uid, await report_text(group, target, item), builder)


async def open_deep_link(message, argument):
    uid = message.from_user.id
    try:
        gid = int(argument.removeprefix("gg_"))
        group, actor, _ = await require_admin(gid, uid)
        if not group.get("added_by") and actor.status == "creator":
            group["added_by"] = uid
            await STORE.save(group)
        if not is_group_owner(group, uid):
            raise ValueError("Настройки этой группы доступны только тому, кто добавил Qwitty.")
        async with lock_for(gid):
            group["admins"][str(uid)] = clip(message.from_user.full_name, 100)
            group["hidden"] = [x for x in group.get("hidden", []) if x != uid]
            await STORE.save(group)
        builder = InlineKeyboardBuilder()
        button(builder, "🔰 Открыть меню групп", "gg:list:0")
        await show(uid, f"<b>👋 Вы администратор группы {await group_title_link(group)}</b>\n\n"
                   "Здесь можно настроить защиту группы и рассматривать жалобы участников.", builder)
    except (ValueError, TelegramBadRequest, TelegramForbiddenError):
        builder = InlineKeyboardBuilder()
        button(builder, "Назад в главное меню 🏠", "root_menu")
        await show(uid, "<b>Доступ недоступен</b>\nУправление открыто только действующим администраторам группы, в которой есть Qwitty.", builder)


async def safe_answer(callback, text=None, alert=False):
    try:
        await callback.answer(text, show_alert=alert, cache_time=0)
    except TelegramBadRequest:
        pass


@dp.callback_query(F.data == "guard")
@dp.callback_query(F.data.startswith("gg:"))
async def guard_callback(callback: types.CallbackQuery):
    uid = callback.from_user.id
    if not callback.message or callback.message.chat.id != uid:
        await safe_answer(callback, "Откройте бота в личном чате.", True)
        return
    action = callback.data
    try:
        if action == "guard" or action.startswith("gg:list:"):
            await safe_answer(callback)
            await render_groups(uid, int(action.rsplit(":", 1)[1]) if action != "guard" else 0)
            return
        parts = action.split(":")
        command = parts[1]
        if command == "noop":
            await safe_answer(callback)
            return
        if command == "restore":
            await safe_answer(callback, "Восстановление…")
            started = time.monotonic()
            failures = await restore_groups(uid)
            await asyncio.sleep(max(0, 3 - (time.monotonic() - started)))
            await render_groups(uid, banner="Часть групп не проверена: повторите позже." if failures else "Список восстановлен.")
            return
        gid = int(parts[2])
        permission = "can_restrict_members" if command in {"ban", "nb"} else None
        if command == "toggle":
            permission = "can_restrict_members" if len(parts) > 3 and parts[3] == "raid" else "can_delete_messages"
        group, _, _ = await require_admin(gid, uid, permission)
        if command not in {"ban", "nb", "ignore"} and not is_group_owner(group, uid):
            raise ValueError("Настройки этой группы доступны только тому, кто добавил Qwitty.")
        if command == "soon":                                               
            await safe_answer(callback)
            await render_mode(uid, group, "raid")
            return
        if command == "ignore":
            await callback.message.delete()
            await safe_answer(callback)
            return
        await safe_answer(callback)
        async with lock_for(gid):
            if command == "group":
                await render_group(uid, group)
            elif command == "mode":
                await render_mode(uid, group, parts[3])
            elif command == "toggle":
                mode = parts[3]
                if mode not in MODES:
                    raise ValueError("Неизвестный режим")
                group["settings"][mode] = not group["settings"].get(mode, False)
                if mode == "spam":
                    reset_spam(gid)
                if mode == "raid" and not group["settings"][mode]:
                    group.pop("raid_window", None)
                    group.pop("raid_pending", None)
                    group.pop("raid_seen", None)
                await STORE.save(group)
                await render_mode(uid, group, mode)
            elif command == "commands":
                await render_commands(uid, group)
            elif command == "reports":
                await render_reports_home(uid, group)
            elif command == "targets":
                await render_targets(uid, group, int(parts[3]))
            elif command == "target":
                await render_target(uid, group, int(parts[3]))
            elif command in {"ban", "nb"}:
                target = int(parts[3])
                if str(target) not in group["reports"]:
                    raise ValueError("Жалоба уже обработана другим администратором.")
                target_member = await bot.get_chat_member(gid, target)
                if target_member.status in ADMIN_STATUSES:
                    group["reports"].pop(str(target), None)
                    await STORE.save(group, urgent=True)
                    raise ValueError("Нельзя блокировать владельца или администратора группы. Жалоба удалена.")
                await moderate(group, uid, target, "ban", None, purge_messages=True)
                group["reports"].pop(str(target), None)
                await STORE.save(group, urgent=True)
                if command == "nb":
                    try:
                        await callback.message.delete()
                    except TelegramBadRequest:
                        pass
                                                                                          
                    state = ub.get_user_state(uid)
                    token = ub.UI_ACTION_TASK.set(None)
                    try:
                        if state.get("state", "").startswith("GUARD") and state.get("msg_id"):
                            await render_targets(uid, group)
                    finally:
                        ub.UI_ACTION_TASK.reset(token)
                else:
                    await render_targets(uid, group)
            elif command == "remove":
                builder = InlineKeyboardBuilder()
                button(builder, "Да, я уверен", f"gg:hide:{gid}")
                button(builder, "Назад ⬅️", f"gg:group:{gid}")
                await show(uid, f"<b>Убрать {await group_title_link(group)} из вашего списка?</b>\n\n"
                           "Бот останется в группе. Уведомления о её жалобах вам приходить не будут.", builder)
            elif command == "hide":
                group["hidden"] = sorted(set(group.get("hidden", [])) | {uid})
                await STORE.save(group)
                await render_groups(uid)
    except (ValueError, KeyError, IndexError) as exc:
        await show_callback_error(callback, str(exc) if isinstance(exc, ValueError) else "Кнопка устарела. Откройте меню заново.")
    except TelegramRetryAfter as exc:
        await show_callback_error(callback, f"Telegram просит подождать {exc.retry_after} сек.")
    except (TelegramBadRequest, TelegramForbiddenError):
        await show_callback_error(callback, "Действие недоступно. Проверьте права бота и администратора.")
    except Exception as exc:
        logging.warning("Guard callback: %s", type(exc).__name__)
        await show_callback_error(callback, "Не удалось сохранить или выполнить действие. Повторите позже.")


async def show_callback_error(callback, text):
                                                                                    
    if (callback.data or "").startswith(("gg:nb:", "gg:ignore:")):
        try:
            await callback.message.edit_text(esc(text), parse_mode="HTML")
        except TelegramBadRequest:
            pass
    else:
        builder = InlineKeyboardBuilder()
        button(builder, "Назад в меню ⬅️", "gg:list:0")
        await show(callback.from_user.id, f"<b>⚠️ {esc(text)}</b>", builder)
    await safe_answer(callback)


def dangerous_file(message):
    document = message.document
    if not document:
        return False
    name = unicodedata.normalize("NFKC", document.file_name or "").casefold()
    name = "".join(c for c in name if unicodedata.category(c) != "Cf")
    name = name.replace("\\", "/").rsplit("/", 1)[-1].rstrip(" .")
                                                                                   
    pieces = name.split(".")[1:]
    return any("." + part.rstrip(" .") in DANGEROUS_EXTENSIONS for part in pieces)


def reset_spam(gid):
    for key in list(SPAM_LAST):
        if key[0] == gid:
            SPAM_LAST.pop(key, None)


def spam_flood(gid, uid, mid, now=None):
    """Return True for the 2nd+ message from one user inside a 1-second gap."""
    now = time.monotonic() if now is None else float(now)
    key = (int(gid), int(uid))
    previous = SPAM_LAST.get(key)
    if previous and previous[0] == int(mid):
        return False
    SPAM_LAST[key] = (int(mid), now)
    SPAM_LAST.move_to_end(key)
    while len(SPAM_LAST) > 10000:
        SPAM_LAST.popitem(last=False)
    return bool(previous and 0 <= now - previous[1] <= SPAM_WINDOW)


UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "с": 1, "сек": 1, "секунда": 1, "секунды": 1, "секунд": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "м": 60, "мин": 60, "минута": 60, "минуты": 60, "минут": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400,
}
DURATION_TOKEN = re.compile(r"(\d+)\s*([a-zа-яё]+)\.?", re.I)


def parse_duration(text):
    text = text.strip().casefold()
    if not text:
        return None
    total, position = 0, 0
    for match in DURATION_TOKEN.finditer(text):
        if text[position:match.start()].strip(" ,"):
            raise ValueError("Срок: число с единицей, например 5 мин 2 часа 1 день.")
        unit = UNITS.get(match[2])
        if unit is None or int(match[1]) <= 0:
            raise ValueError("Неизвестная единица срока или неположительное число.")
        total += int(match[1]) * unit
        position = match.end()
    if text[position:].strip(" ,") or not position:
        raise ValueError("После числа нужна единица: сек, мин, час или дн.")
    if not 60 <= total <= 365 * 86400:
        raise ValueError("Временный срок — от 60 секунд до 365 дней. Запас нужен, чтобы сетевое ожидание не превратило короткий срок в бессрочный.")
    return total


async def resolve_target(message, arguments):
    replied = message.reply_to_message
    mentions = re.findall(r"(?<!\w)@([A-Za-z0-9_]{1,32})(?!\w)", arguments)
    if len(mentions) > 1:
        raise ValueError("Укажите одного участника.")
    rest = re.sub(r"(?<!\w)@[A-Za-z0-9_]{1,32}(?!\w)", "", arguments).strip()
    if mentions:
        username = mentions[0]
        target = await STORE.find_member(message.chat.id, username)
        if not target:
                                                                         
            for uid, member in (await admin_ids(message.chat.id)).items():
                if (member.user.username or "").casefold() == username.casefold():
                    target = uid
                    break
        if not target:
            raise ValueError("Бот ещё не знает этого @username. Ответьте командой на сообщение участника.")
        current = await bot.get_chat_member(message.chat.id, target)
        if (current.user.username or "").casefold() != username.casefold():
            raise ValueError("Юзернейм изменился. Используйте ответ на сообщение участника.")
        if replied and replied.from_user and replied.from_user.id != target:
            raise ValueError("В ответе и @username указаны разные люди.")
        return target, rest
    if replied and replied.from_user and not replied.sender_chat:
        return replied.from_user.id, rest
    raise ValueError("Ответьте на сообщение участника или укажите его @username.")


async def moderate(group, actor, target, action, duration, source=None, purge_messages=False):
    gid = group["id"]
    await require_admin(gid, actor, "can_restrict_members")
    if target in {actor, bot.id}:
        raise ValueError("Нельзя применить эту команду к себе или к Qwitty.")
    member = await bot.get_chat_member(gid, target)
    if member.status in ADMIN_STATUSES:
        raise ValueError("Нельзя блокировать владельца или администратора группы.")
    if action in {"kick", "mute"} and member.status not in {"member", "restricted"}:
        raise ValueError("Участника сейчас нет в группе.")
    until = int(time.time()) + duration if duration else 0
    if action == "ban":
        if duration and (await bot.get_chat(gid)).type != "supergroup":
            raise ValueError("Временный бан доступен в супергруппах. В обычной группе срок Telegram не учитывает.")
                                                                             
                                                                                     
        await bot.ban_chat_member(
            gid, target, until_date=until, revoke_messages=bool(purge_messages), request_timeout=20
        )
        await record_local_ban(group, member.user, until, source)
    elif action == "kick":
                                                                                                 
        KICK_EVENTS[(gid, target)] = time.monotonic() + 120
        await bot.ban_chat_member(gid, target, until_date=int(time.time()) + 60)
        await bot.unban_chat_member(gid, target, only_if_banned=True)
    elif action == "mute":
        chat = await bot.get_chat(gid)
        if chat.type != "supergroup":
            raise ValueError("Мут доступен в супергруппах. Преобразуйте обычную группу в супергруппу.")
        permissions = types.ChatPermissions(
            can_send_messages=False, can_send_audios=False, can_send_documents=False,
            can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
            can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
            can_add_web_page_previews=False, can_change_info=False, can_invite_users=False,
            can_pin_messages=False, can_manage_topics=False)
        await bot.restrict_chat_member(gid, target, permissions=permissions,
                                       use_independent_chat_permissions=True, until_date=until, request_timeout=20)


async def group_reply(message, text):
    try:
        await message.reply(text, parse_mode="HTML")
    except TelegramBadRequest:
        await bot.send_message(message.chat.id, text, parse_mode="HTML")


async def notify_report(gid, target):
                                                                                            
    try:
        admins = await admin_ids(gid, force=True)
    except (TelegramBadRequest, TelegramForbiddenError):
        return
    for uid, member in admins.items():
        group = STORE.groups.get(gid)
        if not group or hidden(group, uid) or member.user.is_bot:
            continue
        item = group["reports"].get(str(target))
        if not item:
            return
        try:
            await require_admin(gid, uid)
            if hidden(group, uid):
                continue
                                                                                       
            builder = InlineKeyboardBuilder()
            button(builder, "Забанить 🚫", f"gg:nb:{gid}:{target}")
            button(builder, "Игнорировать 💤", f"gg:ignore:{gid}:{target}")
            await bot.send_message(uid, await report_text(group, target, item, True),
                                   parse_mode="HTML", reply_markup=builder.as_markup())
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after, 60))
        except (ValueError, TelegramBadRequest, TelegramForbiddenError):
            continue
        await asyncio.sleep(0.05)


async def report_notification_worker():
    while True:
        key = await REPORT_QUEUE.get()
        try:
            await notify_report(*key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning("Guard report notification: %s", type(exc).__name__)
        finally:
            REPORT_QUEUED.discard(key)
            REPORT_QUEUE.task_done()
        await asyncio.sleep(1)


async def receive_report(message, group, reason):
    source = message.reply_to_message
    reporter = message.from_user
    if (not source or not source.from_user or source.sender_chat or not reporter or message.sender_chat):
        raise ValueError("Ответьте командой /admin на сообщение участника. Причину можно написать после команды.")
    target = source.from_user
    if target.id == reporter.id or target.is_bot:
        raise ValueError("Жалобу можно отправить на другого участника, не на бота.")
    membership = await bot.get_chat_member(group["id"], reporter.id)
    if membership.status not in PRESENT_STATUSES or (membership.status == "restricted" and not membership.is_member):
        raise ValueError("Отправлять жалобы могут участники этой группы.")
    key = (group["id"], reporter.id)
    now = time.time()
    if now - REPORT_RATE.get(key, 0) < 20:
        raise ValueError("Подождите 20 секунд перед следующей жалобой.")
    fingerprint = hashlib.sha256(f"{reporter.id}:{target.id}:{source.message_id}".encode()).hexdigest()[:32]
    if fingerprint in group["dedup"]:
        raise ValueError("Ваша жалоба на это сообщение уже отправлена.")
    if str(target.id) not in group["reports"] and len(group["reports"]) >= MAX_TARGETS:
        raise ValueError("Список жалоб заполнен. Администраторам нужно рассмотреть текущие обращения.")
    REPORT_RATE[key] = now
    REPORT_RATE.move_to_end(key)
    while len(REPORT_RATE) > 5000:
        REPORT_RATE.popitem(last=False)
    group["sequence"] = int(group.get("sequence", 0)) + 1
    item = group["reports"].setdefault(str(target.id), {"count": 0, "reasons": [], "messages": []})
    item.setdefault("reasons", [])
    item.setdefault("messages", [])
    item.update(name=clip(target.full_name, 100), username=target.username,
                updated=now, sequence=group["sequence"])
    item["count"] += 1
    item["reasons"] = report_reasons(item)
    if reason.strip():
        item["reasons"] = (item["reasons"] + [{"text": clip(reason.strip(), 240), "at": now}])[-3:]
    record = {"message_id": int(source.message_id), "text": reported_message_text(source), "at": now,
              "group_id": group["id"], "group_title": group["title"], "group_username": group.get("username")}
    item["messages"] = ([m for m in item["messages"] if m.get("message_id") != record["message_id"]] + [record])[-3:]
    group["dedup"][fingerprint] = now
    if len(group["dedup"]) > 2000:
        oldest = sorted(group["dedup"], key=group["dedup"].get)[:len(group["dedup"])-2000]
        for entry in oldest:
            group["dedup"].pop(entry, None)
    await STORE.save(group, urgent=True)
    notification_key = (group["id"], target.id)
    if notification_key not in REPORT_QUEUED:
        try:
            REPORT_QUEUE.put_nowait(notification_key)
            REPORT_QUEUED.add(notification_key)
        except asyncio.QueueFull:
            logging.warning("Guard notification queue full; report remains in group menu")
                                                                   
    await group_reply(message, "📨 Жалоба сохранена и направлена администрации.")


async def handle_group_command(message, group):
    match = re.match(r"^/(ban|mute|kick|admin|start)(?:@([\w]+))?(?:\s+(.*))?$", message.text or "", re.I | re.S)
    if not match:
        return False
    command, addressed, arguments = match[1].casefold(), match[2], match[3] or ""
    if addressed and addressed.casefold() != (await bot_name()).casefold():
        return True
    if command == "start":
        return True
    try:
        if command == "admin":
            await receive_report(message, group, arguments)
        else:
            if not message.from_user or message.sender_chat:
                return True
            try:
                actor = await bot.get_chat_member(group["id"], message.from_user.id)
            except (TelegramBadRequest, TelegramForbiddenError):
                return True
            if actor.status not in ADMIN_STATUSES:
                return True
            await require_admin(group["id"], message.from_user.id, "can_restrict_members")
            target, duration_text = await resolve_target(message, arguments)
            if command == "kick" and duration_text:
                raise ValueError("У /kick нет срока. Используйте /ban для временного бана.")
            duration = parse_duration(duration_text) if command != "kick" else None
            await moderate(group, message.from_user.id, target, command, duration,
                           source=message.reply_to_message)
            if command == "ban" and str(target) in group["reports"]:
                group["reports"].pop(str(target), None)
                await STORE.save(group, urgent=True)
            verb = {"ban": "Заблокирован", "mute": "Ограничен в отправке сообщений", "kick": "Удалён из группы"}[command]
            suffix = "" if command == "kick" else (f" на {duration} сек." if duration else " бессрочно")
            await group_reply(message, f"✅ {user_link(target, 'Участник')} — {verb.lower()}{suffix}.")
    except ValueError as exc:
        await group_reply(message, "⚠️ " + esc(exc))
    except (TelegramBadRequest, TelegramForbiddenError):
        await group_reply(message, "⚠️ Не удалось выполнить действие. Проверьте права бота и тип группы.")
    return True


async def discover_group(chat, added_by=None):
    gid = chat.id
    group = STORE.groups.get(gid)
    if not group:
        group = fresh_group(gid, chat.title)
        STORE.groups[gid] = group
    group["active"] = True
    group["title"] = clip(chat.title or str(gid), 128)
    group["username"] = getattr(chat, "username", None)
    if added_by and not group.get("added_by"):
        group["added_by"] = added_by
    try:
        await sync_admins(group)
        group["member_count"] = await bot.get_chat_member_count(gid)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    await STORE.save(group)
    schedule_welcome(gid)
    return group


async def refresh_open_lists(group):
    for uid_text in group.get("admins", {}):
        uid = int(uid_text)
        state = ub.USER_DATA.get(uid)
        if not state or state.get("state") != "GUARD_LIST" or hidden(group, uid):
            continue
        async with state.setdefault("ui_lock", asyncio.Lock()):
            if state.get("state") == "GUARD_LIST":
                token = ub.UI_ACTION_TASK.set(None)
                try:
                    await render_groups(uid, state.get("guard_page", 0))
                finally:
                    ub.UI_ACTION_TASK.reset(token)


def schedule_welcome(gid):
    group = STORE.groups.get(gid)
    current = WELCOME_TASKS.get(gid)
    if group and group.get("active") and group.get("welcome_pending") and (not current or current.done()):
        task = spawn(welcome_when_ready(gid))
        WELCOME_TASKS[gid] = task


async def welcome_when_ready(gid):
    while STORE.groups.get(gid, {}).get("active"):
        group = STORE.groups[gid]
        if not group.get("welcome_pending"):
            return
        try:
            own = await bot.get_chat_member(gid, bot.id)
            if own.status == "administrator" and time.time() >= group.get("joined_at", 0) + 30:
                await bot.send_message(gid, "<b>Всем привет! Я Qwitty 🛡</b>\n\n"
                                       "Спасибо, что добавили меня в группу. Помогаю администраторам удалять спам, "
                                       "опасные файлы и поддерживать порядок.\n\n"
                                       "<b>Заметили нарушение?</b> Ответьте на сообщение участника:\n"
                                       "<code>/admin причина</code>\n<i>Причину можно не указывать. Сообщение останется в чате, "
                                       "а жалоба попадёт администрации.</i>",
                                       parse_mode="HTML")
                async with lock_for(gid):
                    group["welcome_pending"] = False
                    await STORE.save(group, urgent=True)
                return
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after, 60))
        except (TelegramForbiddenError, TelegramBadRequest):
            return
        await asyncio.sleep(15)


@dp.my_chat_member(F.chat.type.in_(GROUP_TYPES))
async def bot_membership(event: types.ChatMemberUpdated):
    if not STORE.ready:
        return
    gid = event.chat.id
    async with lock_for(gid):
        new = event.new_chat_member.status
        if new in {"left", "kicked"}:
            group = STORE.groups.get(gid)
            if group:
                group["active"] = False
                await STORE.save(group, urgent=True)
            task = WELCOME_TASKS.pop(gid, None)
            if task:
                task.cancel()
            return
        if new not in PRESENT_STATUSES:
            return
        was_absent = event.old_chat_member.status in {"left", "kicked"}
        group = await discover_group(event.chat, event.from_user.id if was_absent else None)
        if was_absent:
            group.update(joined_at=time.time(), welcome_pending=True)
            await STORE.save(group)
        ADMIN_CACHE.pop(gid, None)
    await refresh_open_lists(group)


RAID_THRESHOLD = 50
RAID_SECONDS = 60
RAID_RUNNING = set()


def member_present(member):
    return member.status in {"creator", "administrator", "member"} or (
        member.status == "restricted" and bool(getattr(member, "is_member", False)))


def close_raid_window(group, now):
    window = group.get("raid_window", {})
    due = window.get("due")
    if not due or now < due:
        return False
    pending = group.setdefault("raid_pending", {})
    for uid in window.get("members", {}):
        pending.setdefault(uid, {"joined": window["members"][uid], "retry_at": 0})
    group["last_raid"] = {"at": due, "count": len(window.get("members", {}))}
    group["raid_window"] = {"members": {}, "due": None}
    return True


async def record_raid_join(group, user, joined_at=None):
    """Called with the group lock held. Count each account once per window."""
    if not group.get("active") or not group.get("settings", {}).get("raid") or user.id == bot.id:
        return
    stamp = float(joined_at if joined_at is not None else time.time())
    uid = str(user.id)
    seen = group.setdefault("raid_seen", {})
    if uid in seen and abs(stamp - float(seen[uid])) < RAID_SECONDS:
        return                                                         
    closed = close_raid_window(group, stamp)
    window = group.setdefault("raid_window", {"members": {}, "due": None})
    members = window.setdefault("members", {})
    if not window.get("due"):
        members = {k: v for k, v in members.items() if stamp - float(v) < RAID_SECONDS}
        window["members"] = members
    members[uid] = stamp
    seen[uid] = stamp
    group["raid_seen"] = {k: v for k, v in seen.items() if stamp - float(v) < RAID_SECONDS * 2}
    if len(members) >= RAID_THRESHOLD and not window.get("due"):
        window["due"] = min(members.values()) + RAID_SECONDS
    await STORE.save(group, urgent=closed or bool(window.get("due")))


async def process_raid(gid):
    try:
        async with lock_for(gid):
            group = STORE.groups.get(gid)
            if not group or not group.get("active") or not group.get("settings", {}).get("raid"):
                return
            if close_raid_window(group, time.time()):
                await STORE.save(group, urgent=True)
            targets = list(group.get("raid_pending", {}))
        for uid in targets:
            async with lock_for(gid):
                group = STORE.groups.get(gid)
                if not group or not group.get("active") or not group.get("settings", {}).get("raid"):
                    return
                entry = group.get("raid_pending", {}).get(uid)
                if not entry or float(entry.get("retry_at", 0)) > time.time():
                    continue
                try:
                    member = await bot.get_chat_member(gid, int(uid))
                                                                                       
                    if member.status not in ADMIN_STATUSES and int(uid) != bot.id:
                        await bot.ban_chat_member(gid, int(uid))
                        await record_local_ban(group, member.user)
                    group["raid_pending"].pop(uid, None)
                except TelegramRetryAfter as exc:
                    entry["retry_at"] = time.time() + exc.retry_after + 1
                    await STORE.save(group, urgent=True)
                    return
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    entry["retry_at"] = time.time() + 60
                    entry["error"] = type(exc).__name__
                except Exception as exc:
                    entry["retry_at"] = time.time() + 15
                    entry["error"] = type(exc).__name__
                await STORE.save(group, urgent=True)
            await asyncio.sleep(0.05)
    finally:
        RAID_RUNNING.discard(gid)


async def raid_worker():
    while True:
        now = time.time()
        for gid, group in list(STORE.groups.items()):
            if gid in RAID_RUNNING or not group.get("active") or not group.get("settings", {}).get("raid"):
                continue
            due = group.get("raid_window", {}).get("due")
            has_pending = any(float(v.get("retry_at", 0)) <= now for v in group.get("raid_pending", {}).values())
            if (due and due <= now) or has_pending:
                RAID_RUNNING.add(gid)
                spawn(process_raid(gid))
        await asyncio.sleep(0.5)


@dp.chat_member(F.chat.type.in_(GROUP_TYPES))
async def admin_membership(event: types.ChatMemberUpdated):
    if not STORE.ready:
        return
    gid = event.chat.id
    group = STORE.groups.get(gid)
    if not group:
        async with lock_for(gid):
            group = STORE.groups.get(gid) or await discover_group(event.chat)
    elif not group.get("active") and member_present(event.new_chat_member):
        async with lock_for(gid):
            group = await discover_group(event.chat)
    member = event.new_chat_member
    async with lock_for(gid):
        target = str(member.user.id)
        if member.status == "kicked":
            if time.monotonic() >= KICK_EVENTS.get((gid, member.user.id), 0):
                until = getattr(member, "until_date", None)
                until = int(until.timestamp()) if isinstance(until, dt.datetime) else int(until or 0)
                await record_local_ban(group, member.user, until)
        elif target in group.get("local_bans", {}):
            group["local_bans"].pop(target, None)
            await STORE.save(group, urgent=True)
    if not member_present(event.old_chat_member) and member_present(member) and member.status not in ADMIN_STATUSES:
        async with lock_for(gid):
            await record_raid_join(group, member.user, event.date.timestamp())
    remember_member(gid, member.user)
    if is_global_banned(member.user.id) and member_present(member) and member.status not in ADMIN_STATUSES:
        await enforce_global_ban_in_group(group, member.user.id, member.user, announce=True, joining=True)
        return
    if event.old_chat_member.status in ADMIN_STATUSES or member.status in ADMIN_STATUSES:
        async with lock_for(gid):
            ADMIN_CACHE.pop(gid, None)
            await sync_admins(group)
        await refresh_open_lists(group)


async def migrate_group(old_id, new_id, title):
    if old_id == new_id:
        return
    async with lock_for(min(old_id, new_id)):
        async with lock_for(max(old_id, new_id)):
            old = STORE.groups.get(old_id)
            if not old or old.get("migrated_to") == new_id:
                return
            copied = copy.deepcopy(old)
            copied.update(id=new_id, title=title or old["title"], active=True)
            old.update(active=False, migrated_to=new_id, reports={}, local_bans={}, dedup={})
            await STORE.save(copied, urgent=True)
            await STORE.save(old, urgent=True)
            ADMIN_CACHE.pop(old_id, None)
    schedule_welcome(new_id)
    await refresh_open_lists(copied)


async def touch_group_activity(group, stamp):
    stamp = float(stamp)
    if stamp <= float(group.get("last_activity_at", 0)):
        return
    group["last_activity_at"] = stamp
    await STORE.save(group, defer_remote=True)


@dp.message(F.chat.type.in_(GROUP_TYPES))
async def group_message(message: types.Message):
    if not STORE.ready:
        return
    gid = message.chat.id
    received_at = time.monotonic()
    try:
        if message.migrate_to_chat_id:
            await migrate_group(gid, message.migrate_to_chat_id, message.chat.title)
            return
        if message.migrate_from_chat_id:
            await migrate_group(message.migrate_from_chat_id, gid, message.chat.title)
            return
        async with lock_for(gid):
            group = STORE.groups.get(gid)
            if not group or not group.get("active"):
                group = await discover_group(message.chat)
                spawn(refresh_open_lists(group))
            if not group.get("active"):
                return
            if group["title"] != message.chat.title:
                group["title"] = clip(message.chat.title, 128)
                await STORE.save(group)
            await touch_group_activity(group, message.date.timestamp())
            remember_member(gid, message.from_user)
            if message.from_user and not message.from_user.is_bot and is_global_banned(message.from_user.id):
                if await enforce_global_ban_in_group(group, message.from_user.id, message.from_user, announce=True):
                    return
            if message.reply_to_message:
                remember_member(gid, message.reply_to_message.from_user)
            for joined in message.new_chat_members or []:
                remember_member(gid, joined)
                await record_raid_join(group, joined, message.date.timestamp())
                if not joined.is_bot and is_global_banned(joined.id):
                    queue_global_ban(gid, joined.id, joining=True)
                    spawn(enforce_global_ban_in_group(group, joined.id, joined, announce=True, joining=True))
            if any(getattr(message, field, None) for field in SERVICE_FIELDS):
                if group["settings"].get("service"):
                    await bot.delete_message(gid, message.message_id)
                return
            uid = message.from_user.id if message.from_user else 0
            flooding = spam_flood(gid, uid, message.message_id, received_at)
            if group["settings"].get("spam") and flooding and message.from_user and not message.from_user.is_bot and not message.sender_chat:
                                                                                           
                await bot.delete_message(gid, message.message_id)
                return
            if await handle_group_command(message, group):
                return
            if group["settings"].get("files") and dangerous_file(message):
                await bot.delete_message(gid, message.message_id)
                return
    except TelegramRetryAfter as exc:
        logging.warning("Guard rate limit: %s seconds", exc.retry_after)
    except (TelegramBadRequest, TelegramForbiddenError):
        logging.info("Guard could not moderate group %s; check bot permissions", gid)
    except Exception as exc:
        logging.warning("Guard message %s: %s", gid, type(exc).__name__)


@dp.edited_message(F.chat.type.in_(GROUP_TYPES))
async def group_edited(message: types.Message):
                                                                            
    group = STORE.groups.get(message.chat.id)
    if not group or not group.get("active"):
        return
    try:
        if group["settings"].get("files") and dangerous_file(message):
            await bot.delete_message(message.chat.id, message.message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


async def writer_loop():
    prune_at = 0.0
    while True:
        try:
            await STORE.flush_members()
            if STORE.global_dirty and time.monotonic() >= STORE.global_due:
                await STORE.flush_global_bans()
            for gid in list(STORE.dirty):
                if time.monotonic() >= STORE.due.get(gid, 0):
                    await STORE.flush(gid)
                    await asyncio.sleep(1)
            if time.monotonic() >= prune_at:
                await STORE.sql(STORE._prune_members)
                for key, expires in list(KICK_EVENTS.items()):
                    if expires <= time.monotonic():
                        KICK_EVENTS.pop(key, None)
                for gid, group in list(STORE.groups.items()):
                    async with lock_for(gid):
                        cutoff = time.time() - REPORT_TTL
                        expired = []                                                     
                        dedup = {k: v for k, v in group["dedup"].items() if v > cutoff}
                        if expired or dedup != group["dedup"]:
                            for key in expired:
                                group["reports"].pop(key, None)
                            group["dedup"] = dedup
                            await STORE.save(group)
                prune_at = time.monotonic() + 3600
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.warning("Guard writer: %s", type(exc).__name__)
        await asyncio.sleep(2)


async def start_guard():
    await STORE.open()
    await bot_name()
    spawn(writer_loop())
    spawn(report_notification_worker())
    spawn(raid_worker())
    spawn(global_unban_worker())
    spawn(global_ban_worker())
    for uid, entry in STORE.global_bans.items():
        if not entry.get("unbanning"):
            for gid, group in STORE.groups.items():
                if group.get("active"):
                    queue_global_ban(gid, int(uid))
    for gid in STORE.groups:
        schedule_welcome(gid)


async def stop_guard():
    tasks = list(TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    TASKS.clear()
    WELCOME_TASKS.clear()
    RAID_RUNNING.clear()
    GLOBAL_UNBAN_RUNNING.clear()
    GLOBAL_BAN_RUNNING.clear()
    GLOBAL_BAN_PENDING.clear()
    GLOBAL_ENFORCEMENT_LOCKS.clear()
    KICK_EVENTS.clear()
    await STORE.flush_members()
    if STORE.global_dirty:
        try:
            await STORE.flush_global_bans()
        except Exception as exc:
            logging.warning("Guard global-ban shutdown flush: %s", type(exc).__name__)
    for gid in list(STORE.dirty):
        try:
            await STORE.flush(gid)
        except Exception as exc:
            logging.warning("Guard shutdown flush: %s", type(exc).__name__)
    STORE.ready = False
    if STORE.db:
        await STORE.sql(STORE.db.close)
        STORE.db = None
