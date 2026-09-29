import logging
import os
import threading
import asyncio
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))  # Admin Telegram ID
OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")  # Render Environment Variable
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
ADMIN_BAN, ADMIN_UNBAN, ADMIN_ADD_BAL_USER, ADMIN_ADD_BAL_AMT, ADMIN_ZERO_BAL_USER, ADMIN_RATE_SET, ADMIN_BROADCAST = range(6, 13)

# Helper Functions: Formatting & Masking
def mask_number(phone_str: str) -> str:
    """Masks digits except country prefix and last 4 digits."""
    clean_num = re.sub(r"[^\d+]", "", str(phone_str))
    if len(clean_num) <= 6:
        return clean_num
    prefix = clean_num[:4] if clean_num.startswith("+") else clean_num[:3]
    suffix = clean_num[-4:]
    masked_part = "*" * (len(clean_num) - len(prefix) - len(suffix))
    return f"{prefix}{masked_part}{suffix}"

# Mongo DB Helper Functions
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
            "selected_service": "wa",
            "is_banned": False,
            "subscription_expiry": None
        }
        users_col.insert_one(user_data)
        return user_data
    else:
        users_col.update_one({"user_id": user_id}, {"$set": {"full_name": full_name}})
        return user

def get_rate(service_code: str = "wa"):
    doc = settings_col.find_one({"type": "rates"})
    if doc and service_code in doc.get("rates", {}):
        return float(doc["rates"][service_code])
    if service_code == "tg":
        return 0.087
    return 0.10

def set_rate(service_code: str, rate: float):
    settings_col.update_one(
        {"type": "rates"},
        {"$set": {f"rates.{service_code}": rate}},
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

# Helper Function: Check Subscription Status
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
        [KeyboardButton("💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴"), KeyboardButton("🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁"), KeyboardButton("🛒 𝙱𝚈 𝚃𝙶")],
        [KeyboardButton("🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈"), KeyboardButton("📱 𝚂𝙴𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴")],
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

def buy_vak_number(service: str = "wa", country: str = "hk", max_price: float = 0.07):
    url = f"https://vak-sms.com/api/getNumber/?apiKey={VAK_SMS_API_KEY}&service={service}&country={country}&maxPrice={max_price}"
    try:
        res = requests.get(url).json()
        
        if isinstance(res, dict) and res.get("error") == "noNumber":
            return {"error": f"Stock Out Telegram Number" if service in ["tg", "telegram"] else f"Stock Out for ${max_price} Price Tier!"}
            
        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            assigned_price = res.get("price")
            if assigned_price is not None:
                try:
                    price_val = float(assigned_price)
                    if price_val > max_price:
                        id_num = str(res["idNum"])
                        set_number_status(id_num, "bad")
                        return {"error": "Stock Out Telegram Number" if service in ["tg", "telegram"] else f"Stock Out! Price (${price_val}) exceeded limit."}
                except ValueError:
                    pass

        return res
    except Exception as e:
        return {"error": str(e)}

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
        await update.message.reply_text("❌ 𝘽𝘼𝙉 𝘽𝙔 𝘼𝘿𝙈𝙄𝙉 𝘾𝙊𝙉𝙏𝘼𝘾𝙏 𝘼𝘿𝙈𝙄𝙉.", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and user_id != ADMIN_ID:
        await update.message.reply_text("🚧 **ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.** ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 𝗕𝗨𝗬 𝗦𝗨𝗕𝗦𝗖𝗥𝗜𝗣𝗧𝗜𝗢𝗡(30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        msg = (
            f"👋 **Hello {user.full_name}!**\n\n"
            f"❌ 𝚈𝙾𝚄 𝙳𝙾𝙽'𝚃 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚃𝙷𝙴 𝙱𝙾𝚃!\n"
            f"ʙᴏᴛ ʙᴇʙᴏʜᴀʀ ᴋᴏʀᴛᴇ ᴄʜᴀɪʟᴇ sᴜʙsᴄʀɪᴘᴛɪᴏɴ ɴɪᴛᴇ ʜᴏʙᴇ.\n\n"
            f"📌 **𝗣𝗥𝗜𝗖𝗘:** `30 Tk`\n"
            f"⏳ **𝗩𝗔𝗟𝗜𝗗𝗜𝗧𝗬:** `3 Days`\n\n"
            f"ɴɪᴄʜᴇʀ ᴍᴇɴᴜ ᴛʜᴇᴋᴇ ᴄʟɪᴄᴋ ᴋᴏʀᴇ sᴜʙsᴄ𝚁𝙸𝙿𝚃𝙸𝙾𝙽 ᴋɪɴᴜɴ:"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 **𝙱𝚄𝙸 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽:**", reply_markup=sub_kb)
        return

    exp_time = u_data.get("subscription_expiry")
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin)"

    welcome_msg = (
        f"👋 **𝚆𝙴𝙻𝙲𝙾𝙼𝙴 𝚁𝙴𝚇 𝙿𝚁𝙸𝚅𝙰𝚃𝙴 𝙱𝙾𝚃!**\n\n"
        f"⚙️ **𝚁𝙴𝙲𝙴𝙽𝚃 𝚂𝙴𝚃𝚄𝙿:**\n"
        f"• 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂: `𝙷𝙾𝙽𝙶 𝙺𝙾𝙽𝙶 (𝙷𝙺)` / `𝙲𝙷𝙸𝙻𝙴 (𝙲𝙻)`\n"
        f"• Services: `𝚆𝙷𝙰𝚃𝚂𝙰𝙿𝙿 (𝚆𝙰)` / `𝚃𝙴𝙻𝙴𝙶𝚁𝙰𝙼 (𝚃𝙶)`\n"
        f"• 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${u_data.get('balance', 0.0):.4f} USDT`\n"
        f"• 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚅𝙰𝙻𝙸𝙳 𝚃𝙸𝙻𝙻: `{exp_str}`\n\n"
        f"𝙺𝙰𝙹 𝙺𝙾𝚁𝚃𝙴 𝙽𝙸𝙲𝙷𝙴 𝙳𝙴𝙰 𝙼𝙴𝙽𝚄 𝚄𝚂𝙴 𝙺𝙾𝚁𝚄𝙽:"
    )
    await update.message.reply_text(welcome_msg, parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("❌ 𝚈𝙾𝚄𝚁 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙷𝙰𝚂 𝙱𝙴𝙴𝙽 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽.", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and user_id != ADMIN_ID:
        await update.message.reply_text("🚧 **𝙱𝙾𝚃 𝚄𝙽𝙳𝙴𝚁 𝙼𝙰𝙸𝙽𝚃𝙰𝙸𝙽𝚂 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽.** 𝚃𝚁𝙸 𝚂𝙾𝙼𝙴 𝚃𝙸𝙼𝙴 𝙰𝙶𝙰𝙸𝙽.", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 𝙱𝚄𝙸 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽(30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        await update.message.reply_text("❌ 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙴𝚇𝙿𝙸𝚁𝙴𝚂! 𝙱𝚄𝙸 𝙽𝙴𝚆 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽.", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 **𝙱𝚄𝙸 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽:**", reply_markup=sub_kb)
        return

    text = update.message.text.strip()

    if text == "💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴":
        bot_bal = u_data.get("balance", 0.0)
        msg = f"💰 **𝙼𝚈 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${bot_bal:.4f}` USDT"
        if user_id == ADMIN_ID:
            site_bal = get_vak_balance()
            msg += f"\n🏦 **𝙿𝙰𝙽𝙴𝙻 𝙱𝙰𝙻𝙰𝙽𝙲𝙴 :** `${site_bal:.4f}` USD"
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    if text == "👤 𝙼𝙸 𝙿𝚁𝙾𝙵𝙸𝙻𝙴":
        bot_bal = u_data.get("balance", 0.0)
        otp_cnt = u_data.get("otp_count", 0)
        exp_time = u_data.get("subscription_expiry")
        exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin)"
        profile_msg = (
            f"👤 **Apnar Profile Info:**\n\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"📛 **Name:** {user.full_name}\n"
            f"💵 **Balance:** `${bot_bal:.4f}` USDT\n"
            f"📩 **Total OTP Received:** `{otp_cnt}`\n"
            f"📅 **Subscription Valid:** `{exp_str}`"
        )
        await update.message.reply_text(profile_msg, parse_mode="Markdown")
        return

    if text == "🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈":
        country_kb = [
            [KeyboardButton("COUNTRY: HK (HONG KONG)")],
            [KeyboardButton("🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")]
        ]
        await update.message.reply_text("🌐 **𝙽𝙾𝚆 𝚂𝙴𝙻𝙴𝙲𝚃 𝙾𝙽𝙻𝚈 𝙷𝙾𝙽𝙶𝙺𝙾𝙽𝙶:**", reply_markup=ReplyKeyboardMarkup(country_kb, resize_keyboard=True))
        return

    if text == "COUNTRY: HK (HONG KONG)":
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_country": "hk"}})
        await update.message.reply_text("✅ Country set: `HONG KONG (HK)`", parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))
        return

    if text == "📱 𝚂𝙴𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴":
        service_kb = [
            [KeyboardButton("𝚂𝙴𝚁𝚅𝙸𝙲𝙴: WA (𝚆𝙷𝙰𝚃𝚂𝙰𝙿𝙿)")],
            [KeyboardButton("🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")]
        ]
        await update.message.reply_text("📱 **𝙽𝙾𝚆 𝚂𝙴𝙻𝙴𝙲𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴 𝙾𝙽𝙻𝚈 𝙷𝙾𝙽𝙶𝙺𝙾𝙽𝙶:**", reply_markup=ReplyKeyboardMarkup(service_kb, resize_keyboard=True))
        return

    if text == "𝚂𝙴𝚁𝚅𝙸𝙲𝙴: WA (𝚆𝙷𝙰𝚃𝚂𝙰𝙿𝙿)":
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_service": "wa"}})
        await update.message.reply_text("✅ 𝚂𝙴𝚁𝚅𝙸𝙲𝙴 𝚂𝙴𝚃: `WHATSAPP (WA)`", parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))
        return

    if text == "🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄":
        await start(update, context)
        return

    # Original HongKong WhatsApp Purchase Logic
    if text == "🛒 𝙱𝙸 𝙽𝚄𝙼𝙱𝙴𝚁":
        country = "hk"
        service = "wa"
        bot_rate = get_rate(service)
        user_bal = u_data.get("balance", 0.0)

        if user_bal < bot_rate:
            await update.message.reply_text(
                f"❌ 𝚂𝙾𝚁𝚁𝙸 𝙳𝙾 𝙽𝙾𝚃𝙴 𝙰𝙽𝙰𝙵 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${bot_rate}` USDT, 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${user_bal:.4f}` USDT.\n𝙳𝙸𝙿𝙾𝚂𝙸𝚃 𝙺𝙾𝚁𝚄𝙽."
            )
            return

        status_msg = await update.message.reply_text("⏳ `𝙷𝙺` 𝙲𝙾𝚄𝙽𝚃𝚁𝙸 𝙱𝚄𝙸𝙸𝙽𝙶 𝙽𝚄𝙼𝙱𝙴𝚁 𝚆𝙰𝙸𝚃 𝙵𝙴𝚆 𝚂𝙴𝙲𝙾𝙽𝙳𝚂...")

        res = buy_vak_number(service, country, max_price=0.07)

        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            raw_phone = str(res["tel"])
            phone_num = f"+{raw_phone}" if not raw_phone.startswith("+") else raw_phone
            id_num = str(res["idNum"])

            inline_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Check Active OTP", callback_data=f"check_otp_{id_num}")],
                [InlineKeyboardButton("❌ Cancel Number", callback_data=f"cancel_num_{id_num}")]
            ])

            sent_msg = await update.message.reply_text(
                f"✅ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙱𝚄𝙸 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝙻𝙸!**\n\n"
                f"📱 **Number:** `<code>{phone_num}</code>`\n"
                f"🆔 **ID Num:** `{id_num}`\n"
                f"🌍 **Country:** `HK`\n"
                f"💬 **Service:** `WA`\n"
                f"💵 **Rate:** `${bot_rate}` USDT *(𝙊𝙏𝙋 𝘼𝙎𝙇𝙀𝙄 𝘽𝘼𝙇𝘼𝙉𝘾𝙀 𝙆𝘼𝙏𝘽𝙀)*\n\n"
                f"⏳ *𝙾𝚃𝙿 𝙿𝙾𝚆𝙴𝚁 𝙹𝙾𝙽𝙽𝙾 𝙾𝙿𝙴𝙺𝙺𝙷𝙰 𝙺𝙾𝚁𝚄𝙽...*",
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
            err_msg = res.get("error", "𝚂𝚃𝙾𝙲𝙺 𝙾𝚄𝚃 𝚆𝙰𝙸𝚃") if isinstance(res, dict) else "Error"
            await update.message.reply_text(f"❌ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙺𝙴𝙽𝙰𝚁 𝚂𝙾𝙼𝚅𝙾𝙱 𝙷𝙾𝙸𝙽𝙸:** `{err_msg}`")
        return

    # NEW Chile Telegram Purchase Logic
    if text == "🛒 𝙱𝙸 𝚃𝙶":
        country = "cl"
        service = "tg"
        bot_rate = get_rate("tg")
        user_bal = u_data.get("balance", 0.0)

        if user_bal < bot_rate:
            await update.message.reply_text(
                f"❌ 𝚂𝙾𝚁𝚁𝙸 𝙳𝙾 𝙽𝙾𝚃𝙴 𝙰𝙽𝙰𝙵 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${bot_rate}` USDT, 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${user_bal:.4f}` USDT.\n𝙳𝙸𝙿𝙾𝚂𝙸𝚃 𝙺𝙾𝚁𝚄𝙽."
            )
            return

        status_msg = await update.message.reply_text("⏳ `Chile` 𝙲𝙾𝚄𝙽𝚃𝚁𝙸 𝚃𝙴𝙻𝙴𝙶𝚁𝙰𝙼 𝙽𝚄𝙼𝙱𝙴𝚁 𝙱𝚄𝙸𝙸𝙽𝙶 𝚆𝙰𝙸𝚃...")

        # Strict limit 0.087
        res = buy_vak_number(service="tg", country="cl", max_price=0.087)

        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            raw_phone = str(res["tel"])
            phone_num = f"+{raw_phone}" if not raw_phone.startswith("+") else raw_phone
            id_num = str(res["idNum"])

            inline_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Check Active OTP", callback_data=f"check_otp_{id_num}")],
                [InlineKeyboardButton("❌ Cancel Number", callback_data=f"cancel_num_{id_num}")]
            ])

            sent_msg = await update.message.reply_text(
                f"✅ **𝚃𝙴𝙻𝙴𝙶𝚁𝙰𝙼 𝙽𝚄𝙼𝙱𝙴𝚁 𝙱𝚄𝙸 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝙻𝙸!**\n\n"
                f"📱 **Number:** `<code>{phone_num}</code>`\n"
                f"🆔 **ID Num:** `{id_num}`\n"
                f"🌍 **Country:** `Chile (CL)`\n"
                f"💬 **Service:** `Telegram (TG)`\n"
                f"💵 **Rate:** `${bot_rate}` USDT *(𝙊𝙏𝙋 𝘼𝙎𝙇𝙀𝙄 𝘽𝘼𝙇𝘼𝙉𝘾𝙀 𝙆𝘼𝙏𝘽𝙀)*\n\n"
                f"⏳ *𝙾𝚃𝙿 𝙿𝙾𝚆𝙴𝚁 𝙹𝙾𝙽𝙽𝙾 𝙾𝙿𝙴𝙺𝙺𝙷𝙰 𝙺𝙾𝚁𝚄𝙽...*",
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
            await update.message.reply_text("❌ Stock Out Telegram Number")
        return

    if text == "⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻" and user_id == ADMIN_ID:
        await send_admin_panel(update, context)
        return

async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_str = "🟢 ON (Active)" if is_bot_active() else "🔴 OFF (Maintenance)"
    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 𝗩𝗜𝗘𝗪 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥", callback_data="admin_view_users")],
        [InlineKeyboardButton("🚫 𝗕𝗔𝗡 𝗨𝗦𝗘𝗥", callback_data="admin_ban_start"), InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_start")],
        [InlineKeyboardButton("💵 𝗦𝗘𝗧 𝗪𝗔 𝗣𝗥𝗜𝗖𝗘", callback_data="admin_rate_start"), InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_bal_start")],
        [InlineKeyboardButton("🔄 𝗭𝗘𝗥𝗢 𝗕𝗔𝗟𝗔𝗡𝗖𝗘", callback_data="admin_zero_bal_start")],
        [InlineKeyboardButton("📢 𝗕𝗥𝗢𝗗𝗖𝗔𝗦𝗧 𝗔𝗟𝗟", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(f"𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦: {status_str}", callback_data="admin_toggle_bot")]
    ])
    if update.message:
        await update.message.reply_text("🛠 **Admin Control Panel:**", reply_markup=admin_kb, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text("🛠 **Admin Control Panel:**", reply_markup=admin_kb, parse_mode="Markdown")

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
                await query.message.reply_text("📋 Currently, there are no active subscribed users.")
                return
            
            msg = f"👥 **Active Subscribed Users ({len(subscribed_users)}):**\n\n"
            for u in subscribed_users:
                uid = u.get("user_id", "N/A")
                raw_name = str(u.get("full_name", "User"))
                safe_name = raw_name.replace("*", "").replace("_", "").replace("`", "").replace("[", "").replace("]", "")
                bal = u.get("balance", 0.0)
                otp_cnt = u.get("otp_count", 0)
                msg += f"• **{safe_name}** (`{uid}`)\n  └ 💰 Balance: `${bal:.4f}` USDT | 📩 OTP Rcv: `{otp_cnt}`\n\n"
                
            await query.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            await query.message.reply_text(f"❌ Error loading users: {str(e)}")

    elif data == "admin_toggle_bot" and user_id == ADMIN_ID:
        current_status = is_bot_active()
        new_status = not current_status
        set_bot_active(new_status)
        status_text = "🟢 **Bot ON (Active) kora hoyeche!**" if new_status else "🔴 **Bot OFF (Maintenance Mode) kora hoyeche!**"
        
        status_str = "🟢 ON (Active)" if new_status else "🔴 OFF (Maintenance)"
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 𝗩𝗜𝗘𝗪 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥", callback_data="admin_view_users")],
            [InlineKeyboardButton("🚫 𝗕𝗔𝗡 𝗨𝗦𝗘𝗥", callback_data="admin_ban_start"), InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_start")],
            [InlineKeyboardButton("💵 𝗦𝗘𝗧 𝗪𝗔 𝗣𝗥𝗜𝗖𝗘", callback_data="admin_rate_start"), InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_bal_start")],
            [InlineKeyboardButton("🔄 𝗭𝗘𝗥𝗢 𝗕𝗔𝗟𝗔𝗡𝗖𝗘", callback_data="admin_zero_bal_start")],
            [InlineKeyboardButton("📢 𝗕𝗥𝗢𝗗𝗖𝗔𝗦𝗧 𝗔𝗟𝗟", callback_data="admin_broadcast_start")],
            [InlineKeyboardButton(f"𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦: {status_str}", callback_data="admin_toggle_bot")]
        ])
        try:
            await query.edit_message_reply_markup(reply_markup=admin_kb)
        except Exception:
            pass
        await query.message.reply_text(status_text, parse_mode="Markdown")

    elif data.startswith("check_otp_"):
        id_num = data.split("_")[2]
        res = fetch_otp_code(id_num)
        if isinstance(res, dict) and "smsCode" in res and res["smsCode"]:
            otp = res["smsCode"]
            await process_otp_success(context, id_num, otp)
        else:
            await query.message.reply_text("⏳ 𝙰𝙺𝙷𝙾𝙽𝙾 𝙾𝚃𝙿 𝙰𝚂𝙴𝙽𝙸, 𝙰𝙺𝚃𝚄 𝙿𝙾𝚁𝙴 𝙰𝙱𝙰𝚁 𝚃𝚁𝙸 𝙺𝙾𝚁𝚄𝙽.")

    elif data.startswith("cancel_num_"):
        id_num = data.split("_")[2]
        if id_num in active_orders:
            set_number_status(id_num, "bad")
            active_orders.pop(id_num, None)
            try:
                await query.edit_message_text(
                    "❌ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙴𝙳(𝙱𝙰𝙻𝙰𝙽𝙲𝙴 𝙺𝙰𝚃𝙰 𝙷𝙾𝙸𝙽𝙸).**",
                    reply_markup=None
                )
            except Exception:
                await query.message.delete()
        else:
            await query.message.reply_text("❌ 𝙳𝙾𝙽'𝚃 𝙰𝙲𝚃𝙸𝚅𝙴 𝙾𝚁𝙳𝙴𝚁 𝙽𝙰𝙷𝙾𝙻𝙴 𝙾𝚃𝙿 𝙰𝙻𝚁𝙴𝙰𝙳𝙸 𝚁𝙴𝙲𝙴𝙸𝚅𝙴𝙳 𝙺𝙾𝚁𝙰 𝙷𝙾𝙸𝙲𝙷𝙴.")

    elif data.startswith("approve_dep_"):
        parts = data.split("_")
        target_id = int(parts[2])
        amount = float(parts[3])
        users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amount}})
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **Approved & Balance Added!**")
        await context.bot.send_message(chat_id=target_id, text=f"🎉 **Apnar `${amount}` USDT deposit shofolbhabe jukto kora hoyeche!**")

    elif data.startswith("reject_dep_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **Deposit Rejected!**")
        await context.bot.send_message(chat_id=target_id, text="❌ Apnar deposit request-ti batil kora hoyeche.")

    elif data.startswith("approve_sub_"):
        target_id = int(data.split("_")[2])
        expiry_date = datetime.now() + timedelta(days=3)
        users_col.update_one({"user_id": target_id}, {"$set": {"subscription_expiry": expiry_date}})
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **Subscription Approved (3 Days Active)!**")
        
        await context.bot.send_message(
            chat_id=target_id,
            text="🎉 **Apnar Subscription Approved hoyeche!** 3 Diner jonno bot-er sob features active kora hoyeche.",
            reply_markup=get_main_keyboard(target_id)
        )

    elif data.startswith("reject_sub_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **Subscription Rejected!**")
        await context.bot.send_message(chat_id=target_id, text="❌ Apnar subscription request-ti batil kora hoyeche.")

async def process_otp_success(context, id_num: str, otp: str):
    if id_num not in active_orders:
        return

    order_info = active_orders.pop(id_num)
    uid = order_info["user_id"]
    cost = order_info["cost"]
    phone = order_info["phone"]
    msg_id = order_info["msg_id"]
    service = order_info.get("service", "wa")
    country = order_info.get("country", "hk")

    users_col.update_one(
        {"user_id": uid},
        {"$inc": {"balance": -cost, "otp_count": 1}}
    )
    
    updated_user = get_user(uid)
    rem_bal = updated_user.get("balance", 0.0) if updated_user else 0.0
    set_number_status(id_num, "end")

    success_text = (
        f"✅ **𝙾𝚃𝙿 𝚁𝙴𝙲𝙴𝙸𝚅𝙴 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝙸!**\n\n"
        f"📱 **𝙽𝚄𝙼𝙱𝙴𝚁:** `<code>{phone}</code>`\n"
        f"🔑 **𝙾𝚃𝙿 𝙲𝙾𝙳𝙴:** `<code>{otp}</code>`\n\n"
        f"💵 **𝙱𝙰𝙻𝙰𝙽𝙲𝙴 𝙳𝙴𝙳𝙸𝙲𝙰𝚃𝙴𝙳:** `${cost}` USDT\n"
        f"💰 **𝚁𝙴𝙼𝙰𝙸𝙽𝙸𝙽𝙶 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${rem_bal:.4f}` USDT"
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
    flag = "🇨🇱" if country == "cl" else "🇭🇰"
    serv_title = "TELEGRAM" if service in ["tg", "telegram"] else "WHATSAPP"
    
    group_forward_msg = (
        f"{flag} **𝙽𝚄𝙼𝙱𝙴𝚁:** `{masked_phone}`\n"
        f"🔑 **𝙾𝚃𝙿:** `{otp}`\n"
        f"💬 **Message:** `𝚈𝙾𝚄𝚁 {serv_title} 𝙲𝙾𝙳𝙴: {otp}`"
    )

    if OTP_GROUP_ID:
        try:
            await context.bot.send_message(
                chat_id=OTP_GROUP_ID,
                text=group_forward_msg,
                parse_mode="Markdown"
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
    await query.message.reply_text("💳 **𝙿𝙰𝙸𝙼𝙴𝙽𝚃 𝙼𝙴𝚃𝙷𝙾𝙳 𝚂𝙴𝙻𝙴𝙲𝚃 𝙺𝙾𝚁𝚄𝙽:**", reply_markup=bkash_kb)
    return SUB_AMOUNT

async def sub_bkash_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("📥 **𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙰𝙼𝙾𝚄𝙽𝚃 (30 Tk) 𝙻𝙸𝙺𝙷𝚄𝙽:**", reply_markup=cancel_kb)
    return SUB_AMOUNT

async def sub_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text != "30":
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text("❌ 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙵𝙴𝙴 𝚂𝚄𝙳𝙷𝚄**30** Tk. 𝙴𝙽𝚃𝙴𝚁 `30` likhun.", reply_markup=cancel_kb)
        return SUB_AMOUNT

    msg = (
        f"💰 **𝙰𝙼𝙾𝚄𝙽𝚃:** `30` Tk\n"
        f"⏳ **𝚅𝙰𝙻𝙸𝙳𝙸𝚃𝙸:** `3 Days`\n\n"
        f"👇 **𝚂𝙴𝙽𝙳 𝙱𝙺𝙰𝚂𝙷 𝙿𝙴𝚁𝚂𝙾𝙽𝙰𝙻 𝙽𝚄𝙼𝙱𝙴𝚁:**\n"
        f"📱 𝙱𝙺𝙰𝚂𝙷 𝙽𝚄𝙼𝙱𝙴𝚁: `{ADMIN_BKASH}`\n\n"
        f"𝚃𝙰𝙺𝙰 𝙳𝙴𝙰 𝚂𝙴𝚂𝙴 𝚃𝚁𝚇 𝙸𝙳**TrxID**-𝚃𝙸 𝙻𝙸.HE 𝙿𝙰𝚃𝙷𝙰𝙽:"
    )
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["sub_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("📸 **𝙱𝙺𝙰𝚂𝙷 𝙿𝙰𝙸𝙼𝙴𝙽𝚃 𝚂𝙲𝚁𝙴𝙴𝙽𝚂𝙷𝙾𝚃(Photo) 𝙳𝙸𝙽:**", reply_markup=cancel_kb)
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
        f"🔔 **𝙽𝙴𝚆 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚁𝙴𝙴𝚄𝙴𝚂𝚃!**\n\n"
        f"👤 **𝚄𝚂𝙴𝚁:** {user.full_name} (`{user.id}`)\n"
        f"💰 **𝙰𝙼𝙾𝚄𝙽𝚃:** `30 Tk`\n"
        f"🧾 **𝚃𝚁𝚇𝙸𝙳:** `{txid}`"
    )

    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="Markdown", reply_markup=admin_kb)
    await update.message.reply_text("✅ **Apnar subscription request admin-er kache pathano hoyeche!** Admin approve korlei bot active hoye jaabe.")
    return ConversationHandler.END

async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💛 Binance Pay", callback_data="pay_binance")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]
    ])
    await update.message.reply_text("💳 **Payment Method select korunk:**", reply_markup=payment_kb)
    return WAITING_AMOUNT

async def deposit_binance_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("📥 **Apni koto USDT pathaben ta likhe janan (Minimum: `1` USDT, jemon: `1`, `2.5`, `5`):**", reply_markup=cancel_kb)
    return WAITING_AMOUNT

async def deposit_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount < 1.0:
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
            await update.message.reply_text("❌ Minimum deposit amount **1 USDT**. Doya kore 1 ba tar besi amount likhun.", reply_markup=cancel_kb)
            return WAITING_AMOUNT

        context.user_data["dep_amount"] = amount
        
        msg = (
            f"💰 **Deposit Amount:** `{amount}` USDT\n\n"
            f"👇 **Nicher Binance Pay ID-te Binance app theke Pay/Send Money Korun:**\n"
            f"🆔 **Binance Pay ID:** `{BINANCE_ID}`\n\n"
            f"⚠️ **Note:** Minimum deposit 1 USDT. Binance Pay-er madhyome kono extra fee charai pathano jabe.\n\n"
            f"Dollar pathanor por apnar **Order ID / TxID**-ti likhe message din:"
        )
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_kb)
        return WAITING_TXID
    except ValueError:
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text("❌ Sothik shongkha likhun (jemon: `1` ba `5`).", reply_markup=cancel_kb)
        return WAITING_AMOUNT

async def deposit_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["dep_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("📸 **Ekhon apnar payment-er screenshot (Photo) Pathan:**", reply_markup=cancel_kb)
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
        f"📥 **Notun Deposit Request!**\n\n"
        f"👤 **User:** {user.full_name} (`{user.id}`)\n"
        f"💰 **Amount:** `${amount}` USDT\n"
        f"🧾 **TxID:** `{txid}`"
    )

    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="Markdown", reply_markup=admin_kb)
    await update.message.reply_text("✅ **Apnar deposit request admin-er kache pathano hoyeche!** Jaachai kore druto balance jukto kora hobe.")
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
    await query.message.reply_text("🚫 **Banned korte chawa User ID-ti likhe pathan:**")
    return ADMIN_BAN

async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
        await update.message.reply_text(f"✅ User `{uid}`-ke banned kora hoyeche.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")
    return ConversationHandler.END

async def admin_unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("✅ **Unban korte chawa User ID-ti likhe pathan:**")
    return ADMIN_UNBAN

async def admin_unban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"✅ User `{uid}`-ke unban kora hoyeche.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")
    return ConversationHandler.END

async def admin_add_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("➕ **Balance add korte chawa User ID-ti pathan:**")
    return ADMIN_ADD_BAL_USER

async def admin_add_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        context.user_data["target_add_uid"] = uid
        await update.message.reply_text(f"💰 **User `{uid}`-er jonno koto USDT balance add korben ta likhun:**", parse_mode="Markdown")
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
        
        await update.message.reply_text(f"✅ Successfully added `${amt}` USDT to User `{uid}`. Notun Balance: `${new_bal:.4f}` USDT", parse_mode="Markdown")
        await context.bot.send_message(chat_id=uid, text=f"🎉 **Admin apnar account-e `${amt}` USDT balance add koreche!**")
    except ValueError:
        await update.message.reply_text("❌ Invalid Amount.")
    return ConversationHandler.END

async def admin_zero_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🔄 **Je user-er balance 0 (zero) korte chan, tar User ID-ti pathan:**", parse_mode="Markdown")
    return ADMIN_ZERO_BAL_USER

async def admin_zero_bal_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        result = users_col.update_one({"user_id": uid}, {"$set": {"balance": 0.0}})
        
        if result.matched_count > 0:
            await update.message.reply_text(f"✅ Successfully User `{uid}`-er balance **0 USDT** kora hoyeche.", parse_mode="Markdown")
            try:
                await context.bot.send_message(chat_id=uid, text="⚠️ **Admin apnar account-er balance 0 kore diyeche.**")
            except Exception:
                pass
        else:
            await update.message.reply_text(f"❌ Database-e `{uid}` ID-er kono user pawa jayni.")
            
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID! Sothik shongkha likhun.")
    return ConversationHandler.END

async def admin_rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 **WhatsApp (WA)-er notun Bot Rate USDT-te likhun (jemon: `0.075` ba `0.10`):**")
    return ADMIN_RATE_SET

async def admin_rate_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw_val = update.message.text.strip()
        rate = float(raw_val)
        set_rate("wa", rate)
        await update.message.reply_text(f"✅ WhatsApp Bot Rate update kora hoyeche: `${rate}` USDT", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate Format! (Sothik number likhun, jemon: `0.075`).")
    return ConversationHandler.END

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📢 **Sob user-der jonno Broadcast Message-ti likhe pathan:**")
    return ADMIN_BROADCAST

async def admin_broadcast_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = update.message.text.strip()
    users = list(users_col.find())
    
    count = 0
    for u in users:
        uid = u.get("user_id")
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 **Notice:**\n\n{msg_text}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await update.message.reply_text(f"✅ Total `{count}` jon user-er kache broadcast message pathano hoyeche!", parse_mode="Markdown")
    return ConversationHandler.END

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    sub_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(sub_start, pattern="^buy_sub_start$")],
        states={
            SUB_AMOUNT: [
                CallbackQueryHandler(sub_bkash_selected, pattern="^pay_bkash_sub$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, sub_amount_received)
            ],
            SUB_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, sub_txid_received)],
            SUB_SCREENSHOT: [MessageHandler(filters.PHOTO, sub_screenshot_received)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_flow),
            CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")
        ]
    )

    dep_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃$"), deposit_start)],
        states={
            WAITING_AMOUNT: [
                CallbackQueryHandler(deposit_binance_selected, pattern="^pay_binance$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_received)
            ],
            WAITING_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_txid_received)],
            WAITING_SCREENSHOT: [MessageHandler(filters.PHOTO, deposit_screenshot_received)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel_flow),
            CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")
        ]
    )

    admin_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_ban_start, pattern="^admin_ban_start$"),
            CallbackQueryHandler(admin_unban_start, pattern="^admin_unban_start$"),
            CallbackQueryHandler(admin_add_bal_start, pattern="^admin_add_bal_start$"),
            CallbackQueryHandler(admin_zero_bal_start, pattern="^admin_zero_bal_start$"),
            CallbackQueryHandler(admin_rate_start, pattern="^admin_rate_start$"),
            CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast_start$"),
        ],
        states={
            ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_process)],
            ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban_process)],
            ADMIN_ADD_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user)],
            ADMIN_ADD_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_amt)],
            ADMIN_ZERO_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_zero_bal_process)],
            ADMIN_RATE_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_process)],
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_process)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_flow),
            CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")
        ]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(sub_handler)
    app.add_handler(dep_handler)
    app.add_handler(admin_handler)
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    print("Rex Private Bot Running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
