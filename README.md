# Conductor Telegram Bot

Python Telegram bot for the Conductor project.

## Files
- `bot.py` — bot source; uses polling locally and Telegram webhook on Render.
- `schedules.txt` — full route timetables shown in the bot.
- `requirements.txt` — Python dependencies.
- `.gitignore` — excludes local secrets and Python artifacts.

## Render settings
- Runtime: Python 3
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Service type: Web Service

Set these environment variables in Render (never commit them to GitHub):
- `BOT_TOKEN`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `ADMIN_IDS` (optional, comma-separated Telegram numeric IDs)

Render automatically provides `RENDER_EXTERNAL_HOSTNAME` and `PORT`.
