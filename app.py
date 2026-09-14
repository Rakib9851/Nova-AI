import os
import re
import threading
import asyncio
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

import google.generativeai as genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ContextTypes, ChatMemberHandler, MessageHandler, 
    CommandHandler, CallbackQueryHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN") 
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") 
GROUP_TOPIC = os.getenv("GROUP_TOPIC", "এটি একটি সাধারণ আলোচনার গ্রুপ।")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY.strip())

LEARNED_DATA = []
RECENT_MESSAGES = deque(maxlen=100)
USER_WARNINGS = {}
BAD_WORDS = ["badword1", "badword2", "scam", "spam"] 

def get_ai_response(prompt_text: str) -> str:
    """স্বয়ংক্রিয়ভাবে কার্যকর মডেল খুঁজে উত্তর আনার ফাংশন"""
    # আপনার AI Studio-তে থাকা সক্রিয় মডেলগুলোর তালিকা
    target_models = [
        "gemini-3.7-flash", 
        "gemini-3.5-flash-lite", 
        "gemini-3.8-flash",
        "gemini-2.0-flash", 
        "gemini-1.5-flash-latest"
    ]
    
    for m_name in target_models:
        try:
            model = genai.GenerativeModel(m_name)
            res = model.generate_content(prompt_text)
            if res and res.text:
                return res.text
        except Exception:
            continue
            
    # যদি ওপরের কোনোটি কাজ না করে, অ্যাকাউন্ট থেকে সচল মডেল খুঁজে নেওয়া
    try:
        for m in genai.list_models():
            if "generateContent" in m.supported_generation_methods:
                model_id = m.name.replace("models/", "")
                try:
                    res = genai.GenerativeModel(model_id).generate_content(prompt_text)
                    if res and res.text:
                        return res.text
                except Exception:
                    continue
    except Exception as e:
        raise RuntimeError(f"মডেল লোড করা যায়নি: {e}")

    raise RuntimeError("অ্যাকাউন্টে কোনো সক্রিয় মডেল পাওয়া যায়নি।")

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat.type == "private":
        return True
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return any(admin.user.id == user_id for admin in admins)
    except Exception:
        return False

async def command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("হ্যালো! আমি আপনার এআই অ্যাসিস্ট্যান্ট। আপনি আমাকে যেকোনো প্রশ্ন করতে পারেন অথবা আপনার গ্রুপে অ্যাড করে অ্যাডমিন বানিয়ে দিতে পারেন।")

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.chat_member
    joined = chat_member.new_chat_member.status in ["member", "administrator"] and chat_member.old_chat_member.status in ["left", "kicked"]
    
    if joined:
        user_name = chat_member.new_chat_member.user.first_name
        welcome_msg = f"স্বাগতম {user_name}! 🎉\nআমাদের গ্রুপে আপনাকে পেয়ে আমরা আনন্দিত। দয়া করে গ্রুপের নিয়মকানুন পড়ে নিন।"
        
        keyboard = [[InlineKeyboardButton("📜 গ্রুপের নিয়মকানুন", callback_data="show_rules")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await context.bot.send_message(chat_id=chat_member.chat.id, text=welcome_msg, reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "show_rules":
        rules = "📌 **গ্রুপের নিয়ম:**\n১. কোনো স্প্যাম বা লিংক শেয়ার করা নিষেধ।\n২. খারাপ ভাষা ব্যবহার করা যাবে না।\n৩. ৩ বার নিয়ম ভাঙলে অটো ব্যান করা হবে।"
        await query.message.reply_text(rules, parse_mode="Markdown")

async def issue_warning(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str):
    user = update.message.from_user
    chat_id = update.message.chat_id
    user_id = user.id

    USER_WARNINGS[user_id] = USER_WARNINGS.get(user_id, 0) + 1
    count = USER_WARNINGS[user_id]

    await update.message.delete()

    if count >= 3:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.send_message(chat_id, f"🚫 {user.first_name}-কে গ্রুপ থেকে ব্যান করা হয়েছে। কারণ: ৩ বার নিয়ম ভঙ্গ।")
        USER_WARNINGS[user_id] = 0
    else:
        await context.bot.send_message(chat_id, f"⚠️ **সতর্কতা!** {user.first_name}, {reason}\nএটি আপনার {count}/3 নং ওয়ার্নিং। ৩ বার হলে ব্যান করা হবে!", parse_mode="Markdown")

async def command_teach(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("❌ এই কমান্ডটি শুধু অ্যাডমিনদের জন্য।")
        return
    text = " ".join(context.args)
    if text:
        LEARNED_DATA.append(text)
        await update.message.reply_text("✅ আমি নতুন তথ্য শিখে নিয়েছি! এখন থেকে কেউ এটা নিয়ে প্রশ্ন করলে আমি উত্তর দিতে পারবো।")

async def command_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        await update.message.reply_text("যাকে ব্যান করতে চান, তার মেসেজে রিপ্লাই দিয়ে /ban লিখুন।")
        return
    target = update.message.reply_to_message.from_user
    await context.bot.ban_chat_member(update.message.chat_id, target.id)
    await update.message.reply_text(f"🚫 {target.first_name}-কে ব্যান করা হয়েছে।")

async def command_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context): return
    if not update.message.reply_to_message:
        return
    await context.bot.pin_chat_message(update.message.chat_id, update.message.reply_to_message.message_id)

async def command_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not RECENT_MESSAGES:
        await update.message.reply_text("দুঃখিত, সামারি করার মতো যথেষ্ট চ্যাট হিস্ট্রি নেই।")
        return
    
    await update.message.reply_text("⏳ আমি গত মেসেজগুলো পড়ছি এবং সামারি তৈরি করছি...")
    chat_text = "\n".join(RECENT_MESSAGES)
    
    prompt = f"নিচের চ্যাটগুলো পড়ে বাংলায় একটি সুন্দর এবং পয়েন্ট করা সামারি তৈরি করো:\n\n{chat_text}"
    try:
        summary_res = get_ai_response(prompt)
        await update.message.reply_text(f"📊 **চ্যাট সামারি:**\n\n{summary_res}", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"সামারি তৈরিতে সমস্যা: {e}")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    user = update.message.from_user
    chat_type = update.effective_chat.type
    bot_username = context.bot.username
    is_user_admin = await is_admin(update, context)

    if chat_type in ["group", "supergroup"]:
        RECENT_MESSAGES.append(f"{user.first_name}: {text}")

        if not is_user_admin:
            if re.search(r"(https?://|www\.|t\.me/|\.com|\.net|\.org|\.me)", text.lower()):
                await issue_warning(update, context, "লিংক শেয়ার করা নিষেধ!")
                return
            
            if any(bad_word in text.lower() for bad_word in BAD_WORDS):
                await issue_warning(update, context, "খারাপ বা অশালীন ভাষা ব্যবহার করা নিষেধ!")
                return

        is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id
        is_bot_mentioned = f"@{bot_username}" in text

        if not (is_reply_to_bot or is_bot_mentioned):
            return
        
        prompt_text = text.replace(f"@{bot_username}", "").strip()
    else:
        prompt_text = text.strip()

    if not prompt_text:
        return

    if not GEMINI_API_KEY:
        await update.message.reply_text("❌ Render-এ GEMINI_API_KEY দেওয়া হয়নি!")
        return

    await context.bot.send_chat_action(chat_id=update.message.chat_id, action="typing")

    learned_context = "\n".join(LEARNED_DATA)
    full_prompt = f"""
    তুমি একটি টেলিগ্রাম গ্রুপের স্মার্ট অ্যাসিস্ট্যান্ট। 
    গ্রুপের মূল বিষয়: {GROUP_TOPIC}
    অ্যাডমিনদের থেকে শেখা বিশেষ তথ্য: {learned_context}
    
    শর্তসমূহ:
    ১. সবসময় বাংলায় এবং ভদ্র ভাষায় উত্তর দিবে।
    ২. পয়েন্ট করে ছোট আকারে উত্তর দিবে।
    ৩. উত্তর না জানলে বলবে অ্যাডমিন @Rakib_1434 সহায়তা করবেন।
    
    ইউজারের প্রশ্ন: {prompt_text}
    """

    try:
        reply_text = get_ai_response(full_prompt)
    except Exception as e:
        reply_text = f"⚠️ Gemini API সমস্যা:\n{e}"

    await update.message.reply_text(reply_text)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive and running!")
    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN is missing!")
        return

    # ইভেন্ট লুপ ফিক্স (যাতে কোনো এরর ছাড়া Render-এ পাস করে)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    threading.Thread(target=run_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", command_start))
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(button_callback))
    
    app.add_handler(CommandHandler("teach", command_teach))
    app.add_handler(CommandHandler("ban", command_ban))
    app.add_handler(CommandHandler("pin", command_pin))
    app.add_handler(CommandHandler("summary", command_summary))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    print("Advanced AI Group Manager Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
