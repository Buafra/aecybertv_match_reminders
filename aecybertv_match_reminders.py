# -*- coding: utf-8 -*-
"""
AECyberTV — Auto Match Reminders Worker
Pulls today's fixtures for Spain, England, Italy, France, UAE, Saudi Arabia
and schedules reminders at 60m, 15m, and Kick-off (Asia/Dubai).

Commands:
  /start            - Help text
  /liveon           - Subscribe to reminders
  /liveoff          - Unsubscribe
  /today            - Quick digest of today's fixtures (no scheduling)
  /today_fixtures   - Pull & schedule today's reminders now
  /autoday_on       - Auto-pull daily @ 09:00 Dubai
  /autoday_off      - Stop auto-pull

Requirements:
  python-telegram-bot==21.4
  httpx>=0.27

Env:
  BOT_TOKEN=xxxxxxxx:yyyyyyyy
  APIFOOTBALL_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxx
  (optional) APIFOOTBALL_BASE=https://v3.football.api-sports.io
"""

import os
import logging
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ----------------------- CONFIG -----------------------
TZ = ZoneInfo("Asia/Dubai")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_URL = os.getenv("APIFOOTBALL_BASE", "https://v3.football.api-sports.io")
API_KEY  = os.getenv("APIFOOTBALL_KEY", "").strip()
HEADERS  = {"x-apisports-key": API_KEY}

# Countries (API uses "England", not "UK")
COUNTRIES = ["Spain", "England", "Italy", "France", "Saudi Arabia", "UAE"]

# Reminders offsets (minutes before KO); 0 = at KO  (keep hardcoded as you requested)
REMINDER_OFFSETS = [60, 15, 0]

# Daily pull time (Dubai)
DAILY_PULL_TIME = dtime(hour=9, minute=0, second=0, tzinfo=TZ)

# ----------------------- STATE ------------------------
# Replace with Redis/DB in production if you like
LEAGUES_CACHE: Dict[str, List[Dict[str, Any]]] = {}   # country -> leagues (current=true)
SUBSCRIBERS: set[int] = set()                         # chat IDs to broadcast to
SCHEDULED_KEYS: set[str] = set()                      # dedupe: "{fixture_id}:{offset}"

# ----------------------- LOGGING ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aecybertv.reminders")

# ----------------------- API -------------------------
async def api_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get(f"{BASE_URL}{path}", headers=HEADERS, params=params)
        r.raise_for_status()
        return r.json()

async def get_current_leagues_for_country(country: str) -> List[Dict[str, Any]]:
    """
    /leagues?country=<country>&current=true
    Returns list items with keys 'league', 'country', 'seasons'...
    """
    data = await api_get("/leagues", {"country": country, "current": "true"})
    leagues = [x for x in data.get("response", []) if x.get("league")]
    return leagues

async def ensure_leagues_cache() -> None:
    for c in COUNTRIES:
        if c not in LEAGUES_CACHE:
            try:
                LEAGUES_CACHE[c] = await get_current_leagues_for_country(c)
                log.info("Cached %d leagues for %s", len(LEAGUES_CACHE[c]), c)
            except Exception as e:
                log.warning("Failed to cache leagues for %s: %s", c, e)

async def get_today_fixtures_for_league(league_id: int, season: int, day: date) -> List[Dict[str, Any]]:
    """
    /fixtures?date=YYYY-MM-DD&league=<id>&season=<year>&timezone=Asia/Dubai
    """
    data = await api_get("/fixtures", {
        "date": day.strftime("%Y-%m-%d"),
        "league": league_id,
        "season": season,
        "timezone": "Asia/Dubai"
    })
    return data.get("response", [])

# --------------------- SCHEDULING ---------------------
def job_key(fixture_id: int, offset_min: int) -> str:
    return f"{fixture_id}:{offset_min}"

async def schedule_reminders_for_fixture(ctx: ContextTypes.DEFAULT_TYPE, fx: Dict[str, Any]) -> int:
    """
    Schedule reminders for a single fixture. Returns number of jobs scheduled (0..3).
    """
    try:
        iso = fx["fixture"]["date"]  # "2025-10-26T14:15:00+00:00" or "...Z"
        ko = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)
        home = fx["teams"]["home"]["name"]
        away = fx["teams"]["away"]["name"]
        league = fx["league"]["name"]
        fixture_id = fx["fixture"]["id"]
    except Exception:
        return 0

    scheduled = 0
    now = datetime.now(TZ)
    if ko <= now:
        return 0

    for mins in REMINDER_OFFSETS:
        when = ko - timedelta(minutes=mins)
        if when <= now:
            continue
        k = job_key(fixture_id, mins)
        if k in SCHEDULED_KEYS:
            continue
        SCHEDULED_KEYS.add(k)
        label = {60: "⏰ قبل ساعة", 15: "⏳ قبل 15 دقيقة", 0: "🏁 الانطلاقة"}.get(mins, f"-{mins}m")

        ctx.job_queue.run_once(
            send_reminder_job,
            when=when,
            data={
                "home": home, "away": away, "ko": ko,
                "league": league, "label": label,
                "fixture_id": fixture_id, "offset": mins
            },
            name=f"fx:{fixture_id}:{mins}"
        )
        scheduled += 1

    return scheduled

async def send_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    msg = (
        f"{d['label']}\n"
        f"⚽️ {d['home']} vs {d['away']}\n"
        f"🕕 {d['ko'].strftime('%I:%M %p').lstrip('0')} (بتوقيت دبي)\n"
        f"🏆 {d['league']}"
    )
    # Broadcast to all opted-in chats
    for chat_id in list(SUBSCRIBERS):
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            log.warning("send_reminder_job failed for %s: %s", chat_id, e)

# --------------------- WORKER TASK --------------------
async def pull_and_schedule(context: ContextTypes.DEFAULT_TYPE):
    """Main worker: cache leagues, pull today's fixtures, schedule reminders."""
    await ensure_leagues_cache()
    today = datetime.now(TZ).date()
    total_jobs = 0
    countries_touched = 0

    for country, leagues in LEAGUES_CACHE.items():
        countries_touched += 1
        for item in leagues:
            league = item.get("league", {})
            seasons = item.get("seasons") or []
            if not league or not seasons:
                continue
            # pick latest season year
            season = seasons[-1].get("year") or datetime.now(TZ).year
            league_id = league.get("id")
            if not league_id:
                continue

            try:
                fixtures = await get_today_fixtures_for_league(league_id, season, today)
            except Exception as e:
                log.warning("Fixtures fetch failed (%s, %s): %s", country, league.get("name"), e)
                continue

            for fx in fixtures:
                # Only schedule future kickoffs today
                try:
                    ko = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
                except Exception:
                    continue
                if ko.date() != today:
                    continue
                total_jobs += await schedule_reminders_for_fixture(context, fx)

    # Optional digest
    if SUBSCRIBERS and total_jobs > 0:
        digest = f"📅 تم جدولة تذكيرات اليوم ({countries_touched} دول) — إجمالي التذكيرات: {total_jobs}."
        for chat_id in list(SUBSCRIBERS):
            try:
                await context.bot.send_message(chat_id=chat_id, text=digest)
            except Exception as e:
                log.warning("Digest send failed to %s: %s", chat_id, e)

# ---------------------- COMMANDS ---------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحبًا! هذا العامل يرسل تذكيرات المباريات تلقائيًا.\n"
        "الأوامر:\n"
        "• /liveon — تفعيل استلام التذكيرات\n"
        "• /liveoff — إيقاف التذكيرات\n"
        "• /today — عرض ملخص مباريات اليوم (مختصر)\n"
        "• /today_fixtures — سحب وجدولة مباريات اليوم الآن\n"
        "• /autoday_on — تشغيل سحب وجدولة يومي 09:00 دبي\n"
        "• /autoday_off — إيقاف التشغيل اليومي"
    )

async def liveon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SUBSCRIBERS.add(update.effective_chat.id)
    await update.message.reply_text("✅ تم تفعيل التذكيرات لبطولات (إسبانيا، إنجلترا، إيطاليا، فرنسا، الإمارات، السعودية).")

async def liveoff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    SUBSCRIBERS.discard(update.effective_chat.id)
    await update.message.reply_text("🔕 تم إيقاف التذكيرات.")

async def autoday_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.job_queue.run_daily(pull_and_schedule, time=DAILY_PULL_TIME, name="autoday-pull")
    await update.message.reply_text("✅ تشغيل تلقائي يومي: 09:00 بتوقيت دبي.")

async def autoday_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for j in context.job_queue.jobs():
        if j.name == "autoday-pull":
            j.schedule_removal()
    await update.message.reply_text("⏸️ تم إيقاف التشغيل التلقائي اليومي.")

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick digest (no scheduling)."""
    await ensure_leagues_cache()
    today_d = datetime.now(TZ).date()
    lines = ["📅 مباريات اليوم (ملخص سريع):"]
    shown = 0

    for country, leagues in LEAGUES_CACHE.items():
        country_shown = False
        for item in leagues:
            league = item.get("league", {})
            seasons = item.get("seasons") or []
            if not league or not seasons:
                continue
            season = seasons[-1].get("year") or datetime.now(TZ).year
            league_id = league.get("id")
            try:
                fixtures = await get_today_fixtures_for_league(league_id, season, today_d)
            except Exception:
                continue
            for fx in fixtures:
                try:
                    ko = datetime.fromisoformat(fx["fixture"]["date"].replace("Z", "+00:00")).astimezone(TZ)
                except Exception:
                    continue
                if ko.date() != today_d:
                    continue
                if not country_shown:
                    lines.append(f"\n🌍 {country}")
                    country_shown = True
                home = fx["teams"]["home"]["name"]
                away = fx["teams"]["away"]["name"]
                lines.append(f"  🕕 {ko.strftime('%I:%M %p').lstrip('0')} — {home} vs {away} ({league.get('name','')})")
                shown += 1

    await update.message.reply_text("لا توجد مباريات اليوم ضمن النطاق المحدد." if shown == 0 else "\n".join(lines))

async def today_fixtures(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pull_and_schedule(context)
    await update.message.reply_text("✅ تم جلب مباريات اليوم وجدولة التذكيرات.")

# --------------------- APPLICATION -------------------
def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("liveon", liveon))
    app.add_handler(CommandHandler("liveoff", liveoff))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("today_fixtures", today_fixtures))
    app.add_handler(CommandHandler("autoday_on", autoday_on))
    app.add_handler(CommandHandler("autoday_off", autoday_off))

    # Safety: first pull a few seconds after boot (useful if you deploy midday)
    app.job_queue.run_once(pull_and_schedule, when=5, name="boot-pull")
    return app

if __name__ == "__main__":
    if not BOT_TOKEN or not API_KEY:
        raise SystemExit("Missing BOT_TOKEN or APIFOOTBALL_KEY in environment.")
    build_app(BOT_TOKEN).run_polling(drop_pending_updates=True)
