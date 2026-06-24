import asyncio
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Location,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "906154697"))
CITY_NAME = os.getenv("CITY_NAME", "Tallinn")
MATCH_RADIUS_KM = float(os.getenv("MATCH_RADIUS_KM", "1.0"))
OPEN_TALK_TTL_MINUTES = int(os.getenv("OPEN_TALK_TTL_MINUTES", "30"))
DB_PATH = os.getenv("DB_PATH", "joinme.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Create .env from .env.example and add your token.")

router = Router()

INTERESTS = [
    ("coffee", "☕ Coffee"),
    ("walk", "🚶 Walk"),
    ("networking", "💼 Networking"),
    ("language", "🗣 Language exchange"),
    ("travel", "✈️ Travel"),
    ("study", "🎓 Study"),
]

pending_interest_selection: dict[int, set[str]] = {}
pending_feedback: dict[int, int] = {}  # user_id -> match_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dt_to_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS open_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                interests TEXT NOT NULL,
                available_until TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS waves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL,
                to_user_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS meetings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wave_id INTEGER NOT NULL,
                user1_id INTEGER NOT NULL,
                user2_id INTEGER NOT NULL,
                happened TEXT,
                rating INTEGER,
                created_at TEXT NOT NULL
            );
            """
        )
        await db.commit()


async def upsert_user(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
            """,
            (user.id, user.username, user.first_name, dt_to_str(utcnow())),
        )
        await db.commit()


def interests_keyboard(selected: Optional[set[str]] = None) -> InlineKeyboardMarkup:
    selected = selected or set()
    rows = []
    for key, label in INTERESTS:
        prefix = "✅ " if key in selected else ""
        rows.append([InlineKeyboardButton(text=prefix + label, callback_data=f"interest:{key}")])
    rows.append([InlineKeyboardButton(text="Continue ➜", callback_data="interest_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def request_location_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Share location", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await upsert_user(message)
    await message.answer(
        f"Welcome to JoinMe {CITY_NAME} Beta 👋\n\n"
        "This is a small experiment: find someone nearby for coffee, a walk, networking, travel talk, or language exchange.\n\n"
        "Commands:\n"
        "/open — become Open To Talk\n"
        "/nearby — see nearby people\n"
        "/cancel — hide yourself\n"
        "/stats — validation metrics"
    )


@router.message(Command("open"))
async def open_to_talk(message: Message) -> None:
    await upsert_user(message)
    pending_interest_selection[message.from_user.id] = set()
    await message.answer("What are you open to? Select one or more:", reply_markup=interests_keyboard())


@router.callback_query(F.data.startswith("interest:"))
async def select_interest(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    key = callback.data.split(":", 1)[1]
    selected = pending_interest_selection.setdefault(user_id, set())
    if key in selected:
        selected.remove(key)
    else:
        selected.add(key)
    await callback.message.edit_reply_markup(reply_markup=interests_keyboard(selected))
    await callback.answer()


@router.callback_query(F.data == "interest_done")
async def interests_done(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    selected = pending_interest_selection.get(user_id, set())
    if not selected:
        await callback.answer("Please select at least one interest.", show_alert=True)
        return
    await callback.message.answer(
        "Now share your location. It will not be shown exactly to other users; only distance is used.",
        reply_markup=request_location_keyboard(),
    )
    await callback.answer()


@router.message(F.location)
async def receive_location(message: Message) -> None:
    await upsert_user(message)
    user_id = message.from_user.id
    selected = pending_interest_selection.pop(user_id, None)
    if not selected:
        await message.answer("Use /open first, then share your location.", reply_markup=ReplyKeyboardRemove())
        return

    loc: Location = message.location
    until = utcnow() + timedelta(minutes=OPEN_TALK_TTL_MINUTES)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE open_sessions SET active=0 WHERE user_id=?", (user_id,))
        await db.execute(
            """
            INSERT INTO open_sessions(user_id, lat, lon, interests, available_until, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (user_id, loc.latitude, loc.longitude, ",".join(sorted(selected)), dt_to_str(until), dt_to_str(utcnow())),
        )
        await db.commit()

    await message.answer(
        f"🟢 You are Open To Talk for {OPEN_TALK_TTL_MINUTES} minutes.\n\nUse /nearby to find people nearby.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def get_latest_active_session(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT * FROM open_sessions
            WHERE user_id=? AND active=1 AND available_until > ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, dt_to_str(utcnow())),
        ) as cursor:
            return await cursor.fetchone()


@router.message(Command("nearby"))
async def nearby(message: Message) -> None:
    await upsert_user(message)
    user_id = message.from_user.id
    my_session = await get_latest_active_session(user_id)
    if not my_session:
        await message.answer("First become visible with /open.")
        return

    my_interests = set(my_session["interests"].split(","))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.*, u.username, u.first_name FROM open_sessions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.user_id != ? AND s.active=1 AND s.available_until > ?
            ORDER BY s.created_at DESC LIMIT 30
            """,
            (user_id, dt_to_str(utcnow())),
        ) as cursor:
            rows = await cursor.fetchall()

    matches = []
    for row in rows:
        dist = haversine_km(my_session["lat"], my_session["lon"], row["lat"], row["lon"])
        if dist <= MATCH_RADIUS_KM:
            other_interests = set(row["interests"].split(","))
            shared = my_interests & other_interests
            matches.append((dist, row, shared))

    if not matches:
        await message.answer("No nearby Open To Talk people right now. Try again later or invite 2–3 testers nearby.")
        return

    matches.sort(key=lambda x: (-len(x[2]), x[0]))
    for dist, row, shared in matches[:5]:
        labels = [label for key, label in INTERESTS if key in set(row["interests"].split(","))]
        shared_text = ", ".join([label for key, label in INTERESTS if key in shared]) or "No shared interests yet"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Wave 👋", callback_data=f"wave:{row['id']}:{row['user_id']}")]]
        )
        await message.answer(
            f"🟢 Someone is open to talk\n\n"
            f"Distance: ~{dist:.1f} km\n"
            f"Interests: {', '.join(labels)}\n"
            f"Shared: {shared_text}\n\n"
            "Want to wave?",
            reply_markup=keyboard,
        )


@router.callback_query(F.data.startswith("wave:"))
async def wave(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    session_id = int(parts[1])
    to_user_id = int(parts[2])
    from_user_id = callback.from_user.id
    if from_user_id == to_user_id:
        await callback.answer("You cannot wave to yourself.", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO waves(from_user_id, to_user_id, session_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (from_user_id, to_user_id, session_id, dt_to_str(utcnow())),
        )
        wave_id = cursor.lastrowid
        await db.commit()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Accept ✅", callback_data=f"accept:{wave_id}:{from_user_id}")],
            [InlineKeyboardButton(text="Decline ❌", callback_data=f"decline:{wave_id}:{from_user_id}")],
        ]
    )
    await bot.send_message(
        to_user_id,
        "👋 Someone nearby waved to you.\n\nIf you accept, the bot will introduce you by Telegram username.",
        reply_markup=keyboard,
    )
    await callback.message.answer("Wave sent 👋")
    await callback.answer()


@router.callback_query(F.data.startswith("decline:"))
async def decline(callback: CallbackQuery, bot: Bot) -> None:
    _, wave_id, from_user_id = callback.data.split(":")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE waves SET status='declined' WHERE id=?", (int(wave_id),))
        await db.commit()
    await bot.send_message(int(from_user_id), "Your wave was declined. No problem — try another time.")
    await callback.message.answer("Declined.")
    await callback.answer()


async def get_user_public_name(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT username, first_name FROM users WHERE user_id=?", (user_id,)) as cursor:
            row = await cursor.fetchone()
    if not row:
        return "a Telegram user"
    if row["username"]:
        return f"@{row['username']}"
    return row["first_name"] or "a Telegram user"


@router.callback_query(F.data.startswith("accept:"))
async def accept(callback: CallbackQuery, bot: Bot) -> None:
    _, wave_id, from_user_id = callback.data.split(":")
    wave_id = int(wave_id)
    from_user_id = int(from_user_id)
    to_user_id = callback.from_user.id

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE waves SET status='accepted' WHERE id=?", (wave_id,))
        cursor = await db.execute(
            "INSERT INTO meetings(wave_id, user1_id, user2_id, created_at) VALUES (?, ?, ?, ?)",
            (wave_id, from_user_id, to_user_id, dt_to_str(utcnow())),
        )
        meeting_id = cursor.lastrowid
        await db.commit()

    from_name = await get_user_public_name(from_user_id)
    to_name = await get_user_public_name(to_user_id)
    intro = (
        "✅ Wave accepted.\n\n"
        f"You can now connect:\n{from_name}\n{to_name}\n\n"
        "Suggestion: meet only in a public place. Coffee shop, campus common area, airport gate area, or coworking lounge."
    )
    await bot.send_message(from_user_id, intro)
    await bot.send_message(to_user_id, intro)
    await callback.message.answer("Accepted. Intro sent ✅")
    await callback.answer()

    # Schedule lightweight meeting feedback.
    await asyncio.sleep(30 * 60)
    feedback_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yes, we met ✅", callback_data=f"met:{meeting_id}:yes")],
            [InlineKeyboardButton(text="No ❌", callback_data=f"met:{meeting_id}:no")],
        ]
    )
    for uid in [from_user_id, to_user_id]:
        await bot.send_message(uid, "Did the meeting happen?", reply_markup=feedback_keyboard)


@router.callback_query(F.data.startswith("met:"))
async def met_feedback(callback: CallbackQuery) -> None:
    _, meeting_id, answer = callback.data.split(":")
    meeting_id = int(meeting_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE meetings SET happened=? WHERE id=?", (answer, meeting_id))
        await db.commit()
    if answer == "yes":
        pending_feedback[callback.from_user.id] = meeting_id
        await callback.message.answer("Great. Rate the meeting from 1 to 5 by sending a number.")
    else:
        await callback.message.answer("Thanks. This helps validate the idea.")
    await callback.answer()


@router.message(F.text.regexp(r"^[1-5]$"))
async def rating(message: Message) -> None:
    meeting_id = pending_feedback.pop(message.from_user.id, None)
    if not meeting_id:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE meetings SET rating=? WHERE id=?", (int(message.text), meeting_id))
        await db.commit()
    await message.answer("Thanks for the rating 🙏")


@router.message(Command("cancel"))
async def cancel(message: Message) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE open_sessions SET active=0 WHERE user_id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("You are no longer Open To Talk.")


@router.message(Command("stats"))
async def stats(message: Message) -> None:
     if message.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        counts = {}
        for name, query in {
            "users": "SELECT COUNT(*) FROM users",
            "open_sessions": "SELECT COUNT(*) FROM open_sessions",
            "active_now": "SELECT COUNT(*) FROM open_sessions WHERE active=1 AND available_until > datetime('now')",
            "waves": "SELECT COUNT(*) FROM waves",
            "accepted_waves": "SELECT COUNT(*) FROM waves WHERE status='accepted'",
            "meetings": "SELECT COUNT(*) FROM meetings",
            "confirmed_meetings": "SELECT COUNT(*) FROM meetings WHERE happened='yes'",
        }.items():
            async with db.execute(query) as cursor:
                counts[name] = (await cursor.fetchone())[0]
        async with db.execute("SELECT AVG(rating) FROM meetings WHERE rating IS NOT NULL") as cursor:
            avg = (await cursor.fetchone())[0]
    await message.answer(
        "📊 JoinMe Validation Metrics\n\n"
        f"Users: {counts['users']}\n"
        f"Open To Talk sessions: {counts['open_sessions']}\n"
        f"Active now: {counts['active_now']}\n"
        f"Waves sent: {counts['waves']}\n"
        f"Accepted waves: {counts['accepted_waves']}\n"
        f"Meetings created: {counts['meetings']}\n"
        f"Confirmed meetings: {counts['confirmed_meetings']}\n"
        f"Average rating: {avg or 'n/a'}"
    )


async def main() -> None:
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
