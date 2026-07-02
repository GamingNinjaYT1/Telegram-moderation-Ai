# Lunar AI Moderation Bot

A full-featured Telegram moderation bot. Every command runs through the
trigger word — nothing else gets a conversational reply from the bot.

**For exact step-by-step VPS setup, see [INSTALL.md](INSTALL.md).**

## Commands (reply to a user, then say `lunar <thing>`)

**User actions:**
- `lunar mute 10m` / `1h` / `1d` — timed mute (no duration = indefinite)
- `lunar unmute`
- `lunar ban` / `lunar unban`
- `lunar kick`
- `lunar warn` / `lunar unwarn` (3 warns = auto-ban)
- `lunar promote` / `lunar demote`
- `lunar pin` / `lunar unpin`
- `lunar approve` / `lunar unapprove` — exempt a user from filters/locks/antiflood
- `lunar info` — shows user ID, username, warn count, approval status
- `lunar report` — flags the replied-to message to admins and pings the owner

**Chat management** (no reply needed):
- `lunar filter <word> [custom reply]` — auto-delete messages containing that word
- `lunar unfilter <word>`
- `lunar lock <links/media/forward/stickers/gifs/usernames>` — auto-delete that content type
- `lunar unlock <type>`
- `lunar purge` (reply to a message) — deletes everything from that message to now
- `lunar setwelcome <text>` — use `{name}` for the new member's name
- `lunar setrules <text>` / `lunar rules`
- `lunar welcome` — shows current welcome message

**Notes:**
- `lunar save <name> <content>`
- `lunar note <name>`
- `lunar notes` — list saved note names

Slash-command equivalents (`/ban`, `/mute 10m`, etc.) still work too.

## Hindi / Hinglish abuse detection

Common Hindi/Hinglish abusive words — including short forms and light
leetspeak obfuscation ("g@ndu", "ch00t1y4", extra letters, spaced-out
spelling) — are matched by a local, no-API-call word list. A match triggers
an immediate **10-minute auto-mute** plus message deletion, no AI call
needed. Very short, ambiguous abbreviations ("mc", "bc") only count when
they appear as their own word, to avoid false positives on unrelated words.
This list covers common profanity, not slurs — extend `HINDI_ABUSE_WORDS`
in `bot.py` if you want broader coverage. If a message slips past this list
in some other language or obfuscation the AI NSFW/abuse check (below) still
gets a shot at it as a second layer.

## Toggleable features

Every automatic protection can be switched on/off per chat:

- `lunar disable <feature>` / `lunar enable <feature>`
- `lunar features` — shows current on/off state of everything

Feature names: `antiflood`, `ai_moderation` (covers both NSFW and abusive-
language detection), `hindi_abuse`, `welcome`, `locks`, `filters`. All
default to **on**. Toggles are per-chat and persist in the database.

## Help command

`/help` (and `lunar help`) only responds to admins and approved users
(`lunar approve`) — everyone else gets silence, not even an error, so the
command's existence isn't advertised to random members. `/start` stays open
to anyone as a minimal "I'm a mod bot, ask an admin" message.

## AI content moderation (NSFW / abuse detection)

When `GROQ_API_KEY` is set, every regular chat message (not "lunar" commands)
from a non-admin, non-approved user is checked by Groq for NSFW content or
targeted abusive language. Flagged messages are deleted automatically with
a short notice — no warning/ban is applied automatically, just removal, so
you can layer `lunar warn` on top yourself if you want escalation. Admins
are always exempt so normal moderation conversation isn't touched.

Note: this adds a Groq API call per non-admin message that passes a cheap
local pre-filter first — short greetings ("ok", "lol", "hi"), single emoji
reactions, and pure numbers/punctuation never reach the AI, so you're not
burning calls on filler chat. On Groq's free tier this is normally fine for
small-to-medium groups; watch your rate limits in busy chats — check
current limits at console.groq.com.

## What's automatic (no "lunar" needed)

Filters, locks, and antiflood run silently in the background — they delete
matching content or mute flooders the moment it happens, because that's
what those features are for. They're not conversational replies; the bot
doesn't chime in on regular chat otherwise. Everything an admin actively
*tells* the bot to do still requires saying `lunar` first.

- **Antiflood**: more than 5 messages in 10 seconds from one user (excluding
  admins/approved users) triggers a 10-minute auto-mute. Defaults are stored
  per-chat in the `settings` table if you want to tune them directly in
  the DB.
- **Welcome messages** post automatically when someone joins, if you've set
  one with `lunar setwelcome`.

AI (Groq) runs first whenever a `GROQ_API_KEY` is set — it interprets
everything typed after "lunar", including slang and crude phrasing like
"get this dude out of here", and maps it to the right action. Keyword
matching is the fallback if Groq is unavailable or doesn't return anything
confident.

## Owner panel

The user ID set as `OWNER_ID` (defaults to `7140576750`) gets special
treatment:

- **Bypasses the admin check everywhere the bot is admin.** You can reply
  `lunar ban`/`kick`/`mute`/etc. in any group the bot has been made admin
  in, even if Telegram doesn't list you as an admin of that specific group.
  (The bot itself still needs the relevant Telegram admin permissions in
  that chat for the action to actually succeed.)
- **Private commands** (owner-only, work in any chat including DM with the bot):
  - `/panel` — overview: chat count, today's action counts, command list
  - `/groups` — every chat the bot is currently admin in
  - `/log [n]` — last n moderation actions across all chats (default 10)
  - `/stats` — today's action counts
  - `/broadcast <text>` — sends a message to every chat the bot is admin in

The bot tracks which chats it's admin in automatically (via Telegram's
`my_chat_member` updates), so `/groups` and `/broadcast` stay in sync as
you add or remove it from groups — no manual config needed.

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in:
   - `BOT_TOKEN` — from @BotFather
   - `GROQ_API_KEY` — optional, enables AI fallback for slangy/fuzzy phrasing
     (get one free at console.groq.com). Without it, the bot still works
     using keyword matching only.
   - `GROQ_MODEL` — defaults to `llama-3.3-70b-versatile`; check
     console.groq.com/docs/models if you want a different one.
   - `OWNER_ID` — defaults to `7140576750`. This ID gets bypass + panel access
     (see below).

3. Give the bot admin rights in your group with these permissions at minimum:
   - Delete messages
   - Restrict members (needed for mute/unmute)
   - Ban users
   - **Add new admins** (needed for the `promote` command specifically —
     the bot can only grant permissions it itself has)

4. Run it:
   ```
   python bot.py
   ```

## Deploying on your VPS (systemd)

```bash
mkdir -p /root/lunarbot
# upload bot.py, requirements.txt, .env, lunarbot.service into /root/lunarbot
cd /root/lunarbot
pip install -r requirements.txt
cp lunarbot.service /etc/systemd/system/lunarbot.service
systemctl daemon-reload
systemctl enable --now lunarbot
journalctl -u lunarbot -f   # watch logs
```

## Notes

- Only chat admins can trigger any action (checked via `getChatMember`).
- Warns and an action log (who did what, to whom, when, via keyword or AI)
  are stored in a local SQLite file (`lunarbot.db`).
- `promote`/`demote` are gated the same as everything else — admin-only —
  but since this is the most sensitive action, consider tightening it
  further in `execute_action()` to a specific allowlist of trusted user IDs
  if you want extra safety.
- Duration parsing accepts `10s`, `10m`, `10h`, `10d` (and `min`/`hour`/`day`
  spelled out too).
