import logging
import os
import threading
import asyncio
import re
from datetime import datetime, timedelta
from flask import Flask
from pymongo import MongoClient
from telegram import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)
import requests

# Logging Configuration
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# Environment Variables & Config
BOT_TOKEN = os.getenv("BOT_TOKEN")
VAK_SMS_API_KEY = os.getenv("VAK_SMS_API_KEY", "087d6bfb54884a7bbfd963a232add065")

# Multiple Admins Support (Commas or single ID formatted into list)
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "123456789,987654321")
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_RAW.split(",") if i.strip().isdigit()]

OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")
BINANCE_ID = os.getenv("BINANCE_ID", "907194603")
ADMIN_BKASH = "01858582881"
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB Setup (Isolated DB for 2nd Bot to prevent affecting Main Bot)
if not MONGODB_URI:
    logging.error("❌ MONGODB_URI Environment Variable missing!")
client = MongoClient(MONGODB_URI)
db = client["vaksms_child_bot_db"] # Dedicated Database Name for 2nd Bot

users_col = db["users"]
settings_col = db["settings"]

# Flask Web Server
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Child Telegram Bot (Chile WS) is Active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# Self-Ping Heartbeat (Uptime Keeping Mechanism)
async def self_ping():
    await asyncio.sleep(10)
    port = int(os.environ.get("PORT", 8080))
    url = f"http://127.0.0.1:{port}/"
    while True:
        try:
            requests.get(url, timeout=5)
            logging.info("💓 Internal Ping Successful - Server kept alive.")
        except Exception as e:
            logging.warning(f"⚠️ Internal Ping warning: {e}")
        await asyncio.sleep(300) # Ping every 5 minutes

# In-Memory Active Orders
active_orders = {}

# Conversation States
WAITING_AMOUNT, WAITING_TXID, WAITING_SCREENSHOT = range(3)
SUB_PLAN_SELECT, SUB_METHOD_SELECT, SUB_AMOUNT, SUB_TXID, SUB_SCREENSHOT = range(3, 8)
(
    ADMIN_BAN,
    ADMIN_UNBAN,
    ADMIN_ADD_BAL_USER,
    ADMIN_ADD_BAL_AMT,
    ADMIN_ZERO_BAL_USER,
    ADMIN_RATE_WA_CL_SET,
    ADMIN_BROADCAST,
) = range(8, 15)

# Helper Functions
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_country_flag(country_code: str) -> str:
    return "🇨🇱"

def mask_number(phone_str: str) -> str:
    clean_num = re.sub(r"[^\d+]", "", str(phone_str))
    if len(clean_num) <= 6:
        return clean_num
    prefix = clean_num[:4] if clean_num.startswith("+") else clean_num[:3]
    suffix = clean_num[-4:]
    masked_part = "*" * (len(clean_num) - len(prefix) - len(suffix))
    return f"{prefix}{masked_part}{suffix}"

def get_user(user_id: int):
    return users_col.find_one({"user_id": user_id})

def get_or_create_user(user_id: int, full_name: str = "User"):
    user = users_col.find_one({"user_id": user_id})
    if not user:
        user_data = {
            "user_id": user_id,
            "full_name": full_name,
            "balance": 0.0,
            "otp_count": 0,
            "selected_country": "cl",  # Fixed Chile (cl)
            "selected_service": "wa",  # Fixed WhatsApp (wa)
            "is_banned": False,
            "subscription_expiry": None
        }
        users_col.insert_one(user_data)
        return user_data
    else:
        users_col.update_one({"user_id": user_id}, {"$set": {"full_name": full_name}})
        return user

def get_rate():
    doc = settings_col.find_one({"type": "rates"})
    if doc and "rates" in doc and "wa_cl" in doc["rates"]:
        return float(doc["rates"]["wa_cl"])
    return 0.10  # Default Chile WS rate

def set_rate(rate: float):
    settings_col.update_one(
        {"type": "rates"},
        {"$set": {"rates.wa_cl": rate}},
        upsert=True
    )

def is_bot_active() -> bool:
    doc = settings_col.find_one({"type": "bot_status"})
    if doc:
        return doc.get("is_active", True)
    return True

def set_bot_active(status: bool):
    settings_col.update_one(
        {"type": "bot_status"},
        {"$set": {"is_active": status}},
        upsert=True
    )

def is_subscribed(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    user = get_user(user_id)
    if user and user.get("subscription_expiry"):
        expiry = user["subscription_expiry"]
        if datetime.now() < expiry:
            return True
    return False

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, photo_id, caption, reply_markup):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_id,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
        except Exception as e:
            logging.error(f"Failed to send notification to admin {admin_id}: {e}")

# Keyboards
def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton("💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴"), KeyboardButton("🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁")],
        [KeyboardButton("👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴"), KeyboardButton("💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃")]
    ]
    if is_admin(user_id):
        keyboard.append([KeyboardButton("⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# VAK-SMS API Functions
def set_number_status(id_num: str, status: str):
    url = f"https://vak-sms.com/api/setStatus/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}&status={status}"
    try:
        return requests.get(url).json()
    except Exception as e:
        return {"error": str(e)}

def buy_vak_number(service: str = "wa", country: str = "cl", max_price: float = 0.079):
    url = f"https://vak-sms.com/api/getNumber/?apiKey={VAK_SMS_API_KEY}&service={service}&country={country}&maxPrice={max_price}"
    try:
        res = requests.get(url).json()
        
        if isinstance(res, dict) and res.get("error") == "noNumber":
            return {"error": "Stock Out!"}
            
        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            assigned_price = res.get("price")
            if assigned_price is not None:
                try:
                    price_val = float(assigned_price)
                    if price_val > max_price:
                        id_num = str(res["idNum"])
                        set_number_status(id_num, "bad")
                        return {"error": "Stock Out!"}
                except ValueError:
                    pass

        return res
    except Exception as e:
        return {"error": "Stock Out!"}

def get_vak_balance():
    url = f"https://vak-sms.com/api/getBalance/?apiKey={VAK_SMS_API_KEY}"
    try:
        res = requests.get(url).json()
        return res.get("balance", 0.0)
    except Exception:
        return 0.0

def fetch_otp_code(id_num: str):
    url = f"https://vak-sms.com/api/getSmsCode/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}"
    try:
        return requests.get(url).json()
    except Exception as e:
        return {"error": str(e)}

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("❌ 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and not is_admin(user_id):
        await update.message.reply_text("🚧 **ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.** ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 𝙱𝚄𝙸 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾??", callback_data="buy_sub_start")]
        ])
        msg = (
            f"👋 **Hello {user.full_name}!**\n\n"
            f"❌ 𝚈𝙾𝚄 𝙳𝙾𝙽'𝚃 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚃𝙷𝙴 𝙱𝙾𝚃!\n"
            f"ʙᴏᴛ ʙᴇʙᴏʜᴀʀ ᴋᴏʀᴛᴇ ᴄʜᴀɪʟᴇ sᴜ𝙱sᴄʀɪ𝙿𝚃𝙸𝙾𝙽 ɴɪᴛᴇ ʜᴏʙᴇ.\n\n"
            f"📌 **𝗣𝗥𝗜𝗖𝗘𝗦:**\n"
            f"• ⏳ `5 Days`: `50 BDT` (or `0.40 USDT`)\n"
            f"• ⏳ `7 Days`: `70 BDT` (or `0.56 USDT`)\n\n"
            f"ɴɪᴄʜᴇʀ ᴍᴇɴᴜ ᴛʜᴇᴋᴇ ᴄʟɪᴄᴋ ᴋᴏʀᴇ sᴜ𝙱sᴄ𝚁𝙸𝙿𝚃𝙸𝙾𝙽 ᴋɪɴᴜɴ:"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 **𝙱𝚄𝙸 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽:**", reply_markup=sub_kb)
        return

    exp_time = u_data.get("subscription_expiry")
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and not is_admin(user_id)) else "Unlimited (Admin)"

    welcome_msg = (
        f"👋 **𝚆𝙴𝙻𝙲𝙾𝙼𝙴 CHILE WS BOT!**\n\n"
        f"⚙️ **আরে ভাই, সমস্যা নেই! দুইজন অ্যাডমিন যাতে সমানভাবে বট কন্ট্রোল করতে পারে, তার ব্যবস্থা একদম সহজ। 

আমরা বটের কোডে একটি Admin List ব্যবহার করব। এতে একাধিক Admin ID যুক্ত করা যাবে এবং দুজনেই বটের সব কমান্ড বা কন্ট্রোল অ্যাক্সেস করতে পারবেন।

আপনি **Python (Telebot/Aiogram)** নাকি **PHP (Telegram Bot API)** ব্যবহার করছেন, সেটা জানালে একদম হুবহু কোড দিয়ে দিতাম। 

তবে সাধারণ ধারণা এবং কোড ব্লক কেমন হবে, তা নিচে দেখে নিন:

### ১. Python (pyTelegramBotAPI / Telebot) হলে:

```python
# দুইজন অ্যাডমিনের Telegram User ID একটি লিস্টে রাখুন
ADMIN_IDS = [123456789, 987654321]  # এখানে আপনাদের নিজ নিজ ID বসাবেন

@bot.message_handler(commands=['admin', 'control'])
def admin_command(message):
    user_id = message.from_user.id
    
    # ইউজার অ্যাডমিন লিস্টে আছে কিনা চেক করা
    if user_id in ADMIN_IDS:
        bot.reply_to(message, "স্বাগতম অ্যাডমিন! আপনি বট কন্ট্রোল করতে পারবেন।")
        # অ্যাডমিন ফাংশনাল কোড
    else:
        bot.reply_to(message, "অ্যালাইভ না ভাই! আপনি এই কমান্ড ব্যবহার করতে পারবেন না।")
