import os
import re
import sqlite3
import threading
import time
import logging
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application,
    ContextTypes,
    ChatMemberHandler,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROUP_TOPIC = os.getenv("GROUP_TOPIC", "এটি একটি সাধারণ আলোচনার গ্রুপ।")

# Render-এ Persistent Disk ব্যবহার করলে DATA_DIR-কে সেই disk path করো।
# না দিলে /tmp/ai_group_bot_data ব্যবহার হবে।
DATA_DIR = os.getenv("DATA_DIR", "/tmp/ai_group_bot_data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")

# =========================================================
# LOGGING / MEMORY
# =========================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("AIGroupBot")

RECENT_MESSAGES = deque(maxlen=120)
BAD_WORDS = ["badword1", "badword2", "scam", "spam"]

# In-memory cache; permanent data goes to SQLite.
USER_WARNINGS = {}
SPAM_STATE = {}
MUTED_UNTIL = {}

DB_LOCK = threading.Lock()


# =========================================================
# DATABASE
# =========================================================
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with DB_LOCK:
        conn = db_connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS learned_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                data TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_warnings (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                welcome_enabled INTEGER NOT NULL DEFAULT 1,
                links_enabled INTEGER NOT NULL DEFAULT 1,
                badwords_enabled INTEGER NOT NULL DEFAULT 1,
                antispam_enabled INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                role TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        conn.commit()
        conn.close()


def ensure_chat(chat_id):
    with DB_LOCK:
        conn = db_connect()
        conn.execute(
            "INSERT OR IGNORE INTO chat_settings(chat_id) VALUES(?)",
            (chat_id,),
        )
        conn.commit()
        conn.close()


def get_settings(chat_id):
    ensure_chat(chat_id)
    with DB_LOCK:
        conn = db_connect()
        row = conn.execute(
            "SELECT * FROM chat_settings WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        conn.close()
    return dict(row)


def toggle_setting(chat_id, field):
    allowed = {
        "welcome_enabled",
        "links_enabled",
        "badwords_enabled",
        "antispam_enabled",
    }
    if field not in allowed:
        return
    ensure_chat(chat_id)
    with DB_LOCK:
        conn = db_connect()
        conn.execute(
            f"UPDATE chat_settings SET {field}=CASE {field} WHEN 1 THEN 0 ELSE 1 END WHERE chat_id=?",
            (chat_id,),
        )
        conn.commit()
        conn.close()


def save_learned(chat_id, text):
    with DB_LOCK:
        conn = db_connect()
        conn.execute(
            "INSERT INTO learned_data(chat_id,data,created_at) VALUES(?,?,?)",
            (chat_id, text, int(time.time())),
        )
        conn.commit()
        conn.close()


def get_learned(chat_id, limit=30):
    with DB_LOCK:
        conn = db_connect()
        rows = conn.execute(
            "SELECT data FROM learned_data WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        conn.close()
    return [row["data"] for row in reversed(rows)]


def get_warning(chat_id, user_id):
    with DB_LOCK:
        conn = db_connect()
        row = conn.execute(
            "SELECT count FROM user_warnings WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
        conn.close()
    return int(row["count"]) if row else 0


def set_warning(chat_id, user_id, count):
    with DB_LOCK:
        conn = db_connect()
        conn.execute("""
            INSERT INTO user_warnings(chat_id,user_id,count)
            VALUES(?,?,?)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count
        """, (chat_id, user_id, count))
        conn.commit()
        conn.close()


def save_memory(chat_id, user_id, user_name, role, message):
    # Keep a reasonable permanent context history.
    with DB_LOCK:
        conn = db_connect()
        conn.execute(
            "INSERT INTO chat_memory(chat_id,user_id,user_name,role,message,created_at) VALUES(?,?,?,?,?,?)",
            (chat_id, user_id, user_name, role, message[:4000], int(time.time())),
        )
        conn.execute("""
            DELETE FROM chat_memory
            WHERE chat_id=?
              AND id NOT IN (
                  SELECT id FROM chat_memory
                  WHERE chat_id=? ORDER BY id DESC LIMIT 80
              )
        """, (chat_id, chat_id))
        conn.commit()
        conn.close()


def get_memory(chat_id, limit=25):
    with DB_LOCK:
        conn = db_connect()
        rows = conn.execute(
            "SELECT user_name,role,message FROM chat_memory WHERE chat_id=? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        conn.close()
    return list(reversed(rows))


# =========================================================
# GEMINI
# =========================================================
def ask_gemini(prompt_text: str) -> str:
    if not GEMINI_API_KEY:
        raise ValueError("Render-এ GEMINI_API_KEY দেওয়া হয়নি!")

    model = "gemini-3.6-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY.strip(),
    }
    payload = {
        "contents": [{"parts": [{"text": prompt_text}]}]
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=60)
    except requests.Timeout:
        raise RuntimeError("Gemini উত্তর দিতে বেশি সময় নিচ্ছে। একটু পরে আবার চেষ্টা করুন।")
    except requests.RequestException as e:
        raise RuntimeError(f"Gemini connection error: {e}")

    if res.status_code != 200:
        try:
            err = res.json()
            message = err.get("error", {}).get("message", res.text)
        except Exception:
            message = res.text
        raise RuntimeError(f"Gemini API Error {res.status_code}: {message}")

    try:
        data = res.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("Gemini থেকে বৈধ উত্তর পাওয়া যায়নি।")


# =========================================================
# COMMON HELPERS
# =========================================================
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat or not update.effective_user:
        return False
    if update.effective_chat.type == "private":
        return True

    try:
        member = await context.bot.get_chat_member(
            update.effective_chat.id,
            update.effective_user.id,
        )
        return member.status in ("administrator", "creator")
    except Exception as e:
        logger.warning("Admin check failed: %s", e)
        return False


async def safe_delete(message):
    try:
        await message.delete()
    except Exception as e:
        logger.warning("Message delete failed: %s", e)


async def safe_send(context, chat_id, text, **kwargs):
    try:
        return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as e:
        logger.exception("send_message failed: %s", e)
        return None


def moderation_reason(text, settings):
    low = text.lower()

    if settings["links_enabled"]:
        if re.search(r"(https?://|www\.|t\.me/|\.com\b|\.net\b|\.org\b|\.me\b)", low):
            return "লিংক শেয়ার করা নিষেধ!"

    if settings["badwords_enabled"]:
        if any(word in low for word in BAD_WORDS):
            return "খারাপ বা অশালীন ভাষা ব্যবহার করা নিষেধ!"

    return None


def spam_detected(user_id, text):
    now = time.time()
    state = SPAM_STATE.setdefault(user_id, {"times": deque(maxlen=10), "last": ""})

    # Repeated identical messages
    if text.strip().lower() == state["last"] and text.strip():
        state["last"] = text.strip().lower()
        state["times"].append(now)
        return True

    state["last"] = text.strip().lower()
    state["times"].append(now)

    recent = [t for t in state["times"] if now - t <= 8]
    state["times"] = deque(recent, maxlen=10)

    return len(recent) >= 6


# =========================================================
# START / HELP
# =========================================================
async def command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "হ্যালো! আমি আপনার AI Group Assistant.\n\n"
        "আমি AI প্রশ্নের উত্তর দিতে, গ্রুপ ম্যানেজ করতে এবং moderation-এ সাহায্য করতে পারি।\n\n"
        "গ্রুপে আমাকে অ্যাড করে Admin permission দিন।"
    )
    await update.message.reply_text(text)


async def command_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 Bot Commands\n\n"
        "/start — Bot সম্পর্কে জানুন\n"
        "/help — Help menu\n"
        "/summary — সাম্প্রতিক chat summary\n"
        "/teach <তথ্য> — Admin হিসেবে AI-কে তথ্য শেখান\n"
        "/ban — Reply করে user ban করুন\n"
        "/pin — Reply করা message pin করুন\n"
        "/panel — Admin control panel\n\n"
        "AI-কে প্রশ্ন করতে group-এ আমাকে mention করুন বা আমার message-এ reply করুন।"
    )
    await update.message.reply_text(text)


# =========================================================
# SMART WELCOME
# =========================================================
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_member = update.chat_member
        joined = (
            chat_member.new_chat_member.status in ["member", "administrator"]
            and chat_member.old_chat_member.status in ["left", "kicked"]
        )
        if not joined:
            return

        chat_id = chat_member.chat.id
        settings = get_settings(chat_id)
        if not settings["welcome_enabled"]:
            return

        user = chat_member.new_chat_member.user
        user_name = user.first_name or "বন্ধু"

        welcome_msg = (
            f"👋 স্বাগতম {user_name}!\n\n"
            "আমাদের গ্রুপে আপনাকে পেয়ে ভালো লাগছে।\n"
            "গ্রুপের নিয়ম মেনে সুন্দর পরিবেশ বজায় রাখুন।\n\n"
            "নিচের বাটন থেকে নিয়মগুলো দেখে নিন।"
        )

        keyboard = [[
            InlineKeyboardButton("📜 গ্রুপের নিয়ম", callback_data="show_rules")
        ]]
        await context.bot.send_message(
            chat_id=chat_id,
            text=welcome_msg,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        logger.exception("Welcome handler failed")


# =========================================================
# RULES / ADMIN PANEL
# =========================================================
def rules_text():
    return (
        "📌 গ্রুপের নিয়ম\n\n"
        "1. Spam বা অনুমতি ছাড়া link দেওয়া যাবে না।\n"
        "2. খারাপ বা অশালীন ভাষা ব্যবহার করা যাবে না।\n"
        "3. একই message বারবার পাঠানো যাবে না।\n"
        "4. নিয়ম ভাঙলে warning, mute বা ban হতে পারে।"
    )


async def show_panel(query):
    chat_id = query.message.chat_id
    settings = get_settings(chat_id)

    def mark(v):
        return "ON" if settings[v] else "OFF"

    keyboard = [
        [
            InlineKeyboardButton("🤖 AI Info", callback_data="panel_ai"),
            InlineKeyboardButton("🧠 Memory", callback_data="panel_memory"),
        ],
        [
            InlineKeyboardButton(
                f"👋 Welcome: {mark('welcome_enabled')}",
                callback_data="toggle_welcome",
            )
        ],
        [
            InlineKeyboardButton(
                f"🔗 Links: {mark('links_enabled')}",
                callback_data="toggle_links",
            ),
            InlineKeyboardButton(
                f"🛡️ Anti-Spam: {mark('antispam_enabled')}",
                callback_data="toggle_antispam",
            ),
        ],
        [
            InlineKeyboardButton(
                f"🚫 Bad Words: {mark('badwords_enabled')}",
                callback_data="toggle_badwords",
            )
        ],
        [InlineKeyboardButton("📜 Rules", callback_data="show_rules")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="panel")],
    ]

    await query.edit_message_text(
        "🎛️ AI GROUP ADMIN PANEL\n\n"
        "নিচের button থেকে group settings নিয়ন্ত্রণ করুন।",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        data = query.data

        if data == "show_rules":
            await query.message.reply_text(rules_text())
            return

        if data == "panel":
            if not await is_admin(update, context):
                await query.answer("শুধু Admin ব্যবহার করতে পারবেন।", show_alert=True)
                return
            await show_panel(query)
            return

        if data.startswith("toggle_"):
            if not await is_admin(update, context):
                await query.answer("শুধু Admin ব্যবহার করতে পারবেন।", show_alert=True)
                return

            field = {
                "toggle_welcome": "welcome_enabled",
                "toggle_links": "links_enabled",
                "toggle_badwords": "badwords_enabled",
                "toggle_antispam": "antispam_enabled",
            }.get(data)

            if field:
                toggle_setting(query.message.chat_id, field)
            await show_panel(query)
            return

        if data == "panel_ai":
            await query.message.reply_text(
                "🤖 Advanced Gemini AI\n\n"
                "• Gemini 3.6 Flash\n"
                "• Group topic awareness\n"
                "• Learned admin information\n"
                "• Recent conversation context\n"
                "• Better reply/reply-to-bot handling"
            )
            return

        if data == "panel_memory":
            if not await is_admin(update, context):
                await query.answer("শুধু Admin ব্যবহার করতে পারবেন।", show_alert=True)
                return
            count = len(get_memory(query.message.chat_id, 80))
            learned = len(get_learned(query.message.chat_id, 100))
            await query.message.reply_text(
                f"🧠 AI Memory\n\n"
                f"Conversation memory: {count}\n"
                f"Learned facts: {learned}\n\n"
                "ডেটা SQLite database-এ সংরক্ষিত হচ্ছে।"
            )
            return

    except Exception:
        logger.exception("Callback failed")
        try:
            await query.message.reply_text("⚠️ এই action সম্পন্ন করা যায়নি। আবার চেষ্টা করুন।")
        except Exception:
            pass


async def command_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ এই panel শুধু Adminদের জন্য।")
        return

    keyboard = [[InlineKeyboardButton("🎛️ Open Control Panel", callback_data="panel")]]
    await update.message.reply_text(
        "⚙️ Admin Control Center",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# =========================================================
# WARNING → MUTE → BAN
# =========================================================
async def mute_user(context, chat_id, user_id, seconds=600):
    until_date = int(time.time()) + seconds
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )
    await context.bot.restrict_chat_member(
        chat_id,
        user_id,
        permissions=permissions,
        until_date=until_date,
    )
    MUTED_UNTIL[(chat_id, user_id)] = until_date


async def issue_warning(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str):
    user = update.message.from_user
    chat_id = update.message.chat_id
    user_id = user.id

    count = get_warning(chat_id, user_id) + 1
    set_warning(chat_id, user_id, count)

    await safe_delete(update.message)

    try:
        if count >= 5:
            await context.bot.ban_chat_member(chat_id, user_id)
            set_warning(chat_id, user_id, 0)
            await safe_send(
                context,
                chat_id,
                f"🚫 {user.first_name}-কে ৫টি warning-এর পর গ্রুপ থেকে ban করা হয়েছে।",
            )
        elif count == 3:
            await mute_user(context, chat_id, user_id, 10 * 60)
            await safe_send(
                context,
                chat_id,
                f"🔇 {user.first_name}-কে ১০ মিনিটের জন্য mute করা হয়েছে।\nকারণ: {reason}\nWarning: {count}/5",
            )
        else:
            await safe_send(
                context,
                chat_id,
                f"⚠️ Warning: {user.first_name}\n"
                f"কারণ: {reason}\n"
                f"Warning: {count}/5\n\n"
                f"৩টি warning হলে ১০ মিনিট mute এবং ৫টি হলে ban করা হবে।",
            )
    except Exception as e:
        logger.exception("Moderation action failed: %s", e)


# =========================================================
# ADMIN COMMANDS
# =========================================================
async def command_teach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ এই কমান্ডটি শুধু অ্যাডমিনদের জন্য।")
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text(
            "ব্যবহার:\n/teach আপনার গ্রুপ/বট সম্পর্কিত তথ্য"
        )
        return

    save_learned(update.effective_chat.id, text)
    await update.message.reply_text(
        "✅ তথ্যটি permanent AI memory-তে সংরক্ষণ করা হয়েছে।"
    )


async def command_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "যাকে ban করতে চান, তার message-এ reply করে /ban লিখুন।"
        )
        return

    target = update.message.reply_to_message.from_user
    try:
        await context.bot.ban_chat_member(update.message.chat_id, target.id)
        await update.message.reply_text(
            f"🚫 {target.first_name}-কে ban করা হয়েছে।"
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ban করা যায়নি: {e}")


async def command_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "যে message pin করতে চান, সেটিতে reply করে /pin লিখুন।"
        )
        return

    try:
        await context.bot.pin_chat_message(
            update.message.chat_id,
            update.message.reply_to_message.message_id,
        )
        await update.message.reply_text("📌 Message pin করা হয়েছে।")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Pin করা যায়নি: {e}")


async def command_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not RECENT_MESSAGES:
        await update.message.reply_text(
            "দুঃখিত, summary করার মতো যথেষ্ট chat history নেই।"
        )
        return

    await update.message.reply_text(
        "⏳ আমি সাম্প্রতিক message পড়ছি এবং summary তৈরি করছি..."
    )

    chat_text = "\n".join(RECENT_MESSAGES)
    prompt = (
        "নিচের Telegram group chat পড়ে বাংলায় একটি পরিষ্কার, ছোট এবং point-based summary তৈরি করো। "
        "গুরুত্বপূর্ণ সিদ্ধান্ত/প্রশ্ন/বিষয় আলাদা করে দেখাও।\n\n"
        + chat_text
    )

    try:
        summary_res = ask_gemini(prompt)
        await update.message.reply_text(
            f"📊 Chat Summary\n\n{summary_res}"
        )
    except Exception as e:
        logger.exception("Summary failed")
        await update.message.reply_text(
            f"⚠️ Summary তৈরি করা যায়নি: {e}"
        )


# =========================================================
# AI MESSAGE HANDLER
# =========================================================
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    try:
        text = update.message.text.strip()
        user = update.message.from_user
        chat = update.effective_chat
        chat_id = chat.id
        chat_type = chat.type
        bot_username = context.bot.username or ""

        if chat_type in ["group", "supergroup"]:
            ensure_chat(chat_id)
            settings = get_settings(chat_id)
            RECENT_MESSAGES.append(f"{user.first_name}: {text}")

            # Admin messages are not moderated.
            is_user_admin = await is_admin(update, context)

            if not is_user_admin:
                reason = moderation_reason(text, settings)
                if reason:
                    await issue_warning(update, context, reason)
                    return

                if settings["antispam_enabled"] and spam_detected(user.id, text):
                    await issue_warning(
                        update,
                        context,
                        "Spam/flood detected — অনুগ্রহ করে একই message বারবার পাঠাবেন না।",
                    )
                    return

            # AI memory stores group messages only when they are normal text.
            save_memory(
                chat_id,
                user.id,
                user.first_name or "User",
                "user",
                text,
            )

            is_reply_to_bot = (
                update.message.reply_to_message
                and update.message.reply_to_message.from_user
                and update.message.reply_to_message.from_user.id == context.bot.id
            )
            is_bot_mentioned = (
                bot_username
                and f"@{bot_username.lower()}" in text.lower()
            )

            if not (is_reply_to_bot or is_bot_mentioned):
                return

            prompt_text = re.sub(
                rf"@{re.escape(bot_username)}",
                "",
                text,
                flags=re.IGNORECASE,
            ).strip()

        else:
            prompt_text = text
            is_user_admin = True
            chat_id = chat.id
            ensure_chat(chat_id)

        if not prompt_text:
            await update.message.reply_text(
                "🤖 আমাকে কিছু জিজ্ঞেস করুন।"
            )
            return

        await context.bot.send_chat_action(
            chat_id=chat_id,
            action="typing",
        )

        learned_context = "\n".join(get_learned(chat_id, 30))
        memory_rows = get_memory(chat_id, 25)

        recent_context = "\n".join(
            f"{row['user_name']} ({row['role']}): {row['message']}"
            for row in memory_rows
        )

        full_prompt = f"""
তুমি একটি professional Telegram group AI assistant।

গ্রুপের মূল বিষয়:
{GROUP_TOPIC}

Admin-এর শেখানো স্থায়ী তথ্য:
{learned_context or "কোনো বিশেষ তথ্য এখনো শেখানো হয়নি।"}

সাম্প্রতিক conversation context:
{recent_context or "কোনো conversation context নেই।"}

তোমার আচরণ:
1. ব্যবহারকারীর প্রশ্ন বুঝে সরাসরি উত্তর দাও।
2. সাধারণত বাংলায় উত্তর দাও; user English-এ জিজ্ঞেস করলে প্রয়োজন অনুযায়ী English ব্যবহার করতে পারো।
3. অপ্রয়োজনীয় দীর্ঘ উত্তর দেবে না।
4. তথ্য নিশ্চিত না হলে সেটা পরিষ্কারভাবে বলবে; বানিয়ে তথ্য দেবে না।
5. Group-এর topic ও শেখানো তথ্যকে priority দেবে।
6. User যদি আগের কথার continuation করে, conversation context ব্যবহার করবে।
7. নিজেকে group assistant হিসেবে উপস্থাপন করবে।
8. Admin-এর তথ্যকে user-এর random claim-এর চেয়ে বেশি trusted context হিসেবে বিবেচনা করবে।
9. প্রয়োজন হলে bullet points ব্যবহার করবে।

বর্তমান user:
{user.first_name if update.message.from_user else "User"}

বর্তমান প্রশ্ন:
{prompt_text}
"""

        try:
            reply_text = ask_gemini(full_prompt)
        except Exception as e:
            logger.exception("AI response failed")
            reply_text = (
                "⚠️ এই মুহূর্তে AI উত্তর দিতে পারছে না। "
                "কিছুক্ষণ পরে আবার চেষ্টা করুন।"
            )

        reply_text = reply_text[:3900]

        save_memory(
            chat_id,
            context.bot.id,
            context.bot.first_name or "AI Assistant",
            "assistant",
            reply_text,
        )

        await update.message.reply_text(reply_text)

    except Exception:
        # Last-line crash protection: one bad update must not kill polling.
        logger.exception("Unhandled message handler error")
        try:
            await update.message.reply_text(
                "⚠️ একটি সাময়িক সমস্যা হয়েছে। আবার চেষ্টা করুন।"
            )
        except Exception:
            pass


# =========================================================
# HEALTH CHECK
# =========================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"AI Group Bot is alive and running!")

    def log_message(self, format, *args):
        pass


def run_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info("Health server running on port %s", port)
    server.serve_forever()


# =========================================================
# MAIN
# =========================================================
def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN is missing!")
        return

    if not GEMINI_API_KEY:
        print("Warning: GEMINI_API_KEY is missing!")

    init_db()

    threading.Thread(
        target=run_server,
        daemon=True,
        name="health-server",
    ).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", command_start))
    app.add_handler(CommandHandler("help", command_help))
    app.add_handler(CommandHandler("panel", command_panel))
    app.add_handler(ChatMemberHandler(
        welcome_new_member,
        ChatMemberHandler.CHAT_MEMBER,
    ))
    app.add_handler(CallbackQueryHandler(button_callback))

    app.add_handler(CommandHandler("teach", command_teach))
    app.add_handler(CommandHandler("ban", command_ban))
    app.add_handler(CommandHandler("pin", command_pin))
    app.add_handler(CommandHandler("summary", command_summary))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages)
    )

    logger.info("Professional AI Group Manager Bot started successfully!")

    # PTB handles the event loop/polling lifecycle.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
