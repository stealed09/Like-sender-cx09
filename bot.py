import asyncio
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
IST = ZoneInfo("Asia/Kolkata")

# ─── Database ────────────────────────────────────────────────────────────────

def db_conn():
    conn = sqlite3.connect("users.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            api_id          INTEGER,
            api_hash        TEXT,
            session         TEXT,
            phone           TEXT,
            password_2fa    TEXT,
            target_bot      TEXT DEFAULT 'FF_LikesGiver_Bot',
            msg_text        TEXT DEFAULT '/like 0000000000',
            task_active     INTEGER DEFAULT 0,
            next_run        TEXT DEFAULT NULL,
            retry_minutes   INTEGER DEFAULT 30,
            phone_code_hash TEXT DEFAULT NULL
        )""")
        # Add retry_minutes column if upgrading from old DB
        try:
            c.execute("ALTER TABLE users ADD COLUMN retry_minutes INTEGER DEFAULT 30")
        except:
            pass

init_db()

def get_user(uid):
    with db_conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def upsert_user(uid, **kwargs):
    with db_conn() as c:
        existing = c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            c.execute(f"UPDATE users SET {sets} WHERE user_id=?", (*kwargs.values(), uid))
        else:
            kwargs["user_id"] = uid
            cols = ", ".join(kwargs.keys())
            qs   = ", ".join("?" * len(kwargs))
            c.execute(f"INSERT INTO users ({cols}) VALUES ({qs})", tuple(kwargs.values()))

# ─── FSM States ──────────────────────────────────────────────────────────────

class LoginStates(StatesGroup):
    api_id    = State()
    api_hash  = State()
    phone     = State()
    otp       = State()
    password  = State()

class SetStates(StatesGroup):
    bot_username  = State()
    message_text  = State()
    retry_minutes = State()

# ─── Keyboards ───────────────────────────────────────────────────────────────

def main_kb(user):
    task_btn = ("⏹ Stop Task", "stop_task") if user and user["task_active"] else ("▶️ Start Task", "start_task")
    retry_min = user["retry_minutes"] if user and user["retry_minutes"] else 30
    rows = [
        [InlineKeyboardButton(text=task_btn[0], callback_data=task_btn[1])],
        [
            InlineKeyboardButton(text="🤖 Target Bot",   callback_data="set_bot"),
            InlineKeyboardButton(text="✉️ Message",       callback_data="set_msg"),
        ],
        [
            InlineKeyboardButton(text=f"⏱ Retry: {retry_min} min", callback_data="set_retry"),
            InlineKeyboardButton(text="📊 Status",        callback_data="status"),
        ],
    ]
    if not (user and user["session"]):
        rows.insert(0, [InlineKeyboardButton(text="🔑 Login with Telegram", callback_data="login")])
    else:
        rows.append([InlineKeyboardButton(text="🚪 Logout", callback_data="logout")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def retry_kb():
    options = [10, 20, 30, 45, 60, 90, 120]
    rows = []
    row = []
    for i, m in enumerate(options):
        row.append(InlineKeyboardButton(text=f"{m} min", callback_data=f"retry_{m}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Custom (type)", callback_data="retry_custom")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_retry")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")
    ]])

# ─── Bot & Dispatcher ────────────────────────────────────────────────────────

bot  = Bot(token=BOT_TOKEN)
dp   = Dispatcher(storage=MemoryStorage())
running_tasks: dict[int, asyncio.Task] = {}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_client(uid):
    u = get_user(uid)
    return TelegramClient(
        StringSession(u["session"]),
        int(u["api_id"]),
        u["api_hash"],
    )

def next_4am_ist():
    now = datetime.now(IST)
    target = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target.isoformat()

def seconds_until(iso_str):
    target = datetime.fromisoformat(iso_str)
    if target.tzinfo is None:
        target = target.replace(tzinfo=IST)
    diff = (target - datetime.now(IST)).total_seconds()
    return max(diff, 0)

# ─── Response detection ──────────────────────────────────────────────────────

SUCCESS_PATTERNS = [
    r"likes sent successfully",
    r"likes given by bot",
    r"after likes",
    r"daily limit used.*1/1",
]
LIMIT_PATTERNS = [
    r"daily limit reached",
    r"remain count has been exhausted",
    r"already used today",
    r"next reset",
    r"you can try again after reset",
]

def detect_response(text: str):
    t = text.lower()
    # Check success first (more specific)
    if any(re.search(p, t) for p in SUCCESS_PATTERNS):
        return "success"
    if any(re.search(p, t) for p in LIMIT_PATTERNS):
        return "limit"
    return "unknown"

# ─── Task Loop ───────────────────────────────────────────────────────────────

async def run_task(uid: int):
    log.info(f"[{uid}] Task started")
    while True:
        u = get_user(uid)
        if not u or not u["task_active"]:
            log.info(f"[{uid}] Task stopped")
            break

        # Wait if next_run is set (after success)
        if u["next_run"]:
            wait_sec = seconds_until(u["next_run"])
            if wait_sec > 0:
                log.info(f"[{uid}] Sleeping {wait_sec:.0f}s until next_run")
                await asyncio.sleep(min(wait_sec, 60))  # wake every 60s to check stop
                continue
            else:
                upsert_user(uid, next_run=None)

        try:
            client = make_client(uid)
            await client.connect()

            if not await client.is_user_authorized():
                await bot.send_message(uid, "⚠️ Session expire ho gaya. Dobara /start se login karo.")
                upsert_user(uid, task_active=0)
                await client.disconnect()
                break

            u = get_user(uid)
            target   = u["target_bot"] or "FF_LikesGiver_Bot"
            msg_text = u["msg_text"]   or "/like 0000000000"
            retry_m  = u["retry_minutes"] or 30

            result_holder = {"text": None, "event": asyncio.Event()}

            @client.on(events.NewMessage(from_users=target))
            async def handler(event):
                if not result_holder["event"].is_set():
                    result_holder["text"] = event.raw_text
                    result_holder["event"].set()

            await client.send_message(target, msg_text)
            log.info(f"[{uid}] Sent → @{target}: {msg_text}")

            try:
                await asyncio.wait_for(result_holder["event"].wait(), timeout=30)
            except asyncio.TimeoutError:
                log.warning(f"[{uid}] No response in 30s, retry in 60s")
                await client.disconnect()
                await asyncio.sleep(60)
                continue

            reply_text = result_holder["text"]
            status = detect_response(reply_text)
            log.info(f"[{uid}] Status: {status} | Text: {reply_text[:80]}")

            await client.disconnect()

            if status == "success":
                # ✅ Success — wait till next 4 AM
                next_run = next_4am_ist()
                upsert_user(uid, next_run=next_run)
                nr = datetime.fromisoformat(next_run)
                await bot.send_message(uid,
                    f"✅ <b>Like Mil Gaya!</b>\n\n"
                    f"<code>{reply_text[:400]}</code>\n\n"
                    f"😴 Next try: <b>{nr.strftime('%d %b %Y %I:%M %p IST')}</b>",
                    parse_mode="HTML"
                )

            else:
                # ⚠️ Limit ya unknown — retry after user-set minutes
                retry_sec = retry_m * 60
                retry_time = datetime.now(IST) + timedelta(minutes=retry_m)
                await bot.send_message(uid,
                    f"⏳ <b>Like nahi mila — retry karega</b>\n\n"
                    f"<code>{reply_text[:300]}</code>\n\n"
                    f"🔄 Dobara try karega: <b>{retry_time.strftime('%I:%M %p IST')}</b> ({retry_m} min baad)",
                    parse_mode="HTML"
                )
                log.info(f"[{uid}] Retry in {retry_m} min")
                await asyncio.sleep(retry_sec)

        except Exception as e:
            log.error(f"[{uid}] Error: {e}")
            try:
                await bot.send_message(uid,
                    f"❌ <b>Error aaya:</b>\n<code>{e}</code>\n\n5 min baad retry...",
                    parse_mode="HTML"
                )
            except:
                pass
            await asyncio.sleep(300)

    running_tasks.pop(uid, None)
    log.info(f"[{uid}] Task ended")

def ensure_task(uid):
    if uid not in running_tasks or running_tasks[uid].done():
        running_tasks[uid] = asyncio.create_task(run_task(uid))

# ─── /start ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid  = m.from_user.id
    user = get_user(uid)
    if not user:
        upsert_user(uid)
        user = get_user(uid)

    logged_in   = bool(user and user["session"])
    task_active = bool(user and user["task_active"])
    retry_m     = user["retry_minutes"] if user else 30

    status_icon = "🟢 Logged in" if logged_in else "🔴 Not logged in"
    task_icon   = "▶️ Running"   if task_active else "⏹ Stopped"

    await m.answer(
        f"<b>🎮 FF Like Auto-Bot</b>\n\n"
        f"👤 Status: {status_icon}\n"
        f"⚙️ Task: {task_icon}\n"
        f"🤖 Target: <code>@{user['target_bot'] if user else '—'}</code>\n"
        f"✉️ Message: <code>{user['msg_text'] if user else '—'}</code>\n"
        f"⏱ Retry interval: <b>{retry_m} min</b>\n\n"
        f"Bot automatically message bhejta hai. Jab tak ✅ na mile, har <b>{retry_m} min</b> baad retry karta hai.",
        parse_mode="HTML",
        reply_markup=main_kb(user)
    )

# ─── Status ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "status")
async def cb_status(cb: CallbackQuery):
    uid  = cb.from_user.id
    user = get_user(uid)
    logged      = bool(user and user["session"])
    active      = bool(user and user["task_active"])
    retry_m     = user["retry_minutes"] if user else 30
    next_r      = user["next_run"] if user else None

    next_str = "—"
    if next_r:
        nr = datetime.fromisoformat(next_r)
        next_str = nr.strftime("%d %b %Y %I:%M %p IST")

    await cb.message.edit_text(
        f"<b>📊 Status</b>\n\n"
        f"🔑 Login: {'✅ Yes' if logged else '❌ No'}\n"
        f"⚙️ Task: {'🟢 Running' if active else '🔴 Stopped'}\n"
        f"🤖 Target: <code>@{user['target_bot'] if user else '—'}</code>\n"
        f"✉️ Message: <code>{user['msg_text'] if user else '—'}</code>\n"
        f"⏱ Retry: <b>{retry_m} min</b>\n"
        f"😴 Next run (after success): <b>{next_str}</b>",
        parse_mode="HTML",
        reply_markup=main_kb(user)
    )
    await cb.answer()

# ─── Start / Stop Task ───────────────────────────────────────────────────────

@dp.callback_query(F.data == "start_task")
async def cb_start_task(cb: CallbackQuery):
    uid  = cb.from_user.id
    user = get_user(uid)
    if not user or not user["session"]:
        await cb.answer("⚠️ Pehle login karo!", show_alert=True)
        return
    upsert_user(uid, task_active=1)
    ensure_task(uid)
    user = get_user(uid)
    await cb.message.edit_reply_markup(reply_markup=main_kb(user))
    await cb.answer("✅ Task started!")

@dp.callback_query(F.data == "stop_task")
async def cb_stop_task(cb: CallbackQuery):
    uid = cb.from_user.id
    upsert_user(uid, task_active=0)
    user = get_user(uid)
    await cb.message.edit_reply_markup(reply_markup=main_kb(user))
    await cb.answer("⏹ Task stopped!")

# ─── Set Retry Interval ──────────────────────────────────────────────────────

@dp.callback_query(F.data == "set_retry")
async def cb_set_retry(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    curr = user["retry_minutes"] if user else 30
    await cb.message.answer(
        f"⏱ <b>Retry Interval Set Karo</b>\n\n"
        f"Current: <b>{curr} min</b>\n\n"
        f"Jab bhi limit ya unknown message aaye, kitne minute baad dobara try karega?\n"
        f"(Recommended: 30-60 min)",
        parse_mode="HTML",
        reply_markup=retry_kb()
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("retry_") & ~F.data.in_({"retry_custom"}))
async def cb_retry_select(cb: CallbackQuery):
    uid = cb.from_user.id
    minutes = int(cb.data.split("_")[1])
    upsert_user(uid, retry_minutes=minutes)
    user = get_user(uid)
    await cb.message.delete()
    await cb.message.answer(
        f"✅ Retry interval set: <b>{minutes} min</b>",
        parse_mode="HTML",
        reply_markup=main_kb(user)
    )
    await cb.answer()

@dp.callback_query(F.data == "retry_custom")
async def cb_retry_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.retry_minutes)
    await cb.message.answer(
        "✏️ Custom minutes daalo (1-1440):\nExample: <code>45</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await cb.answer()

@dp.message(SetStates.retry_minutes)
async def set_retry_minutes(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or not (1 <= int(m.text.strip()) <= 1440):
        await m.answer("❌ 1 se 1440 ke beech number daalo:")
        return
    minutes = int(m.text.strip())
    upsert_user(m.from_user.id, retry_minutes=minutes)
    await state.clear()
    user = get_user(m.from_user.id)
    await m.answer(
        f"✅ Retry interval set: <b>{minutes} min</b>",
        parse_mode="HTML",
        reply_markup=main_kb(user)
    )

@dp.callback_query(F.data == "cancel_retry")
async def cb_cancel_retry(cb: CallbackQuery):
    await cb.message.delete()
    await cb.answer("Cancelled")

# ─── Set Target Bot ──────────────────────────────────────────────────────────

@dp.callback_query(F.data == "set_bot")
async def cb_set_bot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.bot_username)
    await cb.message.answer(
        "🤖 Target bot ka username bhejo (without @):\nExample: <code>FF_LikesGiver_Bot</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await cb.answer()

@dp.message(SetStates.bot_username)
async def set_bot_username(m: Message, state: FSMContext):
    username = m.text.strip().lstrip("@")
    upsert_user(m.from_user.id, target_bot=username)
    await state.clear()
    user = get_user(m.from_user.id)
    await m.answer(
        f"✅ Target bot set: <code>@{username}</code>",
        parse_mode="HTML",
        reply_markup=main_kb(user)
    )

# ─── Set Message ─────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "set_msg")
async def cb_set_msg(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SetStates.message_text)
    await cb.message.answer(
        "✉️ Woh message bhejo jo bot ko bhejna hai:\nExample: <code>/like 1902086798</code>",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await cb.answer()

@dp.message(SetStates.message_text)
async def set_message_text(m: Message, state: FSMContext):
    upsert_user(m.from_user.id, msg_text=m.text.strip())
    await state.clear()
    user = get_user(m.from_user.id)
    await m.answer(
        f"✅ Message set: <code>{m.text.strip()}</code>",
        parse_mode="HTML",
        reply_markup=main_kb(user)
    )

# ─── Cancel ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.delete()
    await cb.answer("Cancelled")

# ─── Logout ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "logout")
async def cb_logout(cb: CallbackQuery):
    uid = cb.from_user.id
    upsert_user(uid, session="", task_active=0)
    user = get_user(uid)
    await cb.message.edit_text("🚪 Logged out.", reply_markup=main_kb(user))
    await cb.answer()

# ─── Login Flow ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "login")
async def cb_login(cb: CallbackQuery, state: FSMContext):
    await state.set_state(LoginStates.api_id)
    await cb.message.answer(
        "🔑 <b>Login — Step 1/5</b>\n\n"
        "Apna <b>API ID</b> bhejo.\n"
        "👉 my.telegram.org → App → API ID",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await cb.answer()

@dp.message(LoginStates.api_id)
async def login_api_id(m: Message, state: FSMContext):
    if not m.text.strip().isdigit():
        await m.answer("❌ API ID sirf numbers hota hai. Dobara bhejo:")
        return
    await state.update_data(api_id=int(m.text.strip()))
    await state.set_state(LoginStates.api_hash)
    await m.answer(
        "🔑 <b>Step 2/5</b> — Apna <b>API Hash</b> bhejo:",
        parse_mode="HTML", reply_markup=cancel_kb()
    )

@dp.message(LoginStates.api_hash)
async def login_api_hash(m: Message, state: FSMContext):
    await state.update_data(api_hash=m.text.strip())
    await state.set_state(LoginStates.phone)
    await m.answer(
        "🔑 <b>Step 3/5</b> — Phone number bhejo (country code ke saath):\n"
        "Example: <code>+919876543210</code>",
        parse_mode="HTML", reply_markup=cancel_kb()
    )

@dp.message(LoginStates.phone)
async def login_phone(m: Message, state: FSMContext):
    uid   = m.from_user.id
    data  = await state.get_data()
    phone = m.text.strip()
    await state.update_data(phone=phone)
    try:
        client = TelegramClient(StringSession(), data["api_id"], data["api_hash"])
        await client.connect()
        result = await client.send_code_request(phone)
        await state.update_data(
            phone_code_hash=result.phone_code_hash,
            session_str=client.session.save()
        )
        await client.disconnect()
        await state.set_state(LoginStates.otp)
        await m.answer(
            "🔑 <b>Step 4/5</b> — OTP bhejo\n\n"
            "Format: <code>2 3 4 5 6</code> (space se) ya <code>23456</code>",
            parse_mode="HTML", reply_markup=cancel_kb()
        )
    except Exception as e:
        await state.clear()
        await m.answer(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

@dp.message(LoginStates.otp)
async def login_otp(m: Message, state: FSMContext):
    uid  = m.from_user.id
    data = await state.get_data()
    otp  = m.text.strip().replace(" ", "")
    try:
        client = TelegramClient(StringSession(data["session_str"]), data["api_id"], data["api_hash"])
        await client.connect()
        try:
            await client.sign_in(
                phone=data["phone"],
                code=otp,
                phone_code_hash=data["phone_code_hash"]
            )
            session_str = client.session.save()
            await client.disconnect()
            upsert_user(uid, api_id=data["api_id"], api_hash=data["api_hash"],
                        phone=data["phone"], session=session_str, task_active=0)
            await state.clear()
            user = get_user(uid)
            await m.answer("✅ <b>Login ho gaya!</b>\nAb ▶️ Start Task dabao.",
                           parse_mode="HTML", reply_markup=main_kb(user))
        except Exception as e:
            err = str(e)
            if "SessionPasswordNeeded" in err or "password" in err.lower() or "2FA" in err:
                await state.update_data(session_str=client.session.save())
                await client.disconnect()
                await state.set_state(LoginStates.password)
                await m.answer("🔑 <b>Step 5/5</b> — 2FA Password bhejo:",
                               parse_mode="HTML", reply_markup=cancel_kb())
            else:
                await client.disconnect()
                await state.clear()
                await m.answer(f"❌ OTP Error: <code>{e}</code>", parse_mode="HTML")
    except Exception as e:
        await state.clear()
        await m.answer(f"❌ Error: <code>{e}</code>", parse_mode="HTML")

@dp.message(LoginStates.password)
async def login_password(m: Message, state: FSMContext):
    uid  = m.from_user.id
    data = await state.get_data()
    try:
        client = TelegramClient(StringSession(data["session_str"]), data["api_id"], data["api_hash"])
        await client.connect()
        await client.sign_in(password=m.text.strip())
        session_str = client.session.save()
        await client.disconnect()
        upsert_user(uid, api_id=data["api_id"], api_hash=data["api_hash"],
                    phone=data["phone"], session=session_str,
                    password_2fa=m.text.strip(), task_active=0)
        await state.clear()
        user = get_user(uid)
        await m.answer("✅ <b>Login ho gaya!</b>\nAb ▶️ Start Task dabao.",
                       parse_mode="HTML", reply_markup=main_kb(user))
    except Exception as e:
        await state.clear()
        await m.answer(f"❌ Password Error: <code>{e}</code>", parse_mode="HTML")

# ─── Resume on startup ───────────────────────────────────────────────────────

async def resume_tasks():
    with db_conn() as c:
        rows = c.execute("SELECT user_id FROM users WHERE task_active=1").fetchall()
    for row in rows:
        log.info(f"Resuming task for {row['user_id']}")
        ensure_task(row["user_id"])

# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    await resume_tasks()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
