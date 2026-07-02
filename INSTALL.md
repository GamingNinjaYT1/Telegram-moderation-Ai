# Installing Lunar Moderation Bot on your Hostinger VPS

Exact commands, run as root over SSH. Replace anything in `<angle brackets>`.

## 1. Connect to your VPS

```bash
ssh root@<your-vps-ip>
```

## 2. Install system dependencies

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git
```

Check Python version (need 3.9+):
```bash
python3 --version
```

## 3. Create the bot's directory and upload the files

```bash
mkdir -p /root/lunarbot
cd /root/lunarbot
```

Upload `bot.py`, `requirements.txt`, `.env.example`, and `lunarbot.service` into
`/root/lunarbot`. Easiest way from your phone/Termux:

```bash
# from your local machine / Termux, not the VPS:
scp bot.py requirements.txt .env.example lunarbot.service root@<your-vps-ip>:/root/lunarbot/
```

Or `git clone` if you've pushed this to a GitHub repo (you already have
`GamingNinjaYT1/Telegram-moderation-bot` — you could add this bot there too).

## 4. Set up a virtual environment and install dependencies

```bash
cd /root/lunarbot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 5. Get your bot token

1. Open Telegram, message **@BotFather**
2. Send `/newbot` (or `/mybots` → your existing bot → API Token if you already have one)
3. Copy the token — looks like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`

## 6. Get your Groq API key

1. Go to https://console.groq.com
2. Sign up / log in, go to **API Keys**
3. Create a key — starts with `gsk_`

## 7. Create the .env file

```bash
cp .env.example .env
nano .env
```

Fill it in:
```
BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxxxxx
GROQ_MODEL=llama-3.3-70b-versatile
TRIGGER_WORD=lunar
DB_PATH=lunarbot.db
OWNER_ID=7140576750
```

Save and exit: `Ctrl+O`, `Enter`, `Ctrl+X`.

## 8. Test it manually first

```bash
cd /root/lunarbot
source venv/bin/activate
python3 bot.py
```

You should see log lines and no errors. In Telegram:
- Add the bot to a test group
- Promote it to admin with all permissions (especially **"Add new admins"** —
  required for the `promote` command)
- Send `/help` — if you're the owner or a group admin, you should get the
  command list back
- Reply to a test message with `lunar ban` (careful, this actually bans!)

Stop it with `Ctrl+C` once confirmed working.

## 9. Install it as a systemd service (so it runs permanently and restarts on crash/reboot)

Edit `lunarbot.service` if your paths differ, then:

```bash
cp lunarbot.service /etc/systemd/system/lunarbot.service
systemctl daemon-reload
systemctl enable lunarbot
systemctl start lunarbot
```

Check it's running:
```bash
systemctl status lunarbot
```

You should see `active (running)` in green.

## 10. Watch logs

```bash
journalctl -u lunarbot -f
```

`Ctrl+C` to stop watching (this does not stop the bot).

## 11. Common operations

```bash
systemctl restart lunarbot     # after editing bot.py or .env
systemctl stop lunarbot        # stop it
systemctl start lunarbot       # start it again
systemctl disable lunarbot     # stop it from auto-starting on reboot
```

## 12. Updating the bot later

```bash
cd /root/lunarbot
nano bot.py                    # or re-upload the new version
source venv/bin/activate
pip install -r requirements.txt   # only if requirements.txt changed
systemctl restart lunarbot
journalctl -u lunarbot -f      # confirm it came back up clean
```

## Troubleshooting

- **"Only admins can use moderation commands"** even though you're the
  owner: make sure `OWNER_ID` in `.env` matches your real Telegram user ID
  (7140576750 is already the default).
- **Promote doesn't work**: the bot needs **"Add new admins"** permission
  itself — check its admin permissions in the group's admin list.
- **AI features silently not working**: check `journalctl -u lunarbot -f`
  for `GROQ_API_KEY not set` or API errors — usually a typo'd key or hitting
  a rate limit.
- **Bot doesn't respond at all**: check `systemctl status lunarbot` — if
  it's not `active (running)`, check the logs for the exact Python error.
