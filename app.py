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
VAK_SMS_API_KEY = os.getenv("VAK_SMS_API_KEY", "893d842ab70a4e79b4ad323185a69257")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")
BINANCE_ID = os.getenv("BINANCE_ID", "907194603")
ADMIN_BKASH = "01858582881"
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB Setup
if not MONGODB_URI:
    logging.error("❌ MONGODB_URI Environment Variable missing!")
client = MongoClient(MONGODB_URI)
db = client["vaksms_bot_db"]

users_col = db["users"]
settings_col = db["settings"]

# Flask Web Server
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Rex Private Telegram Bot is Active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

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
        return "🇭🇰"
    elif code == "cl":
        return "🇨🇱"
    return "🌐"

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

# Keyboards
def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton("💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴"), KeyboardButton("🛒 𝙱𝚄𝚈 𝙽𝚄𝙼𝙱𝙴𝚁")],
        [KeyboardButton("🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂"), KeyboardButton("📱 𝚂𝙴𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴")],
        [KeyboardButton("👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴"), KeyboardButton("💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃")]
    ]
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton("⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# VAK-SMS API Functions
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

# Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("[❌](tg://emoji?id=5253742260054409879) **YOUR ACCOUNT IS BANNED.**", parse_mode="MarkdownV2", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and user_id != ADMIN_ID:
        await update.message.reply_text("[🛠️](tg://emoji?id=532455502917949981) **BOT IS UNDER MAINTENANCE!**", parse_mode="MarkdownV2")
        return

    if not is_subscribed(user_id):
        if not u_data.get("is_group_verified", False):
            verify_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛡️ Verify Group Membership", callback_data="start_group_verify")]
            ])
            msg = (
                f"[✨](tg://emoji?id=5409048419211682843) *Hello {user.full_name}!*\\n\\n"
                f"[🔒](tg://emoji?id=5206607081334906820) *YOU DON'T HAVE ACTIVE SUBSCRIPTION!*\\n"
                f"বট ব্যবহার করতে চাইলে প্রথমে আমাদের **Private Group**\\-এ জয়েন থাকতে হবে।\\n\\n"
                f"📌 নিচের বাটনে ক্লিক করে আপনার গ্রুপের প্রুফ দিন:"
            )
            await update.message.reply_text(msg, parse_mode="MarkdownV2", reply_markup=ReplyKeyboardRemove())
            await update.message.reply_text("👇 **Group Verification:**", parse_mode="MarkdownV2", reply_markup=verify_kb)
            return

        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 (30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        msg = (
            f"[❌](tg://emoji?id=5253742260054409879) *SUBSCRIPTION EXPIRED!*\\n"
            f"বট ব্যবহার চালিয়ে যেতে সাবস্ক্রিপশন রিনিউ করুন।\\n\\n"
            f"📌 **PRICE:** `30 Tk`\\n"
            f"⏳ **VALIDITY:** `3 Days`"
        )
        await update.message.reply_text(msg, parse_mode="MarkdownV2", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 **Buy Subscription:**", parse_mode="MarkdownV2", reply_markup=sub_kb)
        return

    exp_time = u_data.get("subscription_expiry")
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin)"

    curr_country_code = u_data.get("selected_country", "hk")
    curr_country = curr_country_code.upper()
    country_flag = get_country_flag(curr_country_code)
    curr_service = u_data.get("selected_service", "tg").upper()

    welcome_msg = (
        f"[🌟](tg://emoji?id=5409048419211682843) *WELCOME TO REX PRIVATE BOT!* [🚀](tg://emoji?id=5436113877181941026)\\n\\n"
        f"[⚙️](tg://emoji?id=5206607081334906820) *CURRENT SETUP:*\\n"
        f"• Country: `{curr_country}` {country_flag}\\n"
        f"• Service: `{curr_service}`\\n"
        f"• Balance: `${u_data.get('balance', 0.0):.4f} USDT`\\n"
        f"• Subscription Valid: `{exp_str}`\\n\\n"
        f"🎯 কাজের জন্য নিচের মেনু ব্যবহার করুন:"
    )
    await update.message.reply_text(welcome_msg, parse_mode="MarkdownV2", reply_markup=get_main_keyboard(user_id))

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("[❌](tg://emoji?id=5253742260054409879) **YOUR ACCOUNT IS BANNED.**", parse_mode="MarkdownV2", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and user_id != ADMIN_ID:
        await update.message.reply_text("[🛠️](tg://emoji?id=532455502917949981) **BOT IS UNDER MAINTENANCE!**", parse_mode="MarkdownV2")
        return

    if not is_subscribed(user_id):
        if not u_data.get("is_group_verified", False):
            verify_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛡️ Verify Group Membership", callback_data="start_group_verify")]
            ])
            await update.message.reply_text("❌ প্রথমে প্রাইভেট গ্রুপ ভেরিফিকেশন সম্পন্ন করুন।", parse_mode="MarkdownV2", reply_markup=ReplyKeyboardRemove())
            await update.message.reply_text("👇 **Verification:**", parse_mode="MarkdownV2", reply_markup=verify_kb)
            return

        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 (30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        await update.message.reply_text("❌ Subscription Required!", parse_mode="MarkdownV2", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 **Buy Subscription:**", parse_mode="MarkdownV2", reply_markup=sub_kb)
        return

    text = update.message.text.strip()

    if text == "💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴":
        bot_bal = u_data.get("balance", 0.0)
        msg = f"[💰](tg://emoji?id=5210952531676504517) *MY BALANCE:* `${bot_bal:.4f}` USDT"
        if user_id == ADMIN_ID:
            site_bal = get_vak_balance()
            msg += f"\\n[🏦](tg://emoji?id=5440660757194744323) *PANEL BALANCE:* `${site_bal:.4f}` USD"
        await update.message.reply_text(msg, parse_mode="MarkdownV2")
        return

    if text == "👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴":
        bot_bal = u_data.get("balance", 0.0)
        otp_cnt = u_data.get("otp_count", 0)
        exp_time = u_data.get("subscription_expiry")
        exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin)"
        profile_msg = (
            f"[👤](tg://emoji?id=5240241223632954241) *Your Profile Information:*\\n\\n"
            f"🆔 *User ID:* `{user_id}`\\n"
            f"📛 *Name:* {user.full_name}\\n"
            f"💵 *Balance:* `${bot_bal:.4f}` USDT\\n"
            f"📩 *Total OTP Received:* `{otp_cnt}`\\n"
            f"📅 *Subscription Valid:* `{exp_str}`"
        )
        await update.message.reply_text(profile_msg, parse_mode="MarkdownV2")
        return

    if text in ["🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈", "🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂"]:
        country_kb = [
            [KeyboardButton("🇭🇰 COUNTRY: HK (HONG KONG)"), KeyboardButton("🇨🇱 COUNTRY: CHILE (CL)")],
            [KeyboardButton("🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")]
        ]
        await update.message.reply_text("[🌐](tg://emoji?id=5416081784641168838) *SELECT YOUR COUNTRY:*", parse_mode="MarkdownV2", reply_markup=ReplyKeyboardMarkup(country_kb, resize_keyboard=True))
        return

    if "HK" in text:
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_country": "hk"}})
        await update.message.reply_text("[✅](tg://emoji?id=5411225014148014586) Country set: `HONG KONG (HK)` 🇭🇰", parse_mode="MarkdownV2", reply_markup=get_main_keyboard(user_id))
        return

    if "CHILE" in text or "CL" in text:
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_country": "cl"}})
        await update.message.reply_text("[✅](tg://emoji?id=5411225014148014586) Country set: `CHILE (CL)` 🇨🇱", parse_mode="MarkdownV2", reply_markup=get_main_keyboard(user_id))
        return

    if text == "📱 𝚂𝙴𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴":
        service_kb = [
            [KeyboardButton("📱 SERVICE: TG (TELEGRAM)")],
            [KeyboardButton("📱 SERVICE: WA (WHATSAPP)")],
            [KeyboardButton("🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")]
        ]
        await update.message.reply_text("[📱](tg://emoji?id=5244837092042750681) *SELECT YOUR SERVICE:*", parse_mode="MarkdownV2", reply_markup=ReplyKeyboardMarkup(service_kb, resize_keyboard=True))
        return

    if "TG" in text or "TELEGRAM" in text.upper():
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_service": "tg"}})
        await update.message.reply_text("[✅](tg://emoji?id=5411225014148014586) Service set: `TELEGRAM (TG)`", parse_mode="MarkdownV2", reply_markup=get_main_keyboard(user_id))
        return

    if "WA" in text or "WHATSAPP" in text.upper():
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_service": "wa"}})
        await update.message.reply_text("[✅](tg://emoji?id=5411225014148014586) Service set: `WHATSAPP (WA)`", parse_mode="MarkdownV2", reply_markup=get_main_keyboard(user_id))
        return

    if text == "🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄":
        await start(update, context)
        return

    if text == "🛒 𝙱𝚄𝚈 𝙽𝚄𝙼𝙱𝙴𝚁":
        user_has_active = any(order.get("user_id") == user_id for order in active_orders.values())
        if user_has_active:
            await update.message.reply_text(
                "[⚠️](tg://emoji?id=5334759662677957452) *আপনার অলরেডি একটি নম্বর কেনা রয়েছে!*\\nনতুন নম্বর নেওয়ার আগে আগেরটি সম্পন্ন করুন অথবা ক্যানসেল করুন।",
                parse_mode="MarkdownV2"
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
                f"[❌](tg://emoji?id=5253742260054409879) *INSUFFICIENT BALANCE!*\\nRequired: `${bot_rate}` USDT, Your Balance: `${user_bal:.4f}` USDT.",
                parse_mode="MarkdownV2"
            )
            return

        status_msg = await update.message.reply_text(f"[⏳](tg://emoji?id=5222350726340032308) `{country.upper()}` {country_flag} BUYING NUMBER... PLEASE WAIT.", parse_mode="MarkdownV2")

        res = buy_vak_number(service=service, country=country, max_price=max_price_limit)

        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            raw_phone = str(res["tel"])
            phone_num = f"+{raw_phone}" if not raw_phone.startswith("+") else raw_phone
            id_num = str(res["idNum"])

            inline_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Check Active OTP", callback_data=f"check_otp_{id_num}")],
                [InlineKeyboardButton("❌ Cancel Number", callback_data=f"cancel_num_{id_num}")]
            ])

            sent_msg = await update.message.reply_text(
                f"[✅](tg://emoji?id=5411225014148014586) *NUMBER BUY SUCCESSFUL!*\\n\\n"
                f"[📱](tg://emoji?id=5244837092042750681) *Number:* `{phone_num}`\\n"
                f"[🆔](tg://emoji?id=5240241223632954241) *ID Num:* `{id_num}`\\n"
                f"[🌐](tg://emoji?id=5416081784641168838) *Country:* `{country.upper()}` {country_flag}\\n"
                f"[💬](tg://emoji?id=5337010556253543833) *Service:* `{service.upper()}`\\n"
                f"[💵](tg://emoji?id=5210952531676504517) *Rate:* `${bot_rate}` USDT *(OTP আসলে তবেই ব্যালেন্স কাটা হবে)*\\n\\n"
                f"[⏳](tg://emoji?id=5222350726340032308) *OTP পাওয়ার জন্য অপেক্ষা করা হচ্ছে...*",
                parse_mode="MarkdownV2",
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
            await update.message.reply_text(f"[❌](tg://emoji?id=5253742260054409879) `{err_msg}`", parse_mode="MarkdownV2")
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
        [InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_bal_start"), InlineKeyboardButton("🔄 𝗭𝗘𝗥𝗢 𝗕𝗔𝙻𝗔𝗡𝙲𝗘", callback_data="admin_zero_bal_start")],
        [InlineKeyboardButton("📢 𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧 𝙰𝙻𝙻", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(f"𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦: {status_str}", callback_data="admin_toggle_bot")]
    ])
    if update.message:
        await update.message.reply_text("[🛠️](tg://emoji?id=532455502917949981) *Admin Control Panel:*", reply_markup=admin_kb, parse_mode="MarkdownV2")
    elif update.callback_query:
        await update.callback_query.message.reply_text("[🛠️](tg://emoji?id=532455502917949981) *Admin Control Panel:*", reply_markup=admin_kb, parse_mode="MarkdownV2")

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
                await query.message.reply_text("📋 Currently, there are no active subscribed users.", parse_mode="MarkdownV2")
                return
            
            msg = f"[👥](tg://emoji?id=5240241223632954241) *Active Subscribed Users ({len(subscribed_users)}):*\\n\\n"
            for u in subscribed_users:
                uid = u.get("user_id", "N/A")
                raw_name = str(u.get("full_name", "User"))
                safe_name = raw_name.replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")
                bal = u.get("balance", 0.0)
                otp_cnt = u.get("otp_count", 0)
                msg += f"• *{safe_name}* (`{uid}`)\\n  └ 💰 Balance: `${bal:.4f}` USDT | 📩 OTP Rcv: `{otp_cnt}`\\n\\n"
                
            await query.message.reply_text(msg, parse_mode="MarkdownV2")
        except Exception as e:
            await query.message.reply_text(f"❌ Error loading users: {str(e)}")

    elif data == "admin_toggle_bot" and user_id == ADMIN_ID:
        current_status = is_bot_active()
        new_status = not current_status
        set_bot_active(new_status)
        status_text = "[🟢](tg://emoji?id=5282843764451195532) **Bot ON (Active) করা হয়েছে!**" if new_status else "[🔴](tg://emoji?id=5271604874419647061) **Bot OFF (Maintenance Mode) করা হয়েছে!**"
        
        status_str = "🟢 ON (Active)" if new_status else "🔴 OFF (Maintenance)"
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 𝗩𝗜𝗘𝗪 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥", callback_data="admin_view_users")],
            [InlineKeyboardButton("🚫 𝗕𝗔𝗡 𝗨𝗦𝗘𝗥", callback_data="admin_ban_start"), InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_start")],
            [InlineKeyboardButton("💵 SET HK WA PRICE", callback_data="admin_rate_wa_hk_start"), InlineKeyboardButton("💵 SET CL WA PRICE", callback_data="admin_rate_wa_cl_start")],
            [InlineKeyboardButton("💵 SET HK TG PRICE", callback_data="admin_rate_tg_hk_start"), InlineKeyboardButton("💵 SET CL TG PRICE", callback_data="admin_rate_tg_cl_start")],
            [InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_bal_start"), InlineKeyboardButton("🔄 𝗭𝗘𝗥𝗢 𝗕𝗔𝙻𝗔𝗡𝙲𝗘", callback_data="admin_zero_bal_start")],
            [InlineKeyboardButton("📢 𝗕𝗥𝗢𝗔𝗗𝗖𝗔𝗦𝗧 𝙰𝙻𝙻", callback_data="admin_broadcast_start")],
            [InlineKeyboardButton(f"𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦: {status_str}", callback_data="admin_toggle_bot")]
        ])
        try:
            await query.edit_message_reply_markup(reply_markup=admin_kb)
        except Exception:
            pass
        await query.message.reply_text(status_text, parse_mode="MarkdownV2")

    elif data.startswith("check_otp_"):
        id_num = data.split("_")[2]
        res = fetch_otp_code(id_num)
        if isinstance(res, dict) and "smsCode" in res and res["smsCode"]:
            otp = res["smsCode"]
            await process_otp_success(context, id_num, otp)
        else:
            await query.message.reply_text("[⏳](tg://emoji?id=5222350726340032308) এখনো OTP আসেনি, একটু পরে আবার চেক করুন।", parse_mode="MarkdownV2")

    elif data.startswith("cancel_num_"):
        id_num = data.split("_")[2]
        if id_num in active_orders:
            set_number_status(id_num, "bad")
            active_orders.pop(id_num, None)
            try:
                await query.edit_message_text(
                    "[❌](tg://emoji?id=5253742260054409879) *নম্বরটি ক্যানসেল করা হয়েছে (ব্যালেন্স কাটা হয়নি)।*",
                    parse_mode="MarkdownV2",
                    reply_markup=None
                )
            except Exception:
                await query.message.delete()
        else:
            await query.message.reply_text("❌ কোনো একটিভ অর্ডার নেই অথবা ইতিমধ্যে OTP রিসিভ করা হয়েছে।", parse_mode="MarkdownV2")

    elif data.startswith("approve_dep_"):
        parts = data.split("_")
        target_id = int(parts[2])
        amount = float(parts[3])
        users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amount}})
        await query.edit_message_caption(caption=query.message.caption + "\\n\\n[✅](tg://emoji?id=5411225014148014586) **Approved & Balance Added!**")
        await context.bot.send_message(chat_id=target_id, text=f"[🎉](tg://emoji?id=5447410659077661506) *আপনার `${amount}` USDT ডিপোজিট সফলভাবে যুক্ত করা হয়েছে!*", parse_mode="MarkdownV2")

    elif data.startswith("reject_dep_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_caption(caption=query.message.caption + "\\n\\n[❌](tg://emoji?id=5253742260054409879) **Deposit Rejected!**")
        await context.bot.send_message(chat_id=target_id, text="❌ আপনার ডিপোজিট রিকোয়েস্টটি বাতিল করা হয়েছে।")

    elif data.startswith("approve_sub_"):
        target_id = int(data.split("_")[2])
        expiry_date = datetime.now() + timedelta(days=3)
        users_col.update_one({"user_id": target_id}, {"$set": {"subscription_expiry": expiry_date}})
        await query.edit_message_caption(caption=query.message.caption + "\\n\\n[✅](tg://emoji?id=5411225014148014586) **Subscription Approved (3 Days Active)!**")
        
        await context.bot.send_message(
            chat_id=target_id,
            text="[🎉](tg://emoji?id=5447410659077661506) *আপনার সাবস্ক্রিপশন এপ্রুভ হয়েছে!* ৩ দিনের জন্য বটের সব ফিচার অ্যাক্টিভ করা হয়েছে।",
            parse_mode="MarkdownV2",
            reply_markup=get_main_keyboard(target_id)
        )

    elif data.startswith("reject_sub_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_caption(caption=query.message.caption + "\\n\\n[❌](tg://emoji?id=5253742260054409879) **Subscription Rejected!**")
        await context.bot.send_message(chat_id=target_id, text="❌ আপনার সাবস্ক্রিপশন রিকোয়েস্টটি বাতিল করা হয়েছে।")

    elif data.startswith("verify_approve_"):
        target_id = int(data.split("_")[2])
        users_col.update_one({"user_id": target_id}, {"$set": {"is_group_verified": True}})
        await query.edit_message_caption(caption=query.message.caption + "\\n\\n[✅](tg://emoji?id=5411225014148014586) **Group Membership Verified!**")
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 (30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        await context.bot.send_message(
            chat_id=target_id,
            text="[🎉](tg://emoji?id=5447410659077661506) *আপনার গ্রুপ ভেরিফিকেশন এপ্রুভ হয়েছে!* এখন আপনি সাবস্ক্রিপশন কিনতে পারবেন:",
            parse_mode="MarkdownV2",
            reply_markup=sub_kb
        )

    elif data.startswith("verify_reject_"):
        target_id = int(data.split("_")[2])
        users_col.update_one({"user_id": target_id}, {"$set": {"is_group_verified": False}})
        await query.edit_message_caption(caption=query.message.caption + "\\n\\n[❌](tg://emoji?id=5253742260054409879) **Group Membership Unverified!**")
        await context.bot.send_message(
            chat_id=target_id,
            text="❌ আপনার গ্রুপ ভেরিফিকেশন প্রুফ সঠিক পাওয়া যায়নি। দয়া করে সঠিক স্ক্রিনশট দিয়ে আবার চেষ্টা করুন।"
        )

# Group Verification Conversation Handlers
async def group_verify_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("[✍️](tg://emoji?id=5397916757333654639) *আপনার Telegram Username-টি লিখে পাঠান (যেমন: `@username`):*", parse_mode="MarkdownV2", reply_markup=cancel_kb)
    return WAIT_GROUP_USERNAME

async def group_verify_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip()
    context.user_data["verify_username"] = username
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("[📸](tg://emoji?id=5386367538735104399) *এখন প্রাইভেট গ্রুপে যে জয়েন আছেন তার একটি স্ক্রিনশট (Photo) পাঠান:*", parse_mode="MarkdownV2", reply_markup=cancel_kb)
    return WAIT_GROUP_SCREENSHOT

async def group_verify_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    username = context.user_data.get("verify_username")

    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Verify", callback_data=f"verify_approve_{user.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"verify_reject_{user.id}")
        ]
    ])

    caption = (
        f"[🔍](tg://emoji?id=5395695537687123235) *New Private Group Verification Request!*\\n\\n"
        f"[👤](tg://emoji?id=5240241223632954241) *User:* {user.full_name} (`{user.id}`)\\n"
        f"📌 *Username:* `{username}`"
    )

    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="MarkdownV2", reply_markup=admin_kb)
    await update.message.reply_text("[✅](tg://emoji?id=5411225014148014586) *আপনার ভেরিফিকেশন রিকোয়েস্ট এডমিনের কাছে পাঠানো হয়েছে!*", parse_mode="MarkdownV2")
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
        f"[✅](tg://emoji?id=5411225014148014586) *OTP RECEIVED SUCCESSFUL!*\\n\\n"
        f"[📱](tg://emoji?id=5244837092042750681) *Number:* `{phone}`\\n"
        f"[🔑](tg://emoji?id=5424972470023104089) *OTP Code:* `{otp}`\\n\\n"
        f"[💵](tg://emoji?id=5210952531676504517) *Balance Deducted:* `${cost}` USDT\\n"
        f"[💰](tg://emoji?id=5210952531676504517) *Remaining Balance:* `${rem_bal:.4f}` USDT"
    )

    try:
        await context.bot.edit_message_text(
            chat_id=uid,
            message_id=msg_id,
            text=success_text,
            parse_mode="MarkdownV2"
        )
    except Exception:
        await context.bot.send_message(chat_id=uid, text=success_text, parse_mode="MarkdownV2")

    masked_phone = mask_number(phone)
    group_forward_msg = (
        f"[🌐](tg://emoji?id=5416081784641168838) *COUNTRY:* `{country_code.upper()}` {country_flag}\\n"
        f"[📱](tg://emoji?id=5244837092042750681) *Number:* `{masked_phone}`\\n"
        f"[🔑](tg://emoji?id=5424972470023104089) *OTP:* `{otp}`\\n"
        f"[💬](tg://emoji?id=5337010556253543833) *Message:* `YOUR {service_type} CODE: {otp}`"
    )

    if OTP_GROUP_ID:
        try:
            await context.bot.send_message(
                chat_id=OTP_GROUP_ID,
                text=group_forward_msg,
                parse_mode="MarkdownV2"
            )
            logging.info(f"OTP Forwarded to Group {OTP_GROUP_ID} successfully.")
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
    await query.message.reply_text("[💳](tg://emoji?id=5210952531676504517) *PAYMENT METHOD SELECT করুন:*", parse_mode="MarkdownV2", reply_markup=bkash_kb)
    return SUB_AMOUNT

async def sub_bkash_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("[📥](tg://emoji?id=5289967092265660622) *SUBSCRIPTION AMOUNT (30 Tk) লিখুন:*", parse_mode="MarkdownV2", reply_markup=cancel_kb)
    return SUB_AMOUNT

async def sub_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text != "30":
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text("❌ সাবস্ক্রিপশন ফি শুধুমাত্র **30** টাকা। দয়া করে `30` লিখুন।", parse_mode="MarkdownV2", reply_markup=cancel_kb)
        return SUB_AMOUNT

    msg = (
        f"[💰](tg://emoji?id=5210952531676504517) *Amount:* `30` Tk\\n"
        f"[⏳](tg://emoji?id=5222350726340032308) *Validity:* `3 Days`\\n\\n"
        f"👇 *SEND MONEY TO BKASH PERSONAL:*\\n"
        f"[📱](tg://emoji?id=5244837092042750681) bKash Number: `{ADMIN_BKASH}`\\n\\n"
        f"টাকা পাঠানোর পর আপনার **TrxID**-টি লিখে পাঠান:"
    )
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text(msg, parse_mode="MarkdownV2", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["sub_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("[📸](tg://emoji?id=5386367538735104399) *bKash Payment-এর Screenshot (Photo) দিন:*", parse_mode="MarkdownV2", reply_markup=cancel_kb)
    return SUB_SCREENSHOT

async def sub_screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    txid = context.user_data.get("sub_txid")

    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_sub_{user.id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_sub_{user.id}")
        ]
    ])

    caption = (
        f"[🔔](tg://emoji?id=5960914406366779993) *NEW SUBSCRIPTION REQUEST!*\\n\\n"
        f"[👤](tg://emoji?id=5240241223632954241) *User:* {user.full_name} (`{user.id}`)\\n"
        f"[💰](tg://emoji?id=5210952531676504517) *Amount:* `30 Tk`\\n"
        f"🧾 *TrxID:* `{txid}`"
    )

    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="MarkdownV2", reply_markup=admin_kb)
    await update.message.reply_text("[✅](tg://emoji?id=5411225014148014586) *আপনার সাবস্ক্রিপশন রিকোয়েস্ট এডমিনের কাছে পাঠানো হয়েছে!*", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💛 Binance Pay", callback_data="pay_binance")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]
    ])
    await update.message.reply_text("[💳](tg://emoji?id=5210952531676504517) *Payment Method select করুন:*", parse_mode="MarkdownV2", reply_markup=payment_kb)
    return WAITING_AMOUNT

async def deposit_binance_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("[📥](tg://emoji?id=5289967092265660622) *কত USDT ডিপোজিট করবেন তা লিখুন (Minimum: `1` USDT):*", parse_mode="MarkdownV2", reply_markup=cancel_kb)
    return WAITING_AMOUNT

async def deposit_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount < 1.0:
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
            await update.message.reply_text("❌ Minimum deposit amount **1 USDT**. দয়া করে ১ বা তার বেশি লিখুন।", parse_mode="MarkdownV2", reply_markup=cancel_kb)
            return WAITING_AMOUNT

        context.user_data["dep_amount"] = amount
        
        msg = (
            f"[💰](tg://emoji?id=5210952531676504517) *Deposit Amount:* `{amount}` USDT\\n\\n"
            f"👇 *Binance Pay ID-তে ডলার সেন্ড করুন:*\\n"
            f"[🆔](tg://emoji?id=5240241223632954241) Binance Pay ID: `{BINANCE_ID}`\\n\\n"
            f"টাকা পাঠানোর পর আপনার **TxID**-টি লিখে পাঠান:"
        )
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text(msg, parse_mode="MarkdownV2", reply_markup=cancel_kb)
        return WAITING_TXID
    except ValueError:
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text("❌ সঠিক সংখ্যা লিখুন (যেমন: `1` বা `5`).", parse_mode="MarkdownV2", reply_markup=cancel_kb)
        return WAITING_AMOUNT

async def deposit_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["dep_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("[📸](tg://emoji?id=5386367538735104399) *এখন আপনার পেমেন্টের স্ক্রিনশট (Photo) পাঠান:*", parse_mode="MarkdownV2", reply_markup=cancel_kb)
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
        f"[📥](tg://emoji?id=5289967092265660622) *New Deposit Request!*\\n\\n"
        f"[👤](tg://emoji?id=5240241223632954241) *User:* {user.full_name} (`{user.id}`)\\n"
        f"[💰](tg://emoji?id=5210952531676504517) *Amount:* `${amount}` USDT\\n"
        f"🧾 *TxID:* `{txid}`"
    )

    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="MarkdownV2", reply_markup=admin_kb)
    await update.message.reply_text("[✅](tg://emoji?id=5411225014148014586) *আপনার ডিপোজিট রিকোয়েস্ট এডমিনের কাছে পাঠানো হয়েছে!*", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.message.edit_text("❌ প্রসেস বাতিল করা হয়েছে।")
    elif update.message:
        await update.message.reply_text("❌ প্রসেস বাতিল করা হয়েছে।")
    return ConversationHandler.END

async def admin_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🚫 **ব্যান করতে চাওয়া User ID-টি লিখে পাঠান:**", parse_mode="MarkdownV2")
    return ADMIN_BAN

async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
        await update.message.reply_text(f"[✅](tg://emoji?id=5411225014148014586) User `{uid}`-কে ব্যান করা হয়েছে।", parse_mode="MarkdownV2")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def admin_unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("✅ **আনব্যান করতে চাওয়া User ID-টি লিখে পাঠান:**", parse_mode="MarkdownV2")
    return ADMIN_UNBAN

async def admin_unban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"[✅](tg://emoji?id=5411225014148014586) User `{uid}`-কে আনব্যান করা হয়েছে।", parse_mode="MarkdownV2")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def admin_add_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("➕ **ব্যালেন্স অ্যাড করতে চাওয়া User ID-টি পাঠান:**", parse_mode="MarkdownV2")
    return ADMIN_ADD_BAL_USER

async def admin_add_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        context.user_data["target_add_uid"] = uid
        await update.message.reply_text(f"💰 *User `{uid}`-এর জন্য কত USDT ব্যালেন্স অ্যাড করবেন তা লিখুন:*", parse_mode="MarkdownV2")
        return ADMIN_ADD_BAL_AMT
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.", parse_mode="MarkdownV2")
        return ConversationHandler.END

async def admin_add_bal_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.strip())
        uid = context.user_data.get("target_add_uid")
        users_col.update_one({"user_id": uid}, {"$inc": {"balance": amt}})
        
        u = get_user(uid)
        new_bal = u.get("balance", 0.0) if u else amt
        
        await update.message.reply_text(f"[✅](tg://emoji?id=5411225014148014586) Successfully added `${amt}` USDT to User `{uid}`. New Balance: `${new_bal:.4f}` USDT", parse_mode="MarkdownV2")
        await context.bot.send_message(chat_id=uid, text=f"[🎉](tg://emoji?id=5447410659077661506) *এডমিন আপনার অ্যাকাউন্টে `${amt}` USDT ব্যালেন্স অ্যাড করেছেন!*", parse_mode="MarkdownV2")
    except ValueError:
        await update.message.reply_text("❌ Invalid Amount.", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def admin_zero_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🔄 **যে ইউজারের ব্যালেন্স ০ করতে চান, তার User ID-টি পাঠান:**", parse_mode="MarkdownV2")
    return ADMIN_ZERO_BAL_USER

async def admin_zero_bal_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        result = users_col.update_one({"user_id": uid}, {"$set": {"balance": 0.0}})
        
        if result.matched_count > 0:
            await update.message.reply_text(f"[✅](tg://emoji?id=5411225014148014586) Successfully User `{uid}`-এর ব্যালেন্স **0 USDT** করা হয়েছে।", parse_mode="MarkdownV2")
            try:
                await context.bot.send_message(chat_id=uid, text="⚠ **এডমিন আপনার অ্যাকাউন্টের ব্যালেন্স জিরো করে দিয়েছেন।**")
            except Exception:
                pass
        else:
            await update.message.reply_text(f"❌ Database-এ `{uid}` ID-এর কোনো user পাওয়া যায়নি।", parse_mode="MarkdownV2")
            
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def admin_rate_wa_hk_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 **Hong Kong (HK) WhatsApp (WA) নতুন রেট লিখুন:**", parse_mode="MarkdownV2")
    return ADMIN_RATE_WA_HK_SET

async def admin_rate_wa_hk_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("wa", "hk", rate)
        await update.message.reply_text(f"[✅](tg://emoji?id=5411225014148014586) Hong Kong WA Rate updated: `${rate}` USDT", parse_mode="MarkdownV2")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format!", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def admin_rate_wa_cl_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 **Chile (CL) WhatsApp (WA) নতুন রেট লিখুন:**", parse_mode="MarkdownV2")
    return ADMIN_RATE_WA_CL_SET

async def admin_rate_wa_cl_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("wa", "cl", rate)
        await update.message.reply_text(f"[✅](tg://emoji?id=5411225014148014586) Chile WA Rate updated: `${rate}` USDT", parse_mode="MarkdownV2")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format!", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def admin_rate_tg_hk_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 **Hong Kong (HK) Telegram (TG) নতুন রেট লিখুন:**", parse_mode="MarkdownV2")
    return ADMIN_RATE_TG_HK_SET

async def admin_rate_tg_hk_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("tg", "hk", rate)
        await update.message.reply_text(f"[✅](tg://emoji?id=5411225014148014586) Hong Kong TG Rate updated: `${rate}` USDT", parse_mode="MarkdownV2")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format!", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def admin_rate_tg_cl_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 **Chile (CL) Telegram (TG) নতুন রেট লিখুন:**", parse_mode="MarkdownV2")
    return ADMIN_RATE_TG_CL_SET

async def admin_rate_tg_cl_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("tg", "cl", rate)
        await update.message.reply_text(f"[✅](tg://emoji?id=5411225014148014586) Chile TG Rate updated: `${rate}` USDT", parse_mode="MarkdownV2")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format!", parse_mode="MarkdownV2")
    return ConversationHandler.END

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("[📢](tg://emoji?id=5292166459118606932) *ব্রডকাস্ট করার জন্য মেসেজ বা ফটো এখানে পাঠান:*", parse_mode="MarkdownV2", reply_markup=cancel_kb)
    return ADMIN_BROADCAST

async def admin_broadcast_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = list(users_col.find())
    success_count = 0
    fail_count = 0
    
    status_msg = await update.message.reply_text(f"[⏳](tg://emoji?id=5222350726340032308) *Broadcast Started... Total Users: {len(all_users)}*", parse_mode="MarkdownV2")
    
    for u in all_users:
        uid = u.get("user_id")
        if not uid:
            continue
        try:
            if update.message.photo:
                photo_file_id = update.message.photo[-1].file_id
                caption_text = update.message.caption or ""
                caption_entities = update.message.caption_entities
                await context.bot.send_photo(
                    chat_id=uid, 
                    photo=photo_file_id, 
                    caption=caption_text,
                    caption_entities=caption_entities
                )
            else:
                text_content = update.message.text or ""
                text_entities = update.message.entities
                await context.bot.send_message(
                    chat_id=uid, 
                    text=text_content,
                    entities=text_entities
                )
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail_count += 1

    result_text = (
        f"[📢](tg://emoji?id=5292166459118606932) *Broadcast Completed!*\\n\\n"
        f"[✅](tg://emoji?id=5411225014148014586) *Success:* `{success_count}` Users\\n"
        f"[❌](tg://emoji?id=5253742260054409879) *Failed:* `{fail_count}` Users"
    )
    await status_msg.edit_text(result_text, parse_mode="MarkdownV2")
    return ConversationHandler.END

async def main_async():
    app = Application.builder().token(BOT_TOKEN).build()

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
            WAITING_TXID: [MessageHandler(filters.TEXT & ~filters.Command, deposit_txid_received)],
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
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(group_verify_conv)
    app.add_handler(sub_conv)
    app.add_handler(deposit_conv)
    app.add_handler(admin_ban_conv)
    app.add_handler(admin_unban_conv)
    app.add_handler(admin_add_bal_conv)
    app.add_handler(admin_zero_bal_conv)
    app.add_handler(admin_rate_wa_hk_conv)
    app.add_handler(admin_rate_wa_cl_conv)
    app.add_handler(admin_rate_tg_hk_conv)
    app.add_handler(admin_rate_tg_cl_conv)
    app.add_handler(admin_broadcast_conv)
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logging.info("🤖 Bot startup sequence completed. Polling started successfully.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    stop_event = asyncio.Event()
    await stop_event.wait()

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
