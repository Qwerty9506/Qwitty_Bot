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
SPAM_WINDOW = 120
MAX_TARGETS = 500
MAX_GROUP_BYTES = 1024 * 1024
DANGEROUS_EXTENSIONS = frozenset("""
.exe .com .scr .msi .msix .msp .cpl .pif .gadget .application .appref-ms
.bat .cmd .ps1 .ps1xml .psc1 .psd1 .psm1 .vbs .vbe .wsf .wsh .hta .js .jse
.lnk .scf .apk .xapk .apks .apkm .aab .jar .jnlp .sh .bash .zsh .run .bin
.app .appimage .command .pkg .dmg .reg .inf .ins .isp .py .pyw .rb .pl .cgi
""".split())
MODES = {
    "spam": ("🚯АнтиСпам", "Удаляет повторяющиеся сообщения пользователя в течение 2 минут, даже если между ними были другие сообщения."),
    "files": ("⚠️АнтиВирус", "Удаляет потенциально опасные исполняемые файлы и скрипты по расширению. Это фильтр файлов, а не проверка содержимого антивирусом."),
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
SPAM = OrderedDict()
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
            "settings": {"spam": False, "files": False, "service": False},
            "admins": {}, "hidden": [], "seen": {}, "reports": {}, "dedup": {},
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
        # Never replace unknown remote settings with empty defaults after a network error.
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
        return {"format": GLOBAL_FORMAT, "id": GLOBAL_STATE_ID, "bans": copy.deepcopy(self.global_bans), "updated": time.time()}

    async def save_global_bans(self, urgent=False):
        payload = self._global_payload()
        await self.sql(self._write_meta, GLOBAL_META_KEY, json.dumps(payload, ensure_ascii=False), 1)
        self.global_dirty = True
        self.global_due = time.monotonic() if urgent else time.monotonic() + 2

    async def flush_global_bans(self):
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
            await self.sql(self._write_meta, GLOBAL_META_KEY, json.dumps(snapshot, ensure_ascii=False), 0)
            self.global_dirty = False
        else:
            self.global_due = time.monotonic() + 30
        return ok

    async def save(self, group, urgent=False):
        group["revision"] = int(group.get("revision", 0)) + 1
        encoded = json.dumps(group, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > MAX_GROUP_BYTES:
            raise ValueError("Достигнут лимит данных группы")
        gid = group["id"]
        await self.sql(self._write, gid, encoded, 1)
        self.groups[gid] = group
        self.dirty.add(gid)
        first = self.first_dirty.setdefault(gid, time.monotonic())
        self.due[gid] = min(first + 5, time.monotonic() + (0 if urgent else 2))

    async def flush(self, gid):
        group = self.groups.get(gid)
        if group is None:
            return True
        snapshot = copy.deepcopy(group)
        async with ub.DB_WRITE_SEMAPHORE:
            request = asyncio.create_task(asyncio.to_thread(ub.db_save_data, "config", str(gid), snapshot))
            try:
                ok = await asyncio.shield(request)
            except asyncio.CancelledError:
                await request
                raise
        async with lock_for(gid):
            if ok:
                self.errors.discard(gid)
                if group["revision"] == snapshot["revision"]:
                    await self.sql(self._write, gid, json.dumps(snapshot, ensure_ascii=False), 0)
                    self.dirty.discard(gid)
                    self.first_dirty.pop(gid, None)
                else:
                    self.due[gid] = time.monotonic() + 2
            else:
                self.errors.add(gid)
                self.due[gid] = time.monotonic() + 30
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
    # Fresh checks on EVERY action, including clicks on old notification buttons.
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


async def group_title_link(group):
    try:
        chat = await bot.get_chat(group["id"])
        if getattr(chat, "username", None):
            return f'<a href="https://t.me/{esc(chat.username)}">{esc(group["title"])}</a>'
        if getattr(chat, "invite_link", None):
            return f'<a href="{esc(chat.invite_link)}">{esc(group["title"])}</a>'
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    return esc(group["title"])


async def message_link(group, message_id, label="сообщение"):
    """Build a clickable link to a reported message whenever Telegram exposes one."""
    try:
        chat = await bot.get_chat(group["id"])
        username = getattr(chat, "username", None)
        if username:
            url = f"https://t.me/{username}/{int(message_id)}"
        else:
            raw = str(abs(int(group["id"])))
            if raw.startswith("100") and len(raw) > 3:
                url = f"https://t.me/c/{raw[3:]}/{int(message_id)}"
            else:
                url = f"tg://openmessage?chat_id={int(group['id'])}&message_id={int(message_id)}"
        return f'<a href="{esc(url)}">{esc(label)}</a>'
    except (TelegramBadRequest, TelegramForbiddenError, TypeError, ValueError):
        return esc(label)


def global_reports_rows():
    rows = []
    for group in STORE.groups.values():
        if not group.get("active"):
            continue
        for target, item in group.get("reports", {}).items():
            rows.append((group, int(target), item))
    rows.sort(key=lambda row: row[2].get("updated", 0), reverse=True)
    return rows


def is_global_banned(uid):
    return str(int(uid)) in STORE.global_bans


def global_ban_entry(uid):
    return STORE.global_bans.get(str(int(uid)))


def _global_reasons_for(uid):
    collected = []
    for group in STORE.groups.values():
        item = group.get("reports", {}).get(str(int(uid)))
        if not item:
            continue
        for reason in item.get("reasons", []):
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


async def enforce_global_ban_in_group(group, uid, user=None, announce=True):
    entry = global_ban_entry(uid)
    if not entry or not group or not group.get("active"):
        return False
    gid = group["id"]
    try:
        member = await bot.get_chat_member(gid, int(uid))
        if member.status in ADMIN_STATUSES:
            return False
        present = member.status == "member" or (member.status == "restricted" and getattr(member, "is_member", True))
        if not present:
            return False
        own = await bot.get_chat_member(gid, bot.id)
        if own.status != "administrator" or not getattr(own, "can_restrict_members", False):
            return False
        await bot.ban_chat_member(gid, int(uid), request_timeout=20)
        if announce:
            name = entry.get("name") or (getattr(user, "full_name", None) if user else None) or member.user.full_name or "Пользователь"
            try:
                await global_ban_notice(group, uid, name, entry.get("reasons") or ["Без описания"])
            except (TelegramBadRequest, TelegramForbiddenError):
                logging.info("Global-ban notice could not be sent to group %s", gid)
        return True
    except TelegramRetryAfter:
        raise
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


async def global_ban_user(uid):
    uid = int(uid)
    latest = None
    for group, target, item in global_reports_rows():
        if target == uid and (latest is None or item.get("updated", 0) > latest[1].get("updated", 0)):
            latest = (group, item)
    old = global_ban_entry(uid) or {}
    name = (latest[1].get("name") if latest else None) or old.get("name") or "Пользователь"
    username = (latest[1].get("username") if latest else None) or old.get("username")
    reasons = _global_reasons_for(uid)
    STORE.global_bans[str(uid)] = {
        "name": clip(name, 100),
        "username": username,
        "reasons": reasons[:3],
        "updated": time.time(),
    }
    await STORE.save_global_bans(urgent=True)
    await STORE.flush_global_bans()

    banned_count = 0
    for group in list(STORE.groups.values()):
        if not group.get("active"):
            continue
        try:
            applied = await enforce_global_ban_in_group(group, uid, announce=True)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after, 60))
            applied = await enforce_global_ban_in_group(group, uid, announce=True)
        if applied:
            banned_count += 1
        await asyncio.sleep(0.05)

    # Once the decision is global, close this user's pending reports everywhere.
    for group in list(STORE.groups.values()):
        if str(uid) not in group.get("reports", {}):
            continue
        async with lock_for(group["id"]):
            group.get("reports", {}).pop(str(uid), None)
            await STORE.save(group, urgent=True)
    return banned_count


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
        types.InlineKeyboardButton(text="📛АнтиНакрутка", callback_data=f"gg:soon:{gid}"),
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
    await show(uid, f"<b>{title}</b>\n👥 <b>{esc(group['title'])}</b>\n\n<i>{description}</i>\n\n<b>Статус:</b> {'Включён 🟢' if enabled else 'Выключен 🔴'}", builder)


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
    text = f"<b>ℹ️ Информация о группе</b>\n👥 <b>{esc(group['title'])}</b>\n\n<b>Участников:</b> {count}\n<b>Администраторы:</b>\n"
    # Telegram limits message length; show a bounded admin list.
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
    await show(uid, f"<b>📣 Жалобы пользователей</b>\n👥 <b>{esc(group['title'])}</b>\n\n"
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
    await show(uid, f"<b>📨 Жалобы</b>\n👥 <b>{esc(group['title'])}</b>" + ("\n\nОткрытых жалоб пока нет." if not rows else ""), builder)


async def report_text(group, target, item, notification=False):
    stamp = dt.datetime.fromtimestamp(item["updated"], dt.timezone(dt.timedelta(hours=5))).strftime("%d.%m.%Y %H:%M")
    username = "@" + item["username"] if item.get("username") else "Не указан"
    reasons = "\n".join("• " + esc(clip(r["text"], 240)) for r in reversed(item.get("reasons", [])[-3:]))
    group_link = await group_title_link(group)
    messages = item.get("messages", [])
    latest_message = await message_link(group, messages[-1]["message_id"]) if messages else "Нет ссылки"
    return (f"<b>{'📣 Новая жалоба' if notification else '📣 Жалобы на участника'}</b>\n"
            f"👥 <b>{group_link}</b>\n\n"
            f"<b>Пользователь:</b> {user_link(target, item['name'])}\n"
            f"<b>Юзернейм:</b> {esc(username)}\n<b>Число жалоб:</b> {item['count']}\n"
            f"<b>Жалоба:</b> {stamp} (UTC +5)\n"
            f"<b>Сообщение:</b> {latest_message}\n\n"
            f"<b>Последние причины:</b>\n{reasons or 'Без описания'}")


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
        await show(uid, f"<b>👋 Вы администратор группы {esc(group['title'])}</b>\n\n"
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
            permission = "can_delete_messages"
        group, _, _ = await require_admin(gid, uid, permission)
        if command not in {"ban", "nb", "ignore"} and not is_group_owner(group, uid):
            raise ValueError("Настройки этой группы доступны только тому, кто добавил Qwitty.")
        if command == "soon":
            await safe_answer(callback, "АнтиНакрутка ещё в разработке 🛠")
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
                await moderate(group, uid, target, "ban", None)
                group["reports"].pop(str(target), None)
                await STORE.save(group, urgent=True)
                if command == "nb":
                    try:
                        await callback.message.delete()
                    except TelegramBadRequest:
                        pass
                    # Notification clicks must not turn the notification into the main UI.
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
                await show(uid, f"<b>Убрать {esc(group['title'])} из вашего списка?</b>\n\n"
                           "Группа останется в группе. Уведомления о её жалобах вам приходить не будут.", builder)
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
    # The callback may already have been answered; display errors in the private UI.
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
    # Split all suffixes: catches invoice.exe.pdf as well as upper-case extensions.
    pieces = name.split(".")[1:]
    return any("." + part.rstrip(" .") in DANGEROUS_EXTENSIONS for part in pieces)


def message_signature(message):
    text = " ".join((message.text or message.caption or "").split()).casefold()
    media_id = ""
    for field in ("document", "sticker", "video", "animation", "audio", "voice", "video_note"):
        media = getattr(message, field, None)
        if media:
            media_id = field + ":" + str(media.file_unique_id)
            break
    if message.photo:
        media_id = "photo:" + message.photo[-1].file_unique_id
    if not text and not media_id:
        return None
    return hashlib.sha256((text + "\0" + media_id).encode()).hexdigest()


def spam_duplicate(gid, uid, mid, signature, now=None):
    if signature is None:
        return False
    now = time.monotonic() if now is None else now
    key = (gid, uid, signature)
    entry = SPAM.get(key)
    if not entry or now - entry[0] >= SPAM_WINDOW:
        entry = (now, 0, -1)
    if entry[2] == mid:
        return False
    count = entry[1] + 1
    SPAM[key] = (entry[0], count, mid)
    SPAM.move_to_end(key)
    while len(SPAM) > 10000:
        SPAM.popitem(last=False)
    return count >= 2


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
            # Admins can also be resolved without having recently posted.
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


async def moderate(group, actor, target, action, duration):
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
        await bot.ban_chat_member(gid, target, until_date=until, request_timeout=20)
    elif action == "kick":
        # In case unban fails, the kick expires automatically instead of leaving a permanent ban.
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
    # Re-read live permissions before each delivery. Hidden groups never send notifications.
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
            # Do not create private profiles for admins who have never started the bot.
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
    item["reasons"] = (item["reasons"] + [{"text": clip(reason.strip() or "Без описания", 240), "at": now}])[-3:]
    item["messages"] = (item["messages"] + [{"message_id": int(source.message_id), "at": now}])[-3:]
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
    # The reported message and /admin message are both left intact.
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
            await moderate(group, message.from_user.id, target, command, duration)
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


@dp.chat_member(F.chat.type.in_(GROUP_TYPES))
async def admin_membership(event: types.ChatMemberUpdated):
    if not STORE.ready:
        return
    gid = event.chat.id
    group = STORE.groups.get(gid)
    if not group:
        return
    member = event.new_chat_member
    remember_member(gid, member.user)
    if is_global_banned(member.user.id) and member.status in PRESENT_STATUSES and member.status not in ADMIN_STATUSES:
        await enforce_global_ban_in_group(group, member.user.id, member.user, announce=True)
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
            old.update(active=False, migrated_to=new_id, reports={}, dedup={})
            await STORE.save(copied, urgent=True)
            await STORE.save(old, urgent=True)
            ADMIN_CACHE.pop(old_id, None)
    schedule_welcome(new_id)
    await refresh_open_lists(copied)


@dp.message(F.chat.type.in_(GROUP_TYPES))
async def group_message(message: types.Message):
    if not STORE.ready:
        return
    gid = message.chat.id
    try:
        if message.migrate_to_chat_id:
            await migrate_group(gid, message.migrate_to_chat_id, message.chat.title)
            return
        if message.migrate_from_chat_id:
            await migrate_group(message.migrate_from_chat_id, gid, message.chat.title)
            return
        async with lock_for(gid):
            group = STORE.groups.get(gid)
            if not group:
                group = await discover_group(message.chat)
                spawn(refresh_open_lists(group))
            if not group.get("active"):
                return
            if group["title"] != message.chat.title:
                group["title"] = clip(message.chat.title, 128)
                await STORE.save(group)
            remember_member(gid, message.from_user)
            if message.from_user and not message.from_user.is_bot and is_global_banned(message.from_user.id):
                if await enforce_global_ban_in_group(group, message.from_user.id, message.from_user, announce=True):
                    return
            if message.reply_to_message:
                remember_member(gid, message.reply_to_message.from_user)
            for joined in message.new_chat_members or []:
                remember_member(gid, joined)
                if not joined.is_bot and is_global_banned(joined.id):
                    spawn(enforce_global_ban_in_group(group, joined.id, joined, announce=True))
            if any(getattr(message, field, None) for field in SERVICE_FIELDS):
                if group["settings"].get("service"):
                    await bot.delete_message(gid, message.message_id)
                return
            # See all observed messages when deciding whether a repeat is consecutive.
            signature = message_signature(message)
            uid = message.from_user.id if message.from_user else 0
            duplicate = spam_duplicate(gid, uid, message.message_id, signature)
            if await handle_group_command(message, group):
                return
            if group["settings"].get("files") and dangerous_file(message):
                await bot.delete_message(gid, message.message_id)
                return
            if group["settings"].get("spam") and duplicate and message.from_user and not message.from_user.is_bot and not message.sender_chat:
                # Administrators are exempt from text-spam checks; the file filter still applies.
                admins = await admin_ids(gid)
                if uid not in admins:
                    await bot.delete_message(gid, message.message_id)
    except TelegramRetryAfter as exc:
        logging.warning("Guard rate limit: %s seconds", exc.retry_after)
    except (TelegramBadRequest, TelegramForbiddenError):
        logging.info("Guard could not moderate group %s; check bot permissions", gid)
    except Exception as exc:
        logging.warning("Guard message %s: %s", gid, type(exc).__name__)


@dp.edited_message(F.chat.type.in_(GROUP_TYPES))
async def group_edited(message: types.Message):
    # Never re-execute commands/reports when a user edits a command message.
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
                for gid, group in list(STORE.groups.items()):
                    async with lock_for(gid):
                        cutoff = time.time() - REPORT_TTL
                        expired = [k for k, v in group["reports"].items() if v.get("updated", 0) < cutoff]
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
    for gid in STORE.groups:
        schedule_welcome(gid)


async def stop_guard():
    tasks = list(TASKS)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    TASKS.clear()
    WELCOME_TASKS.clear()
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
