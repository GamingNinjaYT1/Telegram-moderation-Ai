"""
Lunar AI Moderation Bot
------------------------
A Telegram group moderation bot that understands natural-language admin
commands (e.g. "lunar mute him 10m", "lunar ban", "lunar promote him")
as well as classic slash commands (/mute, /ban, etc).

Fast path: keyword + regex matching for common phrasing (free, instant).
Fallback: Groq API classifies intent when keyword matching is unsure
(handles slangy/fuzzy phrasing like "get this dude outta here" or
"phok him" — maps to ban/kick/mute based on context).

Requires: aiogram 3.x, groq, python-dotenv
    pip install aiogram groq python-dotenv

Run:
    python bot.py
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command
from aiogram.types import ChatPermissions, Message
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # optional, enables AI parsing
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
TRIGGER_WORD = os.getenv("TRIGGER_WORD", "lunar").lower()
DB_PATH = os.getenv("DB_PATH", "lunarbot.db")
OWNER_ID = int(os.getenv("OWNER_ID", "7140576750"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("lunarbot")

# ---------------------------------------------------------------------------
# Database (warns + action log)
# ---------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS warns (
            chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER, actor_id INTEGER, target_id INTEGER,
            action TEXT, source TEXT, ts TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            is_admin INTEGER DEFAULT 0,
            updated_ts TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS filters (
            chat_id INTEGER, word TEXT, reply TEXT,
            PRIMARY KEY (chat_id, word)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS locks (
            chat_id INTEGER, lock_type TEXT,
            PRIMARY KEY (chat_id, lock_type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            chat_id INTEGER, name TEXT, content TEXT,
            PRIMARY KEY (chat_id, name)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            welcome_text TEXT,
            rules_text TEXT,
            antiflood_limit INTEGER DEFAULT 5,
            antiflood_window INTEGER DEFAULT 10
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS approved (
            chat_id INTEGER, user_id INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feature_toggles (
            chat_id INTEGER, feature TEXT, enabled INTEGER DEFAULT 1,
            PRIMARY KEY (chat_id, feature)
        )
    """)
    return conn

def upsert_chat(chat_id, title, is_admin_flag):
    conn = db()
    conn.execute(
        "INSERT INTO chats (chat_id, title, is_admin, updated_ts) VALUES (?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=?, is_admin=?, updated_ts=?",
        (chat_id, title, int(is_admin_flag), datetime.now(timezone.utc).isoformat(),
         title, int(is_admin_flag), datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

def list_admin_chats():
    conn = db()
    rows = conn.execute("SELECT chat_id, title FROM chats WHERE is_admin=1").fetchall()
    conn.close()
    return rows

def recent_actions(limit=10):
    conn = db()
    rows = conn.execute(
        "SELECT chat_id, actor_id, target_id, action, source, ts FROM action_log "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return rows

def action_counts_today():
    conn = db()
    today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        "SELECT action, COUNT(*) FROM action_log WHERE ts >= ? GROUP BY action", (today,)
    ).fetchall()
    conn.close()
    return dict(rows)

def log_action(chat_id, actor_id, target_id, action, source):
    conn = db()
    conn.execute(
        "INSERT INTO action_log (chat_id, actor_id, target_id, action, source, ts) VALUES (?,?,?,?,?,?)",
        (chat_id, actor_id, target_id, action, source, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    conn.close()

def get_warns(chat_id, user_id):
    conn = db()
    row = conn.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    conn.close()
    return row[0] if row else 0

def set_warns(chat_id, user_id, count):
    conn = db()
    conn.execute(
        "INSERT INTO warns (chat_id, user_id, count) VALUES (?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET count=?",
        (chat_id, user_id, count, count)
    )
    conn.commit()
    conn.close()

# --- filters -----------------------------------------------------------

def add_filter(chat_id, word, reply):
    conn = db()
    conn.execute(
        "INSERT INTO filters (chat_id, word, reply) VALUES (?,?,?) "
        "ON CONFLICT(chat_id, word) DO UPDATE SET reply=?",
        (chat_id, word.lower(), reply, reply)
    )
    conn.commit()
    conn.close()

def remove_filter(chat_id, word):
    conn = db()
    conn.execute("DELETE FROM filters WHERE chat_id=? AND word=?", (chat_id, word.lower()))
    conn.commit()
    conn.close()

def get_filters(chat_id):
    conn = db()
    rows = conn.execute("SELECT word, reply FROM filters WHERE chat_id=?", (chat_id,)).fetchall()
    conn.close()
    return rows

# --- locks ---------------------------------------------------------------

LOCK_TYPES = {"links", "media", "forward", "stickers", "gifs", "usernames"}

LOCK_ALIASES = {
    "link": "links", "links": "links", "url": "links", "urls": "links",
    "media": "media", "photo": "media", "photos": "media", "video": "media", "videos": "media",
    "forward": "forward", "forwards": "forward",
    "sticker": "stickers", "stickers": "stickers",
    "gif": "gifs", "gifs": "gifs",
    "username": "usernames", "usernames": "usernames", "mention": "usernames", "mentions": "usernames",
}

def set_lock(chat_id, lock_type, enabled):
    conn = db()
    if enabled:
        conn.execute(
            "INSERT OR IGNORE INTO locks (chat_id, lock_type) VALUES (?,?)",
            (chat_id, lock_type)
        )
    else:
        conn.execute("DELETE FROM locks WHERE chat_id=? AND lock_type=?", (chat_id, lock_type))
    conn.commit()
    conn.close()

def get_locks(chat_id):
    conn = db()
    rows = conn.execute("SELECT lock_type FROM locks WHERE chat_id=?", (chat_id,)).fetchall()
    conn.close()
    return {r[0] for r in rows}

# --- notes -----------------------------------------------------------------

def save_note(chat_id, name, content):
    conn = db()
    conn.execute(
        "INSERT INTO notes (chat_id, name, content) VALUES (?,?,?) "
        "ON CONFLICT(chat_id, name) DO UPDATE SET content=?",
        (chat_id, name.lower(), content, content)
    )
    conn.commit()
    conn.close()

def get_note(chat_id, name):
    conn = db()
    row = conn.execute("SELECT content FROM notes WHERE chat_id=? AND name=?", (chat_id, name.lower())).fetchone()
    conn.close()
    return row[0] if row else None

def list_notes(chat_id):
    conn = db()
    rows = conn.execute("SELECT name FROM notes WHERE chat_id=?", (chat_id,)).fetchall()
    conn.close()
    return [r[0] for r in rows]

# --- settings (welcome / rules / antiflood config) --------------------------

def get_settings(chat_id):
    conn = db()
    row = conn.execute(
        "SELECT welcome_text, rules_text, antiflood_limit, antiflood_window FROM settings WHERE chat_id=?",
        (chat_id,)
    ).fetchone()
    conn.close()
    if row:
        return {"welcome": row[0], "rules": row[1], "flood_limit": row[2], "flood_window": row[3]}
    return {"welcome": None, "rules": None, "flood_limit": 5, "flood_window": 10}

def set_setting(chat_id, field, value):
    conn = db()
    conn.execute(f"INSERT INTO settings (chat_id, {field}) VALUES (?,?) "
                 f"ON CONFLICT(chat_id) DO UPDATE SET {field}=?", (chat_id, value, value))
    conn.commit()
    conn.close()

# --- approved users (exempt from filters / locks / antiflood) ---------------

def approve_user(chat_id, user_id):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO approved (chat_id, user_id) VALUES (?,?)", (chat_id, user_id))
    conn.commit()
    conn.close()

def unapprove_user(chat_id, user_id):
    conn = db()
    conn.execute("DELETE FROM approved WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()

def is_approved(chat_id, user_id):
    conn = db()
    row = conn.execute("SELECT 1 FROM approved WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    conn.close()
    return row is not None

# --- feature toggles ---------------------------------------------------------
# Every automatic protection can be switched on/off per chat. Missing row = enabled (default on).

FEATURE_NAMES = {"antiflood", "ai_moderation", "hindi_abuse", "welcome", "locks", "filters"}

FEATURE_ALIASES = {
    "antiflood": "antiflood", "flood": "antiflood",
    "ai": "ai_moderation", "ai_moderation": "ai_moderation", "nsfw": "ai_moderation", "abuse": "ai_moderation",
    "hindi": "hindi_abuse", "hindi_abuse": "hindi_abuse", "hindiabuse": "hindi_abuse",
    "welcome": "welcome",
    "lock": "locks", "locks": "locks",
    "filter": "filters", "filters": "filters",
}

def normalize_feature(name: str):
    return FEATURE_ALIASES.get(name.strip().lower())

def is_feature_enabled(chat_id, feature) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT enabled FROM feature_toggles WHERE chat_id=? AND feature=?", (chat_id, feature)
    ).fetchone()
    conn.close()
    return True if row is None else bool(row[0])

def set_feature(chat_id, feature, enabled: bool):
    conn = db()
    conn.execute(
        "INSERT INTO feature_toggles (chat_id, feature, enabled) VALUES (?,?,?) "
        "ON CONFLICT(chat_id, feature) DO UPDATE SET enabled=?",
        (chat_id, feature, int(enabled), int(enabled))
    )
    conn.commit()
    conn.close()

def all_feature_states(chat_id):
    return {f: is_feature_enabled(chat_id, f) for f in sorted(FEATURE_NAMES)}

# ---------------------------------------------------------------------------
# Duration parsing: "10m", "2h", "1d", "30s", "10min", combos not needed
# ---------------------------------------------------------------------------

DURATION_RE = re.compile(r"(\d+)\s*(s|sec|second|m|min|minute|h|hr|hour|d|day)s?", re.I)

UNIT_SECONDS = {
    "s": 1, "sec": 1, "second": 1,
    "m": 60, "min": 60, "minute": 60,
    "h": 3600, "hr": 3600, "hour": 3600,
    "d": 86400, "day": 86400,
}

def parse_duration(text: str):
    """Returns timedelta or None if no duration found."""
    match = DURATION_RE.search(text)
    if not match:
        return None
    amount, unit = match.groups()
    seconds = int(amount) * UNIT_SECONDS[unit.lower()]
    return timedelta(seconds=seconds)

# ---------------------------------------------------------------------------
# Keyword-based fast intent matcher
# ---------------------------------------------------------------------------

KEYWORDS = {
    "ban":     {"ban", "banned", "yeet"},
    "unban":   {"unban"},
    "kick":    {"kick", "remove"},
    "mute":    {"mute", "silence", "shut"},
    "unmute":  {"unmute", "unsilence"},
    "warn":    {"warn"},
    "unwarn":  {"unwarn", "forgive"},
    "promote": {"promote", "mod", "makeadmin"},
    "demote":  {"demote", "unadmin", "unmod"},
    "pin":     {"pin"},
    "unpin":   {"unpin"},
    "purge":   {"purge", "clean"},
    "approve": {"approve", "trust"},
    "unapprove": {"unapprove", "untrust"},
    "info":    {"info", "whois"},
    "report":  {"report"},
    "filter":  {"filter"},
    "unfilter": {"unfilter", "stopfilter"},
    "lock":    {"lock"},
    "unlock":  {"unlock"},
    "setwelcome": {"setwelcome"},
    "setrules": {"setrules"},
    "rules":   {"rules"},
    "welcome": {"welcome"},
    "save":    {"save", "addnote"},
    "note":    {"note", "getnote"},
    "notes":   {"notes", "listnotes"},
    "help":    {"help", "commands"},
    "enable":  {"enable", "turnon"},
    "disable": {"disable", "turnoff"},
    "features": {"features", "toggles", "settings"},
}

# Actions that take a reply-to-user target (rest work on chat/text params instead)
TARGET_ACTIONS = {"ban", "unban", "kick", "mute", "unmute", "warn", "unwarn",
                   "promote", "demote", "pin", "unpin", "approve", "unapprove", "info"}

def fast_match(text: str):
    words = set(re.findall(r"[a-z]+", text.lower()))
    for action, triggers in KEYWORDS.items():
        if words & triggers:
            return action
    return None

# ---------------------------------------------------------------------------
# AI fallback intent parser (Groq)
# ---------------------------------------------------------------------------

_groq_client = None
if GROQ_API_KEY:
    from groq import Groq
    _groq_client = Groq(api_key=GROQ_API_KEY)

VALID_ACTIONS = set(KEYWORDS.keys()) | {"none"}

CLASSIFIER_SYSTEM_PROMPT = """You classify Telegram group-moderation commands from casual, slangy, or profanity-laced admin messages. Admins are telling you what to do, usually to a user they've replied to. Map their intent to exactly one action.

Actions: ban, unban, kick, mute, unmute, warn, unwarn, promote, demote, pin, unpin, purge, approve, unapprove, info, report, filter, unfilter, lock, unlock, setwelcome, setrules, rules, welcome, save, note, notes, help, enable, disable, features, none

Guidance:
- Harsh/slang phrases meaning "remove permanently" (e.g. "get this dude out of here", "yeet him", crude insults directed at removing someone, "he's done") -> ban
- Phrases meaning "remove but they can rejoin" (e.g. "kick him out", "boot him") -> kick
- Phrases meaning "shut them up" / "can't talk" (e.g. "shut him up", "make him quiet") -> mute
- Phrases meaning "let them talk again" -> unmute
- Phrases meaning "give them a heads up / strike" -> warn
- Phrases meaning "make them admin/mod" -> promote
- Phrases meaning "remove admin/mod" -> demote
- Phrases meaning "delete a bunch of recent messages" -> purge
- Phrases meaning "trust this user, skip filters" -> approve
- Phrases meaning "who is this / show their info" -> info
- Phrases meaning "flag this to admins" -> report
- Phrases meaning "block/ban a word or phrase from chat" -> filter
- Phrases meaning "stop blocking links/media/etc" -> unlock, "start blocking" -> lock
- If genuinely unclear or unrelated to moderation, use "none"

Reply with ONLY raw JSON, no markdown, no explanation:
{"action": "<one of the actions above>", "confidence": <0.0-1.0>}"""

async def ai_match(text: str):
    """Ask Groq to classify the moderation intent. Returns action string or None."""
    if not _groq_client:
        return None
    try:
        resp = await asyncio.to_thread(
            _groq_client.chat.completions.create,
            model=GROQ_MODEL,
            max_tokens=50,
            temperature=0,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": f"Message: {text!r}"},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
        data = json.loads(raw)
        if data.get("action") in VALID_ACTIONS and data.get("confidence", 0) >= 0.6:
            return data["action"] if data["action"] != "none" else None
    except Exception as e:
        log.warning(f"AI intent parse failed: {e}")
    return None

# ---------------------------------------------------------------------------
# Hindi / Hinglish abuse word detection — fast, local, no API call.
# Catches common transliterated/short-form abusive words even when written
# with number/symbol substitutions ("g@nd|1", "bh0sd1k3", spaced out, etc).
# This list covers common profanity, not slurs against protected groups —
# extend HINDI_ABUSE_WORDS below if you want to catch more.
# ---------------------------------------------------------------------------

HINDI_ABUSE_WORDS = {
    "bsdk", "bhosdike", "bhosdi", "madarchod", "behenchod",
    "chutiya", "chutiye", "chutiyapa", "gandu", "gaandu", "gand", "randi",
    "harami", "haramzada", "kutte", "kutta", "kamina", "kamine",
    "chodu", "lund", "loda", "lauda", "laude", "jhant",
    "jhantu", "chodna", "raand", "bhenchod", "madarjaat",
}

# Very short abbreviations ("mc", "bc") are too ambiguous to substring-match —
# they'd false-positive inside unrelated words. These only count as abuse
# when they appear as a standalone word.
HINDI_ABUSE_SHORT_TOKENS = {"mc", "bc"}

_LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a",
    "$": "s", "!": "i", "|": "l",
})

def _normalize_leet(text: str) -> str:
    text = text.lower().translate(_LEET_MAP)
    text = re.sub(r"[^a-z\s]", "", text)          # strip punctuation/digits left over
    text = re.sub(r"(.)\1{2,}", r"\1\1", text)      # collapse 3+ repeated letters ("chutiyaaaa" -> "chutiyaa")
    return text

def contains_hindi_abuse(text: str) -> bool:
    if not text:
        return False
    normalized = _normalize_leet(text)  # spaces preserved here

    # Short ambiguous tokens: must appear as their own word, not a substring.
    if set(normalized.split()) & HINDI_ABUSE_SHORT_TOKENS:
        return True

    # Longer, distinctive words: substring match is safe and also catches
    # obfuscation like "g a n d u" (spaces stripped before comparing).
    normalized_nospace = normalized.replace(" ", "")
    for word in HINDI_ABUSE_WORDS:
        if word.replace(" ", "") in normalized_nospace:
            return True
    return False

# ---------------------------------------------------------------------------
# AI content moderation — NSFW / abusive language detection (Groq)
# Runs on regular chat messages (not "lunar" commands). Admins are always
# exempt; approved users are exempt too, same as filters/locks/antiflood.
# ---------------------------------------------------------------------------

CONTENT_MODERATION_PROMPT = """You are a content moderation classifier for a Telegram group chat. \
Read the message and decide if it violates either category:

- "nsfw": sexually explicit content, solicitation, explicit descriptions of sexual acts
- "abusive": harassment, slurs, hate speech, or targeted insults meant to demean another person \
  (ordinary swearing/frustration NOT directed at a person is NOT abusive)

Reply with ONLY raw JSON, no markdown, no explanation:
{"nsfw": <true/false>, "abusive": <true/false>, "confidence": <0.0-1.0>}"""

# Cheap pre-filter to avoid spending a Groq call on messages that are almost
# certainly harmless — short greetings, single emojis, "ok", "lol", numbers,
# stickers-as-text, etc. Only messages that pass this get sent to the AI.
_SAFE_SHORT_WORDS = {
    "ok", "okay", "yes", "no", "lol", "lmao", "haha", "hi", "hey", "hello",
    "bye", "gm", "gn", "thanks", "thx", "ty", "yep", "nope", "sure", "cool",
    "nice", "true", "false", "same", "wow", "wtf", "brb", "np", "yo",
}

def needs_ai_check(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 8:
        return False
    letters_only = re.sub(r"[^a-zA-Z\s]", "", stripped).strip()
    if not letters_only:
        return False  # pure emoji/punctuation/numbers
    words = letters_only.lower().split()
    if len(words) <= 2 and all(w in _SAFE_SHORT_WORDS for w in words):
        return False
    return True

async def ai_content_check(text: str):
    """Returns ('nsfw'|'abusive', confidence) if flagged, else None."""
    if not _groq_client or not text or not needs_ai_check(text):
        return None
    try:
        resp = await asyncio.to_thread(
            _groq_client.chat.completions.create,
            model=GROQ_MODEL,
            max_tokens=50,
            temperature=0,
            messages=[
                {"role": "system", "content": CONTENT_MODERATION_PROMPT},
                {"role": "user", "content": text[:1000]},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
        data = json.loads(raw)
        confidence = data.get("confidence", 0)
        if confidence < 0.75:
            return None
        if data.get("nsfw"):
            return ("nsfw", confidence)
        if data.get("abusive"):
            return ("abusive", confidence)
    except Exception as e:
        log.warning(f"AI content check failed: {e}")
    return None

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# In-memory antiflood tracker: {(chat_id, user_id): [timestamps]}
_flood_tracker = defaultdict(list)

async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True  # owner can command in any chat the bot is admin in
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception:
        return False

@dp.my_chat_member()
async def track_chat_admin_status(event):
    """Keeps the `chats` table in sync with every chat the bot is (or stops being) admin in."""
    new_status = event.new_chat_member.status
    is_admin_now = new_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    title = event.chat.title or event.chat.full_name or str(event.chat.id)
    upsert_chat(event.chat.id, title, is_admin_now)
    log.info(f"Chat '{title}' ({event.chat.id}) admin status -> {is_admin_now}")

FULL_PERMS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_change_info=False,
    can_invite_users=True, can_pin_messages=False,
)
MUTED_PERMS = ChatPermissions(can_send_messages=False)

ADMIN_PERMS = dict(
    can_delete_messages=True, can_restrict_members=True, can_pin_messages=True,
    can_invite_users=True, can_manage_chat=True, can_promote_members=False,
)
NO_PERMS = {k: False for k in ADMIN_PERMS}

# ---------------------------------------------------------------------------
# Core action executor — shared by slash commands and natural language
# ---------------------------------------------------------------------------

async def execute_action(message: Message, action: str, duration: timedelta, source: str, params: str = ""):
    chat_id = message.chat.id

    if action == "help":
        if not (await is_admin(chat_id, message.from_user.id) or is_approved(chat_id, message.from_user.id)):
            return
        await message.reply(build_help_text())
        return

    if not await is_admin(chat_id, message.from_user.id):
        await message.reply("Only admins can use moderation commands.")
        return

    # --- actions that need a reply-to-user target ---
    if action in TARGET_ACTIONS or action in ("report",):
        if not message.reply_to_message:
            await message.reply("Reply to the user you want me to act on.")
            return
        target = message.reply_to_message.from_user
    else:
        target = None

    try:
        if action == "ban":
            await bot.ban_chat_member(chat_id, target.id)
            await message.reply(f"🔨 Banned {target.full_name}.")

        elif action == "unban":
            await bot.unban_chat_member(chat_id, target.id, only_if_banned=True)
            await message.reply(f"✅ Unbanned {target.full_name}.")

        elif action == "kick":
            await bot.ban_chat_member(chat_id, target.id)
            await bot.unban_chat_member(chat_id, target.id)  # ban+unban = kick
            await message.reply(f"👢 Kicked {target.full_name}.")

        elif action == "mute":
            until = (datetime.now(timezone.utc) + duration) if duration else None
            await bot.restrict_chat_member(
                chat_id, target.id, permissions=MUTED_PERMS,
                until_date=until
            )
            dur_str = f" for {format_duration(duration)}" if duration else " indefinitely"
            await message.reply(f"🔇 Muted {target.full_name}{dur_str}.")

        elif action == "unmute":
            await bot.restrict_chat_member(chat_id, target.id, permissions=FULL_PERMS)
            await message.reply(f"🔊 Unmuted {target.full_name}.")

        elif action == "warn":
            count = get_warns(chat_id, target.id) + 1
            set_warns(chat_id, target.id, count)
            if count >= 3:
                await bot.ban_chat_member(chat_id, target.id)
                set_warns(chat_id, target.id, 0)
                await message.reply(f"⚠️ {target.full_name} hit 3 warns → banned.")
            else:
                await message.reply(f"⚠️ Warned {target.full_name} ({count}/3).")

        elif action == "unwarn":
            count = max(0, get_warns(chat_id, target.id) - 1)
            set_warns(chat_id, target.id, count)
            await message.reply(f"↩️ Removed a warn from {target.full_name} ({count}/3).")

        elif action == "promote":
            await bot.promote_chat_member(chat_id, target.id, **ADMIN_PERMS)
            await message.reply(f"⭐ Promoted {target.full_name} to admin.")

        elif action == "demote":
            await bot.promote_chat_member(chat_id, target.id, **NO_PERMS)
            await message.reply(f"⬇️ Demoted {target.full_name}.")

        elif action == "pin":
            await bot.pin_chat_message(chat_id, message.reply_to_message.message_id)
            await message.reply("📌 Pinned.")

        elif action == "unpin":
            await bot.unpin_chat_message(chat_id, message.reply_to_message.message_id)
            await message.reply("📌 Unpinned.")

        elif action == "purge":
            if not message.reply_to_message:
                await message.reply("Reply to the message you want to purge from.")
                return
            start_id = message.reply_to_message.message_id
            end_id = message.message_id
            deleted = 0
            for mid in range(start_id, end_id + 1):
                try:
                    await bot.delete_message(chat_id, mid)
                    deleted += 1
                except Exception:
                    pass
            log_action(chat_id, message.from_user.id, 0, "purge", source)
            return  # message itself already deleted in the loop above

        elif action == "approve":
            approve_user(chat_id, target.id)
            await message.reply(f"✅ {target.full_name} is now approved — exempt from filters/locks/antiflood.")

        elif action == "unapprove":
            unapprove_user(chat_id, target.id)
            await message.reply(f"❌ {target.full_name} is no longer approved.")

        elif action == "info":
            warns = get_warns(chat_id, target.id)
            approved = "yes" if is_approved(chat_id, target.id) else "no"
            await message.reply(
                f"<b>User info</b>\n"
                f"Name: {target.full_name}\n"
                f"ID: <code>{target.id}</code>\n"
                f"Username: @{target.username if target.username else 'none'}\n"
                f"Warns: {warns}/3\n"
                f"Approved: {approved}"
            )

        elif action == "report":
            reporter = message.from_user
            await message.reply_to_message.reply(
                f"🚩 Reported by {reporter.full_name} — an admin will take a look."
            )
            # notify owner too
            try:
                await bot.send_message(
                    OWNER_ID,
                    f"🚩 Report in chat {chat_id}: {reporter.full_name} flagged a message from {target.full_name}."
                )
            except Exception:
                pass

        elif action == "filter":
            parts = params.split(maxsplit=1)
            if not parts:
                await message.reply("Usage: lunar filter <word> [reply text]")
                return
            word = parts[0]
            reply_text = parts[1] if len(parts) > 1 else "That word isn't allowed here."
            add_filter(chat_id, word, reply_text)
            await message.reply(f"🔒 Filtering \"{word}\" — messages containing it will be deleted.")

        elif action == "unfilter":
            if not params:
                await message.reply("Usage: lunar unfilter <word>")
                return
            remove_filter(chat_id, params.split()[0])
            await message.reply(f"🔓 Stopped filtering \"{params.split()[0]}\".")

        elif action == "lock":
            lock_type = LOCK_ALIASES.get(params.strip().lower())
            if not lock_type:
                await message.reply(f"Usage: lunar lock <{'/'.join(sorted(LOCK_TYPES))}>")
                return
            set_lock(chat_id, lock_type, True)
            await message.reply(f"🔒 Locked {lock_type} — matching content will be deleted.")

        elif action == "unlock":
            lock_type = LOCK_ALIASES.get(params.strip().lower())
            if not lock_type:
                await message.reply(f"Usage: lunar unlock <{'/'.join(sorted(LOCK_TYPES))}>")
                return
            set_lock(chat_id, lock_type, False)
            await message.reply(f"🔓 Unlocked {lock_type}.")

        elif action == "setwelcome":
            if not params:
                await message.reply("Usage: lunar setwelcome <text> (use {name} for the new member's name)")
                return
            set_setting(chat_id, "welcome_text", params)
            await message.reply("👋 Welcome message updated.")

        elif action == "setrules":
            if not params:
                await message.reply("Usage: lunar setrules <text>")
                return
            set_setting(chat_id, "rules_text", params)
            await message.reply("📜 Rules updated.")

        elif action == "rules":
            settings = get_settings(chat_id)
            await message.reply(settings["rules"] or "No rules set yet. Use: lunar setrules <text>")

        elif action == "welcome":
            settings = get_settings(chat_id)
            await message.reply(settings["welcome"] or "No welcome message set yet. Use: lunar setwelcome <text>")

        elif action == "save":
            parts = params.split(maxsplit=1)
            if len(parts) < 2:
                await message.reply("Usage: lunar save <name> <content>")
                return
            save_note(chat_id, parts[0], parts[1])
            await message.reply(f"📝 Saved note \"{parts[0]}\".")

        elif action == "note":
            if not params:
                await message.reply("Usage: lunar note <name>")
                return
            content = get_note(chat_id, params.split()[0])
            await message.reply(content or "No note with that name.")

        elif action == "notes":
            names = list_notes(chat_id)
            await message.reply("Saved notes: " + ", ".join(names) if names else "No notes saved yet.")

        elif action == "enable":
            feature = normalize_feature(params)
            if not feature:
                await message.reply(f"Usage: lunar enable <{'/'.join(sorted(FEATURE_NAMES))}>")
                return
            set_feature(chat_id, feature, True)
            await message.reply(f"✅ {feature} enabled.")

        elif action == "disable":
            feature = normalize_feature(params)
            if not feature:
                await message.reply(f"Usage: lunar disable <{'/'.join(sorted(FEATURE_NAMES))}>")
                return
            set_feature(chat_id, feature, False)
            await message.reply(f"⛔ {feature} disabled.")

        elif action == "features":
            states = all_feature_states(chat_id)
            lines = [f"{'✅' if v else '⛔'} {k}" for k, v in states.items()]
            await message.reply("<b>Feature toggles:</b>\n" + "\n".join(lines))

        else:
            return

        log_action(chat_id, message.from_user.id, target.id if target else 0, action, source)

    except Exception as e:
        await message.reply(f"Couldn't do that: {e}")

def format_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs % 86400 == 0:
        return f"{secs // 86400}d"
    if secs % 3600 == 0:
        return f"{secs // 3600}h"
    if secs % 60 == 0:
        return f"{secs // 60}m"
    return f"{secs}s"

# ---------------------------------------------------------------------------
# Slash commands (classic style)
# ---------------------------------------------------------------------------

SLASH_ACTIONS = ["ban", "unban", "kick", "mute", "unmute", "warn", "unwarn",
                  "promote", "demote", "pin", "unpin", "purge", "approve",
                  "unapprove", "info", "rules", "notes"]

for act in SLASH_ACTIONS:
    async def handler(message: Message, action=act):
        duration = parse_duration(message.text) if action == "mute" else None
        params = message.text.partition(" ")[2].strip()
        await execute_action(message, action, duration, source="slash", params=params)
    dp.message.register(handler, Command(act))

# ---------------------------------------------------------------------------
# Natural language trigger: "lunar <phrase>"
# ---------------------------------------------------------------------------

def strip_action_word(text: str, action: str) -> str:
    """Removes the first occurrence of the matched action keyword from text, leaving the params."""
    triggers = KEYWORDS.get(action, set())
    words = text.split()
    for i, w in enumerate(words):
        if re.sub(r"[^a-z]", "", w.lower()) in triggers:
            return " ".join(words[:i] + words[i+1:]).strip()
    return text.strip()

@dp.message(F.text.func(lambda t: t and t.lower().startswith(TRIGGER_WORD)))
async def natural_language_handler(message: Message):
    text = message.text[len(TRIGGER_WORD):].strip()
    if not text:
        return

    duration = parse_duration(text)
    action = None
    source = None

    if _groq_client:
        action = await ai_match(text)
        source = "ai"

    if not action:
        action = fast_match(text)
        source = "keyword" if action else None

    if not action:
        await message.reply(
            "Not sure what you meant. Try: mute / unmute / ban / kick / warn / promote / "
            "demote / pin / unpin / purge / approve / unapprove / info / report / filter / "
            "unfilter / lock / unlock / setwelcome / setrules / rules / save / note / notes / "
            "help / enable / disable / features."
        )
        return

    params = strip_action_word(text, action) if action not in TARGET_ACTIONS else ""
    await execute_action(message, action, duration, source=source, params=params)

# ---------------------------------------------------------------------------
# Utility commands
# ---------------------------------------------------------------------------

@dp.message(Command("warns"))
async def warns_cmd(message: Message):
    if not message.reply_to_message:
        await message.reply("Reply to a user to check their warns.")
        return
    target = message.reply_to_message.from_user
    count = get_warns(message.chat.id, target.id)
    await message.reply(f"{target.full_name} has {count}/3 warns.")

def build_help_text() -> str:
    return (
        "<b>Lunar Moderation Bot</b>\n\n"
        f"Everything runs through <code>{TRIGGER_WORD}</code> — reply to a user and say it "
        f"naturally: <code>{TRIGGER_WORD} mute 10m</code>, <code>{TRIGGER_WORD} ban</code>, "
        f"<code>{TRIGGER_WORD} get this dude out of here</code>, etc.\n\n"
        "<b>User actions</b> (reply to someone): ban, unban, kick, mute [10m/1h/1d], "
        "unmute, warn, unwarn, promote, demote, pin, unpin, approve, unapprove, info, report\n\n"
        "<b>Chat management</b>: filter &lt;word&gt; [reply], unfilter &lt;word&gt;, "
        "lock/unlock &lt;links/media/forward/stickers/gifs/usernames&gt;, purge (reply to a message), "
        "setwelcome &lt;text&gt;, setrules &lt;text&gt;, rules, welcome\n\n"
        "<b>Notes</b>: save &lt;name&gt; &lt;text&gt;, note &lt;name&gt;, notes\n\n"
        "<b>Toggles</b>: enable/disable &lt;antiflood/ai_moderation/hindi_abuse/welcome/locks/filters&gt;, "
        "features (shows current on/off state of everything)\n\n"
        "Filters, locks, antiflood, Hindi/Hinglish abuse detection, and AI NSFW/abuse "
        "detection all run automatically in the background for everyone except admins — "
        "they don't need the trigger word since they're protections, not commands. Each "
        "can be switched off per chat with <code>lunar disable &lt;feature&gt;</code>.\n\n"
        "This help is only visible to admins and approved users."
    )

async def can_use_help(message: Message) -> bool:
    if message.chat.type == "private":
        return message.from_user.id == OWNER_ID
    return (await is_admin(message.chat.id, message.from_user.id)
            or is_approved(message.chat.id, message.from_user.id))

@dp.message(Command("start"))
async def start_cmd(message: Message):
    await message.reply(
        "<b>Lunar Moderation Bot</b>\n\n"
        "I moderate this group. Admins and approved users can run /help to see everything I can do."
    )

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if not await can_use_help(message):
        return  # silently ignore — don't confirm/deny the command exists to non-admins
    await message.reply(build_help_text())

# ---------------------------------------------------------------------------
# Owner-only admin panel — works from any chat, including DM with the bot
# ---------------------------------------------------------------------------

def owner_only(func):
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != OWNER_ID:
            return  # silently ignore — don't reveal the panel exists to others
        return await func(message, *args, **kwargs)
    return wrapper

@dp.message(Command("panel"))
@owner_only
async def panel_cmd(message: Message):
    chats = list_admin_chats()
    counts = action_counts_today()
    counts_str = ", ".join(f"{k}: {v}" for k, v in counts.items()) or "none yet"
    await message.reply(
        "<b>🌙 Lunar Owner Panel</b>\n\n"
        f"Admin in <b>{len(chats)}</b> chat(s).\n"
        f"Actions today: {counts_str}\n\n"
        "Commands:\n"
        "/groups — list every chat I'm admin in\n"
        "/log [n] — recent moderation actions across all chats\n"
        "/stats — today's action counts\n"
        "/broadcast &lt;text&gt; — send a message to every chat I'm admin in\n\n"
        f"You bypass normal admin checks everywhere I'm an admin — "
        f"just reply to someone in any of those groups with "
        f"<code>{TRIGGER_WORD} ban/kick/mute/...</code>."
    )

@dp.message(Command("groups"))
@owner_only
async def groups_cmd(message: Message):
    chats = list_admin_chats()
    if not chats:
        await message.reply("Not admin in any tracked chats yet.")
        return
    lines = [f"• {title or chat_id} <code>({chat_id})</code>" for chat_id, title in chats]
    await message.reply(f"<b>Admin in {len(chats)} chat(s):</b>\n" + "\n".join(lines))

@dp.message(Command("log"))
@owner_only
async def log_cmd(message: Message):
    parts = message.text.split()
    limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
    rows = recent_actions(limit)
    if not rows:
        await message.reply("No actions logged yet.")
        return
    lines = []
    for chat_id, actor_id, target_id, action, source, ts in rows:
        lines.append(f"[{ts[:19]}] chat {chat_id}: {actor_id} → {action} → {target_id} ({source})")
    await message.reply("<b>Recent actions:</b>\n<code>" + "\n".join(lines) + "</code>")

@dp.message(Command("stats"))
@owner_only
async def stats_cmd(message: Message):
    counts = action_counts_today()
    if not counts:
        await message.reply("No actions logged today.")
        return
    lines = [f"{action}: {count}" for action, count in counts.items()]
    await message.reply("<b>Today's actions:</b>\n" + "\n".join(lines))

@dp.message(Command("broadcast"))
@owner_only
async def broadcast_cmd(message: Message):
    text = message.text.partition(" ")[2].strip()
    if not text:
        await message.reply("Usage: /broadcast your message here")
        return
    chats = list_admin_chats()
    sent, failed = 0, 0
    for chat_id, _title in chats:
        try:
            await bot.send_message(chat_id, text)
            sent += 1
        except Exception:
            failed += 1
    await message.reply(f"Broadcast sent to {sent} chat(s), failed in {failed}.")

# ---------------------------------------------------------------------------
# Automatic protections — these act silently on rule violations (delete /
# mute) and are NOT conversational replies. They don't require "lunar" to be
# typed because a moderation bot has to catch spam/banned content as it
# happens, not only when an admin manually flags it. Everything an admin
# *tells* the bot to do still requires the "lunar" trigger, per your request.
# ---------------------------------------------------------------------------

@dp.message(F.new_chat_members)
async def welcome_handler(message: Message):
    if not is_feature_enabled(message.chat.id, "welcome"):
        return
    settings = get_settings(message.chat.id)
    if not settings["welcome"]:
        return
    for member in message.new_chat_members:
        text = settings["welcome"].replace("{name}", member.full_name)
        await message.answer(text)

@dp.message(F.left_chat_member)
async def goodbye_handler(message: Message):
    # Intentionally silent by default — flip this on if you want goodbye messages.
    pass

@dp.message(F.text | F.photo | F.video | F.document | F.sticker | F.animation | F.forward_date)
async def passive_moderation(message: Message):
    # Skip anything already handled by command/lunar handlers above.
    if message.text and (message.text.startswith("/") or message.text.lower().startswith(TRIGGER_WORD)):
        return
    if not message.from_user or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # Admins and approved users are exempt from all automatic protections.
    if await is_admin(chat_id, user_id) or is_approved(chat_id, user_id):
        return

    try:
        # --- locks ---
        if is_feature_enabled(chat_id, "locks"):
            locks = get_locks(chat_id)
            if "links" in locks and message.text and re.search(r"https?://|t\.me/|www\.", message.text, re.I):
                await message.delete()
                return
            if "media" in locks and (message.photo or message.video or message.document):
                await message.delete()
                return
            if "forward" in locks and message.forward_date:
                await message.delete()
                return
            if "stickers" in locks and message.sticker:
                await message.delete()
                return
            if "gifs" in locks and message.animation:
                await message.delete()
                return
            if "usernames" in locks and message.text and re.search(r"@\w{4,}", message.text):
                await message.delete()
                return

        # --- word filters ---
        if is_feature_enabled(chat_id, "filters") and message.text:
            text_lower = message.text.lower()
            for word, reply_text in get_filters(chat_id):
                if word in text_lower:
                    await message.delete()
                    await message.answer(reply_text)
                    return

        # --- Hindi / Hinglish abuse — auto 10 min mute, no AI call needed ---
        if is_feature_enabled(chat_id, "hindi_abuse") and message.text and contains_hindi_abuse(message.text):
            await message.delete()
            await bot.restrict_chat_member(
                chat_id, user_id, permissions=MUTED_PERMS,
                until_date=datetime.now(timezone.utc) + timedelta(minutes=10)
            )
            await message.answer(f"🔇 {message.from_user.full_name} muted 10m — abusive language.")
            log_action(chat_id, 0, user_id, "hindi_abuse_mute", "auto")
            return

        # --- AI NSFW / abusive language detection (English/general) ---
        if is_feature_enabled(chat_id, "ai_moderation") and message.text:
            flagged = await ai_content_check(message.text)
            if flagged:
                category, confidence = flagged
                await message.delete()
                label = "NSFW content" if category == "nsfw" else "abusive language"
                await message.answer(f"⚠️ Removed a message from {message.from_user.full_name} — {label}.")
                log_action(chat_id, 0, user_id, f"ai_block_{category}", "auto")
                return

        # --- antiflood ---
        if is_feature_enabled(chat_id, "antiflood"):
            settings = get_settings(chat_id)
            now = time.time()
            key = (chat_id, user_id)
            _flood_tracker[key] = [t for t in _flood_tracker[key] if now - t < settings["flood_window"]]
            _flood_tracker[key].append(now)
            if len(_flood_tracker[key]) > settings["flood_limit"]:
                await bot.restrict_chat_member(
                    chat_id, user_id, permissions=MUTED_PERMS,
                    until_date=datetime.now(timezone.utc) + timedelta(minutes=10)
                )
                _flood_tracker[key] = []
                await message.answer(f"🔇 {message.from_user.full_name} muted 10m for flooding.")
                log_action(chat_id, 0, user_id, "antiflood_mute", "auto")

    except Exception as e:
        log.warning(f"Passive moderation error: {e}")

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing — set it in .env")
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY not set — AI fallback disabled, keyword matching only.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
