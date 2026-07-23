# Protection Bot

Telegram content-protection + auto-forward bot (Pyrogram + MongoDB).

## What it does

1. Admin uploads media into a private **target** channel (caption can include a `Topic : X` line).
2. Bot posts a "Watch Video" button into the linked **source** channel or group.
3. User taps the button → bot DMs them the protected media (`protect_content=True`, can't forward/save).
4. Full owner/admin panel: admin management, per-user daily limits, bans, broadcast, log channel, index generation.
5. **Group Topics support**: if the source is a Telegram group with Topics enabled, the button is posted into the correct topic thread automatically, based on the `Topic : X` line in the caption. Same topic name always reuses the same thread — never creates duplicates, never mixes topics — even if topics are interleaved out of order.

## Setup

1. Create a bot via [@BotFather](https://t.me/BotFather) → get `TELEGRAM_BOT_TOKEN`.
2. Get `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org.
3. Set `OWNER_ID` to your own numeric Telegram user ID.
4. Create a free MongoDB Atlas cluster → get `MONGO_URI`.
5. Copy `.env.example` to `.env` and fill in the values (for local runs only — see below).

```bash
pip install -r requirements.txt
python bot.py
```

## Deploying on Heroku

```bash
heroku create your-app-name
heroku config:set TELEGRAM_BOT_TOKEN=... TELEGRAM_API_ID=... TELEGRAM_API_HASH=... OWNER_ID=... MONGO_URI=...
git push heroku main
heroku ps:scale worker=1
```

`Procfile` already declares this as a `worker` process (not a web dyno) since the bot runs `pyrogram`'s long-polling client, not an HTTP server.

## Using Group Topics

1. In the target Telegram **group**, go to Group Settings → enable **Topics**.
2. Add the bot as admin in that group with the **"Manage Topics"** permission.
3. Link the group as a source in the bot's Channel Configuration, same as you would a channel — no extra config needed. The bot detects it's a forum group automatically and starts creating/reusing topics based on each video's `Topic : X` caption line.

## Caption format for topics

```
Topic : Hindi Grammar
Lecture 5 — Verbs (caption text can continue here)
```

Keep the topic name on its own `Topic :` line. If you need extra info on the same line, separate it with a dash/pipe/parenthesis — the bot strips anything after that when identifying the topic:

```
Topic : Hindi Grammar - Lecture 5   →  topic resolves to "Hindi Grammar"
```

No `Topic :` line at all → the video is grouped under an "Uncategorized" topic instead of guessing from random caption text.
