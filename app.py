import logging
import os
import threading
import asyncio
import re
from datetime import datetime, timedelta
from flask import Flask, request
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
VAK_SMS_API_KEY = os.getenv("VAK_SMS_API_KEY", "893d842ab70a4e79b4ad323185a69257")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")
BINANCE_ID = os.getenv("BINANCE_ID", "907194603")
ADMIN_BKASH = "01858582881"
MONGODB_URI = os.getenv("MONGODB_URI")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render theke auto ashbe jodi URL thake

# MongoDB Setup
if not MONGODB_URI:
    logging.error("<emoji id='5409048419211682843'>❌</emoji> MONGODB_URI Environment Variable missing!")
client = MongoClient(MONGODB_URI)
db = client["vaksms_bot_db"]

users_col = db["users"]
settings_col = db["settings"]

# Flask Web Server
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Rex Private Telegram Bot is Active with Webhook!", 200

# In-Memory Active Orders
active_orders = {}

# Conversation States
WAITING_AMOUNT, WAITING_TXID, WAITING_SCREENSHOT = range(3)
SUB_AMOUNT, SUB_TXID, SUB_SCREENSHOT = range(3, 6)
WAIT_GROUP_USERNAME, WAIT_GROUP_SCREENSHOT = range(6, 8)
(
    ADMIN_BAN,
    ADMIN_UNBAN,
    ADMIN_ADD_BAL_USER,
    ADMIN_ADD_BAL_AMT,
    ADMIN_ZERO_BAL_USER,
    ADMIN_RATE_WA_HK_SET,
    ADMIN_RATE_WA_CL_SET,
    ADMIN_RATE_TG_HK_SET,
    ADMIN_RATE_TG_CL_SET,
    ADMIN_BROADCAST,
) = range(8, 18)

# Helper Functions
def get_country_flag(country_code: str) -> str:
    code = country_code.lower()
    if code == "hk":
        return "<emoji id='5206607081334906820'>🇭🇰</emoji>"
    elif code == "cl":
        return "<emoji id='5240241223632954241'>🇨🇱</emoji>"
    return "<emoji id='5210952531676504517'>🌐</emoji>"

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
            "selected_country": "hk",
            "selected_service": "tg",
            "is_banned": False,
            "subscription_expiry": None,
            "is_group_verified": False
        }
        users_col.insert_one(user_data)
        return user_data
    else:
        users_col.update_one({"user_id": user_id}, {"$set": {"full_name": full_name}})
        return user

def get_rate(service_code: str = "tg", country_code: str = "hk"):
    doc = settings_col.find_one({"type": "rates"})
    key = f"{service_code.lower()}_{country_code.lower()}"
    
    if doc and "rates" in doc and key in doc["rates"]:
        return float(doc["rates"][key])
    
    defaults = {
        "wa_hk": 0.10,
        "wa_cl": 0.10,
        "tg_hk": 0.12,
        "tg_cl": 0.12
    }
    return defaults.get(key, 0.10)

def set_rate(service_code: str, country_code: str, rate: float):
    key = f"{service_code.lower()}_{country_code.lower()}"
    settings_col.update_one(
        {"type": "rates"},
        {"$set": {f"rates.{key}": rate}},
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
    if user_id == ADMIN_ID:
        return True
    user = get_user(user_id)
    if user and user.get("subscription_expiry"):
        expiry = user["subscription_expiry"]
        if datetime.now() < expiry:
            return True
    return False

def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton("💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴"), KeyboardButton("🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁")],
        [KeyboardButton("🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂"), KeyboardButton("📱 𝚂𝙴𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴")],
        [KeyboardButton("👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴"), KeyboardButton("💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃")]
    ]
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton("⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def set_number_status(id_num: str, status: str):
    url = f"https://vak-sms.com/api/setStatus/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}&status={status}"
    try:
        return requests.get(url).json()
    except Exception as e:
        return {"error": str(e)}

def buy_vak_number(service: str = "tg", country: str = "hk", max_price: float = 0.087):
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("<emoji id='5440660757194744323'>❌</emoji> 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and user_id != ADMIN_ID:
        await update.message.reply_text("🚧 <emoji id='5436113877181941026'><b>ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.</b></emoji> ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="HTML")
        return

    if not is_subscribed(user_id):
        if not u_data.get("is_group_verified", False):
            verify_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Verify Group Membership", callback_data="start_group_verify")]
            ])
            msg = (
                f"👋 <b>Hello {user.full_name}!</b>\n\n"
                f"<emoji id='5416081784641168838'>❌</emoji> 𝚈𝙾𝚄 𝙳𝙾𝙽'𝚃 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚃𝙷𝙴 𝙱𝙾𝚃!\n"
                f"ʙᴏᴛ ʙᴇʙᴏʜᴀʀ ᴋᴏʀᴛᴇ ᴄʜᴀɪʟᴇ prothomti amader <b>Private Group</b>-e join thakte hobe.\n\n"
                f"📌 Nicher button-e click kore apnar group join-er proof (Username & Screenshot) admin-er kache pathan:"
            )
            await update.message.reply_text(msg, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
            await update.message.reply_text("👇 <b>Verification:</b>", parse_mode="HTML", reply_markup=verify_kb)
            return

        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽(30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        msg = (
            f"👋 <b>Hello {user.full_name}!</b>\n\n"
            f"<emoji id='5411225014148014586'>❌</emoji> 𝚈𝙾𝚄 𝙳𝙾𝙽'𝚃 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚃𝙷𝙴 𝙱𝙾𝚃!\n"
            f"ʙᴏᴛ ʙᴇʙᴏʜᴀʀ ᴋᴏʀᴛᴇ ᴄʜᴀɪʟᴇ sᴜʙsᴄʀɪᴘᴛɪᴏɴ ɴɪᴛᴇ ʜᴏʙᴇ.\n\n"
            f"📌 <b>𝗣𝗥𝗜𝗖𝗘:</b> <code>30 Tk</code>\n"
            f"⏳ <b>𝗩𝗔𝗟𝗜𝗗𝗜𝗧𝗬:</b> <code>3 Days</code>\n\n"
            f"ɴɪᴄʜᴇʀ ᴍᴇɴᴜ ᴛʜᴇᴋᴇ ᴄʟɪᴄᴋ ᴋᴏʀᴇ sᴜʙsᴄ𝚁𝙸𝙿𝚃𝙸𝙾𝙽 ᴋɪɴᴜɴ:"
        )
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 <b>𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽:</b>", parse_mode="HTML", reply_markup=sub_kb)
        return

    exp_time = u_data.get("subscription_expiry")
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin)"

    curr_country_code = u_data.get("selected_country", "hk")
    curr_country = curr_country_code.upper()
    country_flag = get_country_flag(curr_country_code)
    curr_service = u_data.get("selected_service", "tg").upper()

    welcome_msg = (
        f"👋 <emoji id='5244837092042750681'><b>𝚆𝙴𝙻𝙲𝙾𝙼𝙴 𝚁𝙴𝚇 𝙿𝚁𝙸𝚅𝙰𝚃𝙴 𝙱𝙾𝚃!</b></emoji>\n\n"
        f"⚙️️ <b>𝚁𝙴𝙲𝙴𝙽𝚃 𝚂𝙴𝚃𝚄𝙿:</b>\n"
        f"• 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂: <code>{curr_country}</code> {country_flag}\n"
        f"• Service: <code>{curr_service}</code>\n"
        f"• 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: <code>${u_data.get('balance', 0.0):.4f} USDT</code>\n"
        f"• 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚅𝙰𝙻𝙸𝙳 𝚃𝙸𝙻𝙻: <code>{exp_str}</code>\n\n"
        f"𝙺𝙰𝙹 𝙺𝙾𝚁𝚃𝙴 𝙽𝙸𝙲𝙷𝙴 𝙳𝙴𝙰 𝙼𝙴𝙽𝚄 𝚄𝚂𝙴 𝙺𝙾𝚁𝙴𝙽:"
    )
    await update.message.reply_text(welcome_msg, parse_mode="HTML", reply_markup=get_main_keyboard(user_id))

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("<emoji id='5424972470023104089'>❌</emoji> 𝚈𝙾𝚄𝚁 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙷𝙰𝚂 𝙱𝙴𝙴𝙽 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽.", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and user_id != ADMIN_ID:
        await update.message.reply_text("🚧 <emoji id='5282843764451195532'><b>𝙱𝙾𝚃 𝚄𝙽𝙳𝙴𝚁 𝙼𝙰𝙸𝙽𝚃𝙰𝙸𝙽𝚂 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽.</b></emoji> 𝚃𝚁𝙸 𝚂𝙾𝙼𝙴 𝚃𝙸𝙼𝙴 𝙰𝙶𝙰𝙸𝙽.", parse_mode="HTML")
        return

    if not is_subscribed(user_id):
        if not u_data.get("is_group_verified", False):
            verify_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Verify Group Membership", callback_data="start_group_verify")]
            ])
            await update.message.reply_text("<emoji id='5271604874419647061'>❌</emoji> Apnake prothome private group verification korte hobe.", reply_markup=ReplyKeyboardRemove())
            await update.message.reply_text("👇 <b>Verification:</b>", parse_mode="HTML", reply_markup=verify_kb)
            return

        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽(30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        await update.message.reply_text("<emoji id='5427168083074628963'>❌</emoji> 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙴𝚇𝙿𝙸𝚁𝙴𝚂! 𝙱𝚄𝚈 𝙽𝙴𝚆 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽.", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 <b>𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽:</b>", parse_mode="HTML", reply_markup=sub_kb)
        return

    text = update.message.text.strip()

    if text == "💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴":
        bot_bal = u_data.get("balance", 0.0)
        msg = f"💰 <b>𝙼𝚈 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:</b> <code>${bot_bal:.4f}</code> USDT"
        if user_id == ADMIN_ID:
            site_bal = get_vak_balance()
            msg += f"\n🏦 <b>𝙿𝙰𝙽𝙴𝙻 𝙱𝙰𝙻𝙰𝙽𝙲𝙴 :</b> <code>${site_bal:.4f}</code> USD"
        await update.message.reply_text(msg, parse_mode="HTML")
        return

    if text == "👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴":
        bot_bal = u_data.get("balance", 0.0)
        otp_cnt = u_data.get("otp_count", 0)
        exp_time = u_data.get("subscription_expiry")
        exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin)"
        profile_msg = (
            f"👤 <b>Apnar Profile Info:</b>\n\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"📛 <b>Name:</b> {user.full_name}\n"
            f"💵 <b>Balance:</b> <code>${bot_bal:.4f}</code> USDT\n"
            f"📩 <b>Total OTP Received:</b> <code>{otp_cnt}</code>\n"
            f"📅 <b>Subscription Valid:</b> <code>{exp_str}</code>"
        )
        await update.message.reply_text(profile_msg, parse_mode="HTML")
        return

    if text in ["🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈", "🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂"]:
        country_kb = [
            [KeyboardButton("COUNTRY: HK 🇭🇰 (HONG KONG)"), KeyboardButton("COUNTRY: CHILE 🇨🇱 (CL)")],
            [KeyboardButton("🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")]
        ]
        await update.message.reply_text("🌐 <b>SELECT YOUR COUNTRY:</b>", parse_mode="HTML", reply_markup=ReplyKeyboardMarkup(country_kb, resize_keyboard=True))
        return

    if "HK" in text:
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_country": "hk"}})
        await update.message.reply_text("✅ Country set: <code>HONG KONG (HK)</code> 🇭🇰", parse_mode="HTML", reply_markup=get_main_keyboard(user_id))
        return

    if "CHILE" in text or "CL" in text:
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_country": "cl"}})
        await update.message.reply_text("✅ Country set: <code>CHILE (CL)</code> 🇨🇱", parse_mode="HTML", reply_markup=get_main_keyboard(user_id))
        return

    if text == "📱 𝚂𝙴𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴":
        service_kb = [
            [KeyboardButton("𝚂𝙴𝚁𝚅𝙸𝙲𝙴: TG (𝚃𝙴𝙻𝙴𝙶𝚁𝙰𝙼)")],
            [KeyboardButton("𝚂𝙴𝚁𝚅𝙸𝙲𝙴: WA (𝚆𝙷𝙰𝚃𝚂𝙰𝙿𝙿)")],
            [KeyboardButton("🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")]
        ]
        await update.message.reply_text("📱 <b>SELECT YOUR SERVICE:</b>", parse_mode="HTML", reply_markup=ReplyKeyboardMarkup(service_kb, resize_keyboard=True))
        return

    if "TG" in text or "TELEGRAM" in text.upper():
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_service": "tg"}})
        await update.message.reply_text("✅ 𝚂𝙴𝚁𝚅𝙸𝙲𝙴 𝚂𝙴𝚃: <code>TELEGRAM (TG)</code>", parse_mode="HTML", reply_markup=get_main_keyboard(user_id))
        return

    if "WA" in text or "WHATSAPP" in text.upper():
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_service": "wa"}})
        await update.message.reply_text("✅ 𝚂𝙴𝚁𝚅𝙸𝙲𝙴 𝚂𝙴𝚃: <code>WHATSAPP (WA)</code>", parse_mode="HTML", reply_markup=get_main_keyboard(user_id))
        return

    if text == "🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄":
        await start(update, context)
        return

    if text == "🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁":
        user_has_active = any(order.get("user_id") == user_id for order in active_orders.values())
        if user_has_active:
            await update.message.reply_text(
                "⚠️ <emoji id='5397916757333654639'><b>অলরেডি একটি নম্বর কেনা রয়েছে!</b></emoji>\nনতুন নম্বর কেনার আগে আগের নম্বরটি ব্যবহার সম্পন্ন করুন অথবা Cancel করুন.",
                parse_mode="HTML"
            )
            return

        country = u_data.get("selected_country", "hk")
        service = u_data.get("selected_service", "tg")
        country_flag = get_country_flag(country)
        
        if country == "hk" and service == "wa":
            max_price_limit = 0.07
        elif country == "cl" and service == "wa":
            max_price_limit = 0.079
        elif country == "cl" and service == "tg":
            max_price_limit = 0.087
        elif country == "cl":
            max_price_limit = 0.087
        else:
            max_price_limit = 0.075
        
        bot_rate = get_rate(service_code=service, country_code=country)
        user_bal = u_data.get("balance", 0.0)

        if user_bal < bot_rate:
            await update.message.reply_text(
                f"<emoji id='5397916757333654639'>❌</emoji> 𝚂𝙾𝚁𝚁𝚈 𝙳𝙾 𝙽𝙾𝚃𝙴 𝙰𝙽𝙰𝙵 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: <code>${bot_rate}</code> USDT, 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: <code>${user_bal:.4f}</code> USDT.\n𝙳𝙸𝙿𝙾𝚂𝙸𝚃 𝙺𝙾𝚁𝚄𝙽.",
                parse_mode="HTML"
            )
            return

        status_msg = await update.message.reply_text(f"⏳ <code>{country.upper()}</code> {country_flag} BUYING NUMBER... WAIT A FEW SECONDS.", parse_mode="HTML")
        res = buy_vak_number(service=service, country=country, max_price=max_price_limit)

        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            raw_phone = str(res["tel"])
            phone_num = f"+{raw_phone}" if not raw_phone.startswith("+") else raw_phone
            id_num = str(res["idNum"])

            inline_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Check Active OTP", callback_data=f"check_otp_{id_num}")],
                [InlineKeyboardButton("❌ Cancel Number", callback_data=f"cancel_num_{id_num}")]
            ])

            sent_msg = await update.message.reply_text(
                f"✅ <b>𝙽𝚄𝙼𝙱𝙴𝚁 𝙱𝚄𝙸𝙻𝙳 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝚈!</b>\n\n"
                f"📱 <b>Number:</b> <code>{phone_num}</code>\n"
                f"🆔 <b>ID Num:</b> <code>{id_num}</code>\n"
                f"🌍 <b>Country:</b> <code>{country.upper()}</code> {country_flag}\n"
                f"💬 <b>Service:</b> <code>{service.upper()}</code>\n"
                f"💵 <b>Rate:</b> <code>${bot_rate}</code> USDT <i>(𝙊𝙏𝙋 𝘼𝙎𝙇𝙀𝙄 𝘽𝘼𝙻𝘼𝙉𝙲𝙀 𝙆𝘼𝙏𝘽𝙀)</i>\n\n"
                f"⏳ <i>𝙾𝚃𝙿 𝙿𝙾𝚆𝙴𝚁 𝙹𝙾𝙽𝙽𝙾 𝙾𝙿𝙴𝙺𝙺𝙷𝙰 𝙺𝙾𝚁𝚄𝙽...</i>",
                parse_mode="HTML",
                reply_markup=inline_kb
            )

            active_orders[id_num] = {
                "user_id": user_id,
                "service": service,
                "country": country,
                "cost": bot_rate,
                "phone": phone_num,
                "msg_id": sent_msg.message_id
            }

            try:
                await status_msg.delete()
            except Exception:
                pass

            asyncio.create_task(auto_check_otp(context, user_id, id_num, str(phone_num), sent_msg.message_id))
        else:
            err_msg = res.get("error", "Stock Out!") if isinstance(res, dict) else "Stock Out!"
            await update.message.reply_text(f"<emoji id='5386367538735104399'>❌</emoji> <code>{err_msg}</code>", parse_mode="HTML")
        return

    if text == "⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻" and user_id == ADMIN_ID:
        await send_admin_panel(update, context)
        return

async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_str = "🟢 ON (Active)" if is_bot_active() else "🔴 OFF (Maintenance)"
    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 𝗩𝗜𝗘𝗪 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥", callback_data="admin_view_users")],
        [InlineKeyboardButton("🚫 𝗕𝗔𝗡 𝗨𝗦𝗘𝗥", callback_data="admin_ban_start"), InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_start")],
        [InlineKeyboardButton("💵 SET HK WA PRICE", callback_data="admin_rate_wa_hk_start"), InlineKeyboardButton("💵 SET CL WA PRICE", callback_data="admin_rate_wa_cl_start")],
        [InlineKeyboardButton("💵 SET HK TG PRICE", callback_data="admin_rate_tg_hk_start"), InlineKeyboardButton("💵 SET CL TG PRICE", callback_data="admin_rate_tg_cl_start")],
        [InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_bal_start"), InlineKeyboardButton("🔄 𝗭𝗘𝗥𝗢 𝗕𝗔𝙻𝙰𝙽𝙲𝙴", callback_data="admin_zero_bal_start")],
        [InlineKeyboardButton("📢 𝗕𝗥𝗢𝙳𝙲𝙰𝚂𝚃 𝙰𝙻𝙻", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(f"𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦: {status_str}", callback_data="admin_toggle_bot")]
    ])
    if update.message:
        await update.message.reply_text("🛠 <b>Admin Control Panel:</b>", reply_markup=admin_kb, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.message.reply_text("🛠 <b>Admin Control Panel:</b>", reply_markup=admin_kb, parse_mode="HTML")

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "admin_view_users" and user_id == ADMIN_ID:
        try:
            now = datetime.now()
            subscribed_users = list(users_col.find({
                "subscription_expiry": {"$gt": now}
            }))
            if not subscribed_users:
                await query.message.reply_text("<emoji id='5395695537687123235'>📋</emoji> Currently, there are no active subscribed users.", parse_mode="HTML")
                return
            msg = f"👥 <b>Active Subscribed Users ({len(subscribed_users)}):</b>\n\n"
            for u in subscribed_users:
                uid = u.get("user_id", "N/A")
                raw_name = str(u.get("full_name", "User"))
                safe_name = raw_name.replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")
                bal = u.get("balance", 0.0)
                otp_cnt = u.get("otp_count", 0)
                msg += f"• <b>{safe_name}</b> (<code>{uid}</code>)\n  └ 💰 Balance: <code>${bal:.4f}</code> USDT | 📩 OTP Rcv: <code>{otp_cnt}</code>\n\n"
            await query.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            await query.message.reply_text(f"<emoji id='5253742260054409879'>❌</emoji> Error loading users: {str(e)}", parse_mode="HTML")

    elif data == "admin_toggle_bot" and user_id == ADMIN_ID:
        current_status = is_bot_active()
        new_status = not current_status
        set_bot_active(new_status)
        status_text = "🟢 <b>Bot ON (Active) kora hoyeche!</b>" if new_status else "🔴 <b>Bot OFF (Maintenance Mode) kora hoyeche!</b>"
        status_str = "🟢 ON (Active)" if new_status else "🔴 OFF (Maintenance)"
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 𝗩𝗜𝗘𝗪 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥", callback_data="admin_view_users")],
            [InlineKeyboardButton("🚫 𝗕𝗔𝗡 𝗨𝗦𝗘𝗥", callback_data="admin_ban_start"), InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_start")],
            [InlineKeyboardButton("💵 SET HK WA PRICE", callback_data="admin_rate_wa_hk_start"), InlineKeyboardButton("💵 SET CL WA PRICE", callback_data="admin_rate_wa_cl_start")],
            [InlineKeyboardButton("💵 SET HK TG PRICE", callback_data="admin_rate_tg_hk_start"), InlineKeyboardButton("💵 SET CL TG PRICE", callback_data="admin_rate_tg_cl_start")],
            [InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_bal_start"), InlineKeyboardButton("🔄 𝗭𝗘𝗥𝗢 𝗕𝙰𝙻𝙰𝙽𝙲𝙴", callback_data="admin_zero_bal_start")],
            [InlineKeyboardButton("📢 𝗕𝗥𝗢𝙳𝙲𝙰𝚂𝚃 𝙰𝙻𝙻", callback_data="admin_broadcast_start")],
            [InlineKeyboardButton(f"𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦: {status_str}", callback_data="admin_toggle_bot")]
        ])
        try:
            await query.edit_message_reply_markup(reply_markup=admin_kb)
        except Exception:
            pass
        await query.message.reply_text(status_text, parse_mode="HTML")

    elif data.startswith("check_otp_"):
        id_num = data.split("_")[2]
        res = fetch_otp_code(id_num)
        if isinstance(res, dict) and "smsCode" in res and res["smsCode"]:
            otp = res["smsCode"]
            await process_otp_success(context, id_num, otp)
        else:
            await query.message.reply_text("<emoji id='5447410659077661506'>⏳</emoji> 𝙰𝙺𝙷𝙾𝙽𝙾 𝙾𝚃𝙿 𝙰𝚂𝙴𝙽𝙸, 𝙰𝙺𝚃𝚄 𝙿𝙾𝚁𝙴 𝙰𝙱𝙰𝚁 𝚃𝚁𝙸 𝙺𝙾𝚁𝚄𝙽.", parse_mode="HTML")

    elif data.startswith("cancel_num_"):
        id_num = data.split("_")[2]
        if id_num in active_orders:
            set_number_status(id_num, "bad")
            active_orders.pop(id_num, None)
            try:
                await query.edit_message_text(
                    "❌ <b>𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙴𝙳 (𝙱𝙰𝙻𝙰𝙽𝙲𝙴 𝙺𝙰𝚃𝙰 𝙷𝙾𝚈𝙽𝙸).</b>",
                    parse_mode="HTML",
                    reply_markup=None
                )
            except Exception:
                await query.message.delete()
        else:
            await query.message.reply_text("<emoji id='5332455502917949981'>❌</emoji> 𝙳𝙾𝙽'𝚃 𝙰𝙲𝚃𝙸𝚅𝙴 𝙾𝚁𝙳𝙴𝚁 𝙽𝙰𝙷𝙾𝙻𝙴 𝙾𝚃𝙿 𝙰𝙻𝚁𝙴𝙰𝙳𝚈 𝚁𝙴𝙲𝙴𝙸𝚅𝙴𝙳 𝙺𝙾𝚁𝙰 𝙷𝙾𝙸𝙲𝙷𝙴.", parse_mode="HTML")

    elif data.startswith("approve_dep_"):
        parts = data.split("_")
        target_id = int(parts[2])
        amount = float(parts[3])
        users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amount}})
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ <b>Approved & Balance Added!</b>", parse_mode="HTML")
        await context.bot.send_message(chat_id=target_id, text=f"🎉 <b>Apnar `${amount}` USDT deposit shofolbhabe jukto kora hoyeche!</b>", parse_mode="HTML")

    elif data.startswith("reject_dep_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ <b>Deposit Rejected!</b>", parse_mode="HTML")
        await context.bot.send_message(chat_id=target_id, text="❌ Apnar deposit request-ti batil kora hoyeche.")

    elif data.startswith("approve_sub_"):
        target_id = int(data.split("_")[2])
        expiry_date = datetime.now() + timedelta(days=3)
        users_col.update_one({"user_id": target_id}, {"$set": {"subscription_expiry": expiry_date}})
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ <b>Subscription Approved (3 Days Active)!</b>", parse_mode="HTML")
        await context.bot.send_message(
            chat_id=target_id,
            text="🎉 <b>Apnar Subscription Approved hoyeche!</b> 3 Diner jonno bot-er sob features active kora hoyeche.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard(target_id)
        )

    elif data.startswith("reject_sub_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ <b>Subscription Rejected!</b>", parse_mode="HTML")
        await context.bot.send_message(chat_id=target_id, text="❌ Apnar subscription request-ti batil kora hoyeche.")

    elif data.startswith("verify_approve_"):
        target_id = int(data.split("_")[2])
        users_col.update_one({"user_id": target_id}, {"$set": {"is_group_verified": True}})
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ <b>Group Membership Verified!</b>", parse_mode="HTML")
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽(30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        await context.bot.send_message(
            chat_id=target_id,
            text="🎉 <b>Apnar Group Verification Admin কর্তৃক Approved হয়েছে!</b> এখন আপনি নিচের বাটন থেকে সাবস্ক্রিপশন কিনতে পারবেন:",
            parse_mode="HTML",
            reply_markup=sub_kb
        )

    elif data.startswith("verify_reject_"):
        target_id = int(data.split("_")[2])
        users_col.update_one({"user_id": target_id}, {"$set": {"is_group_verified": False}})
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ <b>Group Membership Unverified!</b>", parse_mode="HTML")
        await context.bot.send_message(
            chat_id=target_id,
            text="❌ আপনার গ্রুপ ভেরিফিকেশন প্রুফ সঠিক পাওয়া যায়নি। দয়া করে সঠিক স্ক্রিনশট ও ইউজারনেম দিয়ে পুনরায় চেষ্টা করুন."
        )

async def group_verify_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("✍️ <b>Doya kore apnar Telegram Username-ti likhe pathan (jemon: <code>@username</code>):</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return WAIT_GROUP_USERNAME

async def group_verify_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    context.user_data["verify_username"] = username
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("📸 <b>Ekhon apnar Private Group-e add achen tar Screenshot (Photo) pathan:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return WAIT_GROUP_SCREENSHOT

async def group_verify_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    username = context.user_data.get("verify_username")
    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Verified", callback_data=f"verify_approve_{user.id}"),
            InlineKeyboardButton("❌ Unverified", callback_data=f"verify_reject_{user.id}")
        ]
    ])
    caption = (
        f"🔍 <b>New Private Group Verification Request!</b>\n\n"
        f"👤 <b>User:</b> {user.full_name} (<code>{user.id}</code>)\n"
        f"📌 <b>Username:</b> <code>{username}</code>"
    )
    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
    await update.message.reply_text("✅ <b>Apnar verification request admin-er kache pathano hoyeche!</b> Admin check kore verify korlei apnake subscription option dewa hobe.", parse_mode="HTML")
    return ConversationHandler.END

async def process_otp_success(context, id_num: str, otp: str):
    if id_num not in active_orders:
        return
    order_info = active_orders.pop(id_num)
    uid = order_info["user_id"]
    cost = order_info["cost"]
    phone = order_info["phone"]
    msg_id = order_info["msg_id"]
    service_type = order_info.get("service", "tg").upper()
    country_code = order_info.get("country", "hk")
    country_flag = get_country_flag(country_code)

    users_col.update_one(
        {"user_id": uid},
        {"$inc": {"balance": -cost, "otp_count": 1}}
    )
    updated_user = get_user(uid)
    rem_bal = updated_user.get("balance", 0.0) if updated_user else 0.0
    set_number_status(id_num, "end")

    success_text = (
        f"✅ <b>𝙾𝚃𝙿 𝚁𝙴𝙲𝙴𝙸𝚅𝙴 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝚈!</b>\n\n"
        f"📱 <b>𝙽𝚄𝙼𝙱𝙴𝚁:</b> <code>{phone}</code>\n"
        f"🔑 <b>𝙾𝚃𝙿 𝙲𝙾𝙳𝙴:</b> <code>{otp}</code>\n\n"
        f"💵 <b>𝙱𝙰𝙻𝙰𝙽𝙲𝙴 𝙳𝙴𝙳𝙸𝙲𝙰𝚃𝙴𝙳:</b> <code>${cost}</code> USDT\n"
        f"💰 <b>𝚁𝙴𝙼𝙰𝙸𝙽𝙸𝙽𝙶 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:</b> <code>${rem_bal:.4f}</code> USDT"
    )
    try:
        await context.bot.edit_message_text(
            chat_id=uid,
            message_id=msg_id,
            text=success_text,
            parse_mode="HTML"
        )
    except Exception:
        await context.bot.send_message(chat_id=uid, text=success_text, parse_mode="HTML")

    masked_phone = mask_number(phone)
    group_forward_msg = (
        f"🌐 <b>COUNTRY:</b> <code>{country_code.upper()}</code> {country_flag}\n"
        f"📱 <b>𝙽𝚄𝙼𝙱𝙴𝚁:</b> <code>{masked_phone}</code>\n"
        f"🔑 <b>𝙾𝚃𝙿:</b> <code>{otp}</code>\n"
        f"💬 <b>Message:</b> <code>YOUR {service_type} CODE: {otp}</code>"
    )
    if OTP_GROUP_ID:
        try:
            await context.bot.send_message(
                chat_id=OTP_GROUP_ID,
                text=group_forward_msg,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Failed to forward OTP to group: {e}")

async def auto_check_otp(context: ContextTypes.DEFAULT_TYPE, user_id: int, id_num: str, phone_num: str, msg_id: int):
    for _ in range(35):
        await asyncio.sleep(6)
        if id_num not in active_orders:
            break
        res = fetch_otp_code(id_num)
        if isinstance(res, dict) and "smsCode" in res and res["smsCode"]:
            otp = res["smsCode"]
            await process_otp_success(context, id_num, otp)
            break

async def sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bkash_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌸 𝙱𝙺𝙰𝚂𝙷", callback_data="pay_bkash_sub")],
        [InlineKeyboardButton("❌ 𝙲𝙰𝙽𝙲𝙴𝙻", callback_data="cancel_flow_cb")]
    ])
    await query.message.reply_text("💳 <b>𝙿𝙰𝚈𝙼𝙴𝙽𝚃 𝙼𝙴𝚃𝙷𝙾𝙳 𝚂𝙴𝙻𝙴𝙲𝚃 𝙺𝙾𝚁𝚄𝙽:</b>", parse_mode="HTML", reply_markup=bkash_kb)
    return SUB_AMOUNT

async def sub_bkash_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("📥 <b>𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙰𝙼𝙾𝚄𝙽𝚃 (30 Tk) 𝙻𝙸𝙺𝙷𝚄𝙽:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return SUB_AMOUNT

async def sub_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text != "30":
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text("❌ 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙵𝙴𝙴 𝚂𝚄𝙳𝙷𝚄 <code>30</code> Tk. 𝙴𝙽𝚃𝙴𝚁 <code>30</code> likhun.", parse_mode="HTML", reply_markup=cancel_kb)
        return SUB_AMOUNT
    msg = (
        f"💰 <b>𝙰𝙼𝙾𝚄𝙽𝚃:</b> <code>30</code> Tk\n"
        f"⏳ <b>𝚅𝙰𝙻𝙸𝙳𝙸𝚃𝙸:</b> <code>3 Days</code>\n\n"
        f"👇 <b>𝚂𝙴𝙽𝙳 𝙱𝙺𝙰𝚂𝙷 𝙿𝙴𝚁𝚂𝙾𝙽𝙰𝙻 𝙽𝚄𝙼𝙱𝙴𝚁:</b>\n"
        f"📱 𝙱𝙺𝙰𝚂𝙷 𝙽𝚄𝙼𝙱𝙴𝚁: <code>{ADMIN_BKASH}</code>\n\n"
        f"𝚃𝙰𝙺𝙰 𝙳𝙴𝙰 𝚂𝙴𝚂𝙴 𝚃𝚁𝚇 𝙸𝙳 <b>TrxID</b>-𝚃𝙸 𝙻𝙸𝙺𝙷𝙴 𝙿𝙰𝚃𝙷𝙰𝙽:"
    )
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["sub_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("📸 <b>𝙱𝙺𝙰𝚂𝙷 𝙿𝙰𝚈𝙼𝙴𝙽𝚃 𝚂𝙲𝚁𝙴𝙴𝙽𝚂𝙷𝙾𝚃 (Photo) 𝙳𝙸𝙽:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return SUB_SCREENSHOT

async def sub_screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    txid = context.user_data.get("sub_txid")
    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 𝙰𝙿𝙿𝚁𝙾𝚅𝙴𝙳", callback_data=f"approve_sub_{user.id}"),
            InlineKeyboardButton("❌ 𝚁𝙴𝙹𝙴𝙲𝚃𝙴𝙳", callback_data=f"reject_sub_{user.id}")
        ]
    ])
    caption = (
        f"🔔 <b>𝙽𝙴𝚆 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚁𝙴𝙹𝚄𝙴𝚂𝚃!</b>\n\n"
        f"👤 <b>𝚄𝚂𝙴𝚁:</b> {user.full_name} (<code>{user.id}</code>)\n"
        f"💰 <b>𝙰𝙼𝙾𝚄𝙽𝚃:</b> <code>30 Tk</code>\n"
        f"🧾 <b>𝚃𝚁𝚇𝙸𝙳:</b> <code>{txid}</code>"
    )
    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
    await update.message.reply_text("✅ <b>Apnar subscription request admin-er kache pathano hoyeche!</b> Admin approve korlei bot active hoye jaabe.", parse_mode="HTML")
    return ConversationHandler.END

async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💛 Binance Pay", callback_data="pay_binance")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]
    ])
    await update.message.reply_text("💳 <b>Payment Method select korunk:</b>", parse_mode="HTML", reply_markup=payment_kb)
    return WAITING_AMOUNT

async def deposit_binance_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("📥 <b>Apni koto USDT pathaben ta likhe janan (Minimum: <code>1</code> USDT):</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return WAITING_AMOUNT

async def deposit_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount < 1.0:
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
            await update.message.reply_text("❌ Minimum deposit amount <b>1 USDT</b>.", parse_mode="HTML", reply_markup=cancel_kb)
            return WAITING_AMOUNT
        context.user_data["dep_amount"] = amount
        msg = (
            f"💰 <b>Deposit Amount:</b> <code>{amount}</code> USDT\n\n"
            f"👇 <b>Nicher Binance Pay ID-te Binance app theke Pay/Send Money Korun:</b>\n"
            f"🆔 <b>Binance Pay ID:</b> <code>{BINANCE_ID}</code>\n\n"
            f"Dollar pathanor por apnar <b>Order ID / TxID</b>-ti likhe message din:"
        )
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=cancel_kb)
        return WAITING_TXID
    except ValueError:
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text("❌ Sothik shongkha likhun.", parse_mode="HTML", reply_markup=cancel_kb)
        return WAITING_AMOUNT

async def deposit_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["dep_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("📸 <b>Ekhon apnar payment-er screenshot (Photo) Pathan:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return WAITING_SCREENSHOT

async def deposit_screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    amount = context.user_data.get("dep_amount")
    txid = context.user_data.get("dep_txid")
    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_dep_{user.id}_{amount}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_dep_{user.id}")
        ]
    ])
    caption = (
        f"📥 <b>Notun Deposit Request!</b>\n\n"
        f"👤 <b>User:</b> {user.full_name} (<code>{user.id}</code>)\n"
        f"💰 <b>Amount:</b> <code>${amount}</code> USDT\n"
        f"🧾 <b>TxID:</b> <code>{txid}</code>"
    )
    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="HTML", reply_markup=admin_kb)
    await update.message.reply_text("✅ <b>Apnar deposit request admin-er kache pathano hoyeche!</b>", parse_mode="HTML")
    return ConversationHandler.END

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.message.edit_text("❌ Process batil kora hoyeche.")
    elif update.message:
        await update.message.reply_text("❌ Process batil kora hoyeche.")
    return ConversationHandler.END

async def admin_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🚫 <b>Banned korte chawa User ID-ti likhe pathan:</b>", parse_mode="HTML")
    return ADMIN_BAN

async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
        await update.message.reply_text(f"✅ User <code>{uid}</code>-ke banned kora hoyeche.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")
    return ConversationHandler.END

async def admin_unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("✅ <b>Unban korte chawa User ID-ti likhe pathan:</b>", parse_mode="HTML")
    return ADMIN_UNBAN

async def admin_unban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"✅ User <code>{uid}</code>-ke unban kora hoyeche.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")
    return ConversationHandler.END

async def admin_add_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("➕ <b>Balance add korte chawa User ID-ti pathan:</b>", parse_mode="HTML")
    return ADMIN_ADD_BAL_USER

async def admin_add_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        context.user_data["target_add_uid"] = uid
        await update.message.reply_text(f"💰 <b>User <code>{uid}</code>-er jonno koto USDT balance add korben ta likhun:</b>", parse_mode="HTML")
        return ADMIN_ADD_BAL_AMT
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")
        return ConversationHandler.END

async def admin_add_bal_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip())
        uid = context.user_data.get("target_add_uid")
        users_col.update_one({"user_id": uid}, {"$inc": {"balance": amt}})
        u = get_user(uid)
        new_bal = u.get("balance", 0.0) if u else amt
        await update.message.reply_text(f"✅ Successfully added <code>${amt}</code> USDT to User <code>{uid}</code>.", parse_mode="HTML")
        await context.bot.send_message(chat_id=uid, text=f"🎉 <b>Admin apnar account-e <code>${amt}</code> USDT balance add koreche!</b>", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid Amount.")
    return ConversationHandler.END

async def admin_zero_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🔄 <b>Je user-er balance 0 (zero) korte chan, tar User ID-ti pathan:</b>", parse_mode="HTML")
    return ADMIN_ZERO_BAL_USER

async def admin_zero_bal_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        result = users_col.update_one({"user_id": uid}, {"$set": {"balance": 0.0}})
        if result.matched_count > 0:
            await update.message.reply_text(f"✅ Successfully User <code>{uid}</code>-er balance <b>0 USDT</b> kora hoyeche.", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ Database-e <code>{uid}</code> ID-er kono user pawa jayni.", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")
    return ConversationHandler.END

async def admin_rate_wa_hk_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 <b>Hong Kong (HK) WhatsApp (WA)-er notun Bot Rate USDT-te likhun:</b>", parse_mode="HTML")
    return ADMIN_RATE_WA_HK_SET

async def admin_rate_wa_hk_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("wa", "hk", rate)
        await update.message.reply_text(f"✅ Hong Kong WA Rate update kora hoyeche: <code>${rate}</code> USDT", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format!")
    return ConversationHandler.END

async def admin_rate_wa_cl_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 <b>Chile (CL) WhatsApp (WA)-er notun Bot Rate USDT-te likhun:</b>", parse_mode="HTML")
    return ADMIN_RATE_WA_CL_SET

async def admin_rate_wa_cl_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("wa", "cl", rate)
        await update.message.reply_text(f"✅ Chile WA Rate update kora hoyeche: <code>${rate}</code> USDT", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format!")
    return ConversationHandler.END

async def admin_rate_tg_hk_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 <b>Hong Kong (HK) Telegram (TG)-er notun Bot Rate USDT-te likhun:</b>", parse_mode="HTML")
    return ADMIN_RATE_TG_HK_SET

async def admin_rate_tg_hk_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("tg", "hk", rate)
        await update.message.reply_text(f"✅ Hong Kong TG Rate update kora hoyeche: <code>${rate}</code> USDT", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format!")
    return ConversationHandler.END

async def admin_rate_tg_cl_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 <b>Chile (CL) Telegram (TG)-er notun Bot Rate USDT-te likhun:</b>", parse_mode="HTML")
    return ADMIN_RATE_TG_CL_SET

async def admin_rate_tg_cl_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("tg", "cl", rate)
        await update.message.reply_text(f"✅ Chile TG Rate update kora hoyeche: <code>${rate}</code> USDT", parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format!")
    return ConversationHandler.END

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("📢 <b>Sobai ke broadcast korte chawa message-ti (Text/Photo) ekhane pathan:</b>", parse_mode="HTML", reply_markup=cancel_kb)
    return ADMIN_BROADCAST

async def admin_broadcast_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = list(users_col.find())
    success_count = 0
    fail_count = 0
    status_msg = await update.message.reply_text(f"⏳ <b>Broadcast Process Shuru Hoche... Total Users: {len(all_users)}</b>", parse_mode="HTML")
    for u in all_users:
        uid = u.get("user_id")
        if not uid:
            continue
        try:
            if update.message.photo:
                await context.bot.send_photo(
                    chat_id=uid, 
                    photo=update.message.photo[-1].file_id, 
                    caption=update.message.caption or "",
                    caption_entities=update.message.caption_entities
                )
            else:
                await context.bot.send_message(
                    chat_id=uid, 
                    text=update.message.text or "",
                    entities=update.message.entities
                )
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail_count += 1
    result_text = (
        f"📢 <b>Broadcast Shes Huyeche!</b>\n\n"
        f"✅ <b>Success:</b> <code>{success_count}</code> Users\n"
        f"❌ <b>Failed/Blocked:</b> <code>{fail_count}</code> Users"
    )
    await status_msg.edit_text(result_text, parse_mode="HTML")
    return ConversationHandler.END

# Flask Webhook Endpoint Setup
telegram_app = None

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook_handler():
    if request.method == "POST":
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, telegram_app.bot)
        # Process update asynchronously in the running event loop
        asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), telegram_app.loop)
    return "OK", 200

async def setup_telegram_app():
    global telegram_app
    telegram_app = Application.builder().token(BOT_TOKEN).build()

    group_verify_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(group_verify_start, pattern="^start_group_verify$")],
        states={
            WAIT_GROUP_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_verify_username)],
            WAIT_GROUP_SCREENSHOT: [MessageHandler(filters.PHOTO, group_verify_screenshot)]
        },
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    sub_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sub_start, pattern="^buy_sub_start$")],
        states={
            SUB_AMOUNT: [
                CallbackQueryHandler(sub_bkash_selected, pattern="^pay_bkash_sub$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_amount_received)
            ],
            SUB_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, sub_txid_received)],
            SUB_SCREENSHOT: [MessageHandler(filters.PHOTO, sub_screenshot_received)]
        },
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    deposit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃$"), deposit_start)],
        states={
            WAITING_AMOUNT: [
                CallbackQueryHandler(deposit_binance_selected, pattern="^pay_binance$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_received)
            ],
            WAITING_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_txid_received)],
            WAITING_SCREENSHOT: [MessageHandler(filters.PHOTO, deposit_screenshot_received)]
        },
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_ban_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_ban_start, pattern="^admin_ban_start$")],
        states={ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_unban_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_unban_start, pattern="^admin_unban_start$")],
        states={ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_add_bal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_bal_start, pattern="^admin_add_bal_start$")],
        states={
            ADMIN_ADD_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user)],
            ADMIN_ADD_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_amt)]
        },
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_zero_bal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_zero_bal_start, pattern="^admin_zero_bal_start$")],
        states={ADMIN_ZERO_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_zero_bal_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_rate_wa_hk_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_rate_wa_hk_start, pattern="^admin_rate_wa_hk_start$")],
        states={ADMIN_RATE_WA_HK_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_wa_hk_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_rate_wa_cl_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_rate_wa_cl_start, pattern="^admin_rate_wa_cl_start$")],
        states={ADMIN_RATE_WA_CL_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_wa_cl_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_rate_tg_hk_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_rate_tg_hk_start, pattern="^admin_rate_tg_hk_start$")],
        states={ADMIN_RATE_TG_HK_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_tg_hk_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_rate_tg_cl_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_rate_tg_cl_start, pattern="^admin_rate_tg_cl_start$")],
        states={ADMIN_RATE_TG_CL_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_tg_cl_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast_start$")],
        states={ADMIN_BROADCAST: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, admin_broadcast_process)]},
        fallbacks=[CallbackQueryHandler(ccancel_flow if 'ccancel_flow' in globals() else cancel_flow, pattern="^cancel_flow_cb$")]
    )

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(group_verify_conv)
    telegram_app.add_handler(sub_conv)
    telegram_app.add_handler(deposit_conv)
    telegram_app.add_handler(admin_ban_conv)
    telegram_app.add_handler(admin_unban_conv)
    telegram_app.add_handler(admin_add_bal_conv)
    telegram_app.add_handler(admin_zero_bal_conv)
    telegram_app.add_handler(admin_rate_wa_hk_conv)
    telegram_app.add_handler(admin_rate_wa_cl_conv)
    telegram_app.add_handler(admin_rate_tg_hk_conv)
    telegram_app.add_handler(admin_rate_tg_cl_conv)
    telegram_app.add_handler(admin_broadcast_conv)
    telegram_app.add_handler(CallbackQueryHandler(handle_callbacks))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    await telegram_app.initialize()
    
    # Automatically set webhook if RENDER_EXTERNAL_URL is present, or fallback
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{BOT_TOKEN}"
        await telegram_app.bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook set to: {webhook_url}")
    else:
        logging.warning("RENDER_EXTERNAL_URL not found! Please set webhook manually if needed.")

    await telegram_app.start()

def run_flask_app():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Initialize app in asyncio loop
    loop.run_until_complete(setup_telegram_app())
    
    # Run Flask in background thread sharing the same event loop or thread
    threading.Thread(target=run_flask_app, daemon=True).start()
    
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()

if __name__ == "__main__":
    main()
