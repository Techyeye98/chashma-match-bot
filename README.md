# Frame Matcher Bot 👓

<img src="branding/bot-avatar.png" width="120" align="right" alt="Frame Matcher bot avatar">

Try it on Telegram: [@ChashmaMatchBot](https://t.me/ChashmaMatchBot)

A Telegram bot that finds the Lenskart frames closest to the ones you like, within your budget, and alerts you when something changes.

## What it does

- **Find matches.** Send a Lenskart product link, or type specs like `50-20-145 136mm black square acetate`. The bot replies with the 5 closest frames, showing the difference for each (lens, bridge, temple, width, height, weight, shape, material, colour).
- **Live stock.** Every frame shows Lenskart's online stock count: ❌ out of stock, 🔥 10 or fewer left (buy soon), ⚠️ 11 to 50 left, ✅ more than 50.
- **Where to search.** Only the offer section (default), or all of Lenskart.
- **Budget.** Set a max price; only frames at or below it are shown.
- **Track frames.** Tap ⭐ under a result to track your own frame or any match (max 25 per user).
- **Auto alerts (daily at 12:00 AM and 1:00 PM by default):**
  - a tracked frame comes back in stock
  - a tracked frame's price drops
  - a tracked frame enters the offer section
  - a new frame lands in the offer section that looks like one you track
- **New offer frames.** A list of frames that joined the offer section in the last 7 days.

Each Telegram user gets their own budget, settings and tracked frames, so you can share the bot with anyone.

## Folder layout

```
FrameMatcher_Bot/
├── bot.py          # the whole bot (Python standard library only, nothing to install)
├── start-bot.bat   # double-click to run; restarts the bot if it crashes
├── .env            # your settings and bot token (private, never share)
├── .env.example    # template for .env
├── deploy/
│   ├── install.sh            # one-time VPS setup (Linux)
│   ├── run.sh                # run in foreground on Linux, for testing
│   └── framematcher.service  # systemd service template used by install.sh
├── data/
│   └── state.json  # users, tracked frames, cache (created automatically)
└── README.md
```

## Setup

1. Install Python 3.10 or newer.
2. In Telegram, open **@BotFather**, send `/newbot`, and copy the token it gives you.
3. Open `.env` (if it doesn't exist, copy `.env.example` as `.env`) and paste the token after `BOT_TOKEN=`.
4. Double-click `start-bot.bat`.
5. Send `/start` to your bot in Telegram. The keypad buttons will appear.

## Run on a VPS (24/7)

Works on any Ubuntu/Debian VPS. Needs Python 3.9+ (nothing else to install).

**1. Stop the bot on your PC first.** The same bot token can only run in one place; if both run, messages get lost.

**2. Copy the folder to the VPS** (from your PC, in the `Techyeye98` folder):

```
scp -i <your-key>.pem -r FrameMatcher_Bot <user>@<vps-ip>:~/
```

This also copies `.env` (token) and `data/` (users and tracked frames), so nothing is lost.

**3. Log in and run the installer:**

```
ssh -i <your-key>.pem <user>@<vps-ip>
cd ~/FrameMatcher_Bot
bash deploy/install.sh
```

The installer sets the bot up as a service: it starts on its own when the VPS reboots and restarts if it crashes.

**Useful commands on the VPS**

| Task | Command |
|---|---|
| Live logs | `sudo journalctl -u framematcher -f` |
| Restart (after changing code or `.env`) | `sudo systemctl restart framematcher` |
| Stop | `sudo systemctl stop framematcher` |
| Status | `sudo systemctl status framematcher` |

**Updating the code later:** copy the new `bot.py` only (never overwrite the VPS `data/` folder, it holds live user data), then restart:

```
scp -i <your-key>.pem FrameMatcher_Bot/bot.py <user>@<vps-ip>:~/FrameMatcher_Bot/
```

**Timezone:** check times are always India time (IST), even if the VPS clock is set to UTC or another country.

## Settings (`.env`)

| Key | Meaning | Default |
|---|---|---|
| `BOT_TOKEN` | Token from BotFather | required |
| `CHECK_TIMES` | Daily auto-check times, 24-hour **India time (IST)**, comma separated. The server's own timezone doesn't matter. | `00:00,13:00` |
| `REQUIRED_CHANNELS` | Channels users must join before using the bot. Public: `@username`; private: `-100id\|https://t.me/+invite`. Comma separated. The bot must be an admin in each channel. Empty = open to everyone. | empty |
| `ADMIN_IDS` | Telegram user IDs (comma separated) that get the 🛠 Admin panel: stats, bot health, user lookup, ban/unban, and broadcasts (any message type, test-send to yourself, target all / trackers / new users, send now or schedule). Send `/myid` to the bot to find your ID. | empty |
| `DEFAULT_BUDGET` | Starting max price for new users (₹) | `1200` |

## Keypad buttons

| Button | Action |
|---|---|
| 🔎 Frame dhoondo | How to send a link or specs |
| ⭐ Tracked frames | Tracked list with live stock and price, plus "find similar" and "remove" buttons |
| 💰 Budget | Pick a max price |
| 🌐 Kahan dhoondu | Offer section or all of Lenskart |
| 🆕 Naye offer frames | Frames added to the offer section in the last 7 days |
| 🔄 Abhi check karo | Check stock and price right now |
| 🔔 Alerts | Turn auto alerts on or off |
| ⚙️ Settings | Current settings |
| ❓ Help | Full guide |

## Preview changes with a test bot

Make a second bot in @BotFather just for testing, then double-click `test-bot.bat`. It uses its own settings (`.env.test`) and data (`data-test/`), so the live bot on the VPS keeps running. Never put the live bot's token in `.env.test`.

## Test without Telegram

```
python bot.py test <lenskart-link-or-specs> [budget] [offer|all]
```

Example:

```
python bot.py test "50-20-145 136mm black square" 800 offer
```

## Notes

- On Windows the bot only runs while the PC is on and the `start-bot.bat` window is open. Use the VPS setup above for 24/7.
- On the first run the bot only records the current offer list. New-frame alerts start from the next scheduled check.
- If the PC sleeps through a check time, that check runs when the PC wakes up. If the bot was closed at that time, it skips it and waits for the next check time.
- This is an independent project. It is not affiliated with, endorsed by or sponsored by Lenskart.
- It reads Lenskart's public website data. If Lenskart changes its site, the bot may need an update.
