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

# Multiple Admins Support
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "123456789,987654321")
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_RAW.split(",") if i.strip().isdigit()]

OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")
BINANCE_ID = os.getenv("BINANCE_ID", "907194603")
ADMIN_BKASH = "01858582881"
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB Setup
if not MONGODB_URI:
    logging.error("❌ MONGODB_URI Environment Variable missing!")
client = MongoClient(MONGODB_URI)
db = client["vaksms_child_bot_db"]

users_col = db["users"]
settings_col = db["settings"]

# Flask Web Server for Render Keep-Alive
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Child Telegram Bot (Chile WS) is Active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

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
            "selected_country": "cl",
            "selected_service": "wa",
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
    return 0.10

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
    except Exception:
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
        await update.message.reply_text("🚧 **ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.**", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽", callback_data="buy_sub_start")]
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
        await update.message.reply_text("👇 **𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽:**", reply_markup=sub_kb)
        return

    exp_time = u_data.get("subscription_expiry")
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and not is_admin(user_id)) else "Unlimited (Admin)"

    welcome_msg = (
        f"👋 **𝚆𝙴𝙻𝙲𝙾𝙼𝙴 CHILE WS BOT!**\n\n"
        f"⚙️ **𝚂𝙴𝚁𝚅𝙸𝙲𝙴𝚂:** `WhatsApp` | `Chile (+56)`\n"
        f"⏳ **𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙴𝚇𝙿𝙸𝚁𝙴𝚂:** `{exp_str}`\n\n"
        f"🎯 𝙽𝙸𝙲𝙷𝙴𝚁 𝙼𝙴𝙽𝚄 𝚃𝙷𝙴𝙺𝙴 𝙾𝙿𝚃𝙸𝙾𝙽 𝚂𝙴𝙻𝙴𝙲𝚃 𝙺𝙾𝚁𝚄𝙽:"
    )
    await update.message.reply_text(welcome_msg, parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))

# Subscription Flow
async def sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏳ 5 Days (50 BDT / 0.40 USDT)", callback_data="sub_plan_5d")],
        [InlineKeyboardButton("⏳ 7 Days (70 BDT / 0.56 USDT)", callback_data="sub_plan_7d")],
        [InlineKeyboardButton("❌ 𝙲𝙰𝙽𝙲𝙴𝙻", callback_data="cancel_flow_cb")]
    ])
    await query.message.edit_text("📌 **𝚂𝙴𝙻𝙴𝙲𝚃 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙿𝙻𝙰𝙽:**", parse_mode="Markdown", reply_markup=kb)
    return SUB_PLAN_SELECT

async def sub_plan_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    plan = query.data
    context.user_data["sub_plan"] = plan

    if plan == "sub_plan_5d":
        bdt, usdt, days = 50, 0.40, 5
    else:
        bdt, usdt, days = 70, 0.56, 7

    context.user_data["sub_amount_bdt"] = bdt
    context.user_data["sub_amount_usdt"] = usdt
    context.user_data["sub_days"] = days

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 bKash (Personal)", callback_data="sub_pay_bkash")],
        [InlineKeyboardButton("🪙 Binance Pay (USDT)", callback_data="sub_pay_binance")],
        [InlineKeyboardButton("❌ 𝙲𝙰𝙽𝙲𝙴𝙻", callback_data="cancel_flow_cb")]
    ])
    await query.message.edit_text(
        f"💳 ** Selected Plan:** `{days} Days`\n"
        f"💰 **Amount:** `{bdt} BDT` or `{usdt} USDT`\n\n"
        f"📌 **𝚂𝙴𝙻𝙴𝙲𝚃 𝙿𝙰𝚈𝙼𝙴𝙽𝚃 𝙼𝙴𝚃𝙷𝙾𝙳:**",
        parse_mode="Markdown",
        reply_markup=kb
    )
    return SUB_METHOD_SELECT

async def sub_method_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    method = query.data
    context.user_data["sub_method"] = method
    days = context.user_data["sub_days"]

    if method == "sub_pay_bkash":
        amt = context.user_data["sub_amount_bdt"]
        msg = (
            f"📱 **bKash Payment (Subscription - {days} Days)**\n\n"
            f"💰 **Amount:** `{amt} BDT`\n"
            f"📱 **Send Money Number:** `{ADMIN_BKASH}` (Personal)\n\n"
            f"⚠️ **INSTRUCTION:**\n"
            f"1. Send `{amt} BDT` using Send Money.\n"
            f"2. Send Transaction ID (TrxID) below as text."
        )
    else:
        amt = context.user_data["sub_amount_usdt"]
        msg = (
            f"🪙 **Binance Pay (Subscription - {days} Days)**\n\n"
            f"💰 **Amount:** `{amt} USDT`\n"
            f"🆔 **Binance Pay ID:** `{BINANCE_ID}`\n\n"
            f"⚠️ **INSTRUCTION:**\n"
            f"1. Send `{amt} USDT` to Binance Pay ID.\n"
            f"2. Send Payment Screenshot below."
        )

    await query.message.edit_text(msg, parse_mode="Markdown")
    if method == "sub_pay_bkash":
        await query.message.reply_text("👇 **𝙴𝙽𝚃𝙴𝚁 𝚃𝚁𝙰𝙽𝚂𝙰𝙲𝚃𝙸𝙾𝙽 𝙸𝙳 (TrxID):**")
        return SUB_TXID
    else:
        await query.message.reply_text("👇 **𝚂𝙴𝙽𝙳 𝙿𝙰𝚈𝙼𝙴𝙽𝚃 𝚂𝙲𝚁𝙴𝙴𝙽𝚂𝙷𝙾𝚃:**")
        return SUB_SCREENSHOT

async def sub_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    user = update.effective_user
    days = context.user_data.get("sub_days", 5)
    amt = context.user_data.get("sub_amount_bdt", 50)

    admin_msg = (
        f"🔔 **NEW SUBSCRIPTION REQUEST (bKash)**\n\n"
        f"👤 **User:** {user.full_name} (`{user.id}`)\n"
        f"⏳ **Plan:** `{days} Days` ({amt} BDT)\n"
        f"📝 **TrxID:** `{txid}`"
    )
    
    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"sub_app_{user.id}_{days}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"sub_rej_{user.id}")
        ]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, admin_msg, parse_mode="Markdown", reply_markup=admin_kb)
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")

    await update.message.reply_text("✅ **Request Sent to Admin!** Approval takes 5-30 mins.")
    return ConversationHandler.END

async def sub_screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    user = update.effective_user
    days = context.user_data.get("sub_days", 5)
    amt = context.user_data.get("sub_amount_usdt", 0.40)

    admin_msg = (
        f"🔔 **NEW SUBSCRIPTION REQUEST (Binance)**\n\n"
        f"👤 **User:** {user.full_name} (`{user.id}`)\n"
        f"⏳ **Plan:** `{days} Days` ({amt} USDT)"
    )

    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"sub_app_{user.id}_{days}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"sub_rej_{user.id}")
        ]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(admin_id, photo=photo.file_id, caption=admin_msg, parse_mode="Markdown", reply_markup=admin_kb)
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")

    await update.message.reply_text("✅ **Request Sent to Admin!** Approval takes 5-30 mins.")
    return ConversationHandler.END

# Deposit Flow
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_subscribed(user_id):
        await update.message.reply_text("❌ Subscription Required.")
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 bKash (Personal)", callback_data="pay_bkash")],
        [InlineKeyboardButton("🪙 Binance Pay (USDT)", callback_data="pay_binance")],
        [InlineKeyboardButton("❌ 𝙲𝙰𝙽𝙲𝙴𝙻", callback_data="cancel_flow_cb")]
    ])
    await update.message.reply_text("💳 **𝚂𝙴𝙻𝙴𝙲𝚃 𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝙼𝙴𝚃𝙷𝙾𝙳:**", parse_mode="Markdown", reply_markup=kb)
    return WAITING_AMOUNT

async def deposit_binance_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    msg = (
        f"🪙 **Binance Pay (Balance Deposit)**\n\n"
        f"🆔 **Binance Pay ID:** `{BINANCE_ID}`\n"
        f"💵 **Rate:** `1 USDT = 1 Dollar Balance`\n\n"
        f"⚠️ **INSTRUCTION:**\n"
        f"1. Send USDT to Binance Pay ID.\n"
        f"2. Send Payment Screenshot below."
    )
    await query.message.edit_text(msg, parse_mode="Markdown")
    return WAITING_SCREENSHOT

async def deposit_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        context.user_data["dep_method"] = "bkash"
        msg = (
            f"📱 **bKash Payment (Balance Deposit)**\n\n"
            f"📱 **Send Money Number:** `{ADMIN_BKASH}` (Personal)\n"
            f"💵 **Rate:** `125 BDT = $1.00 Balance`\n\n"
            f"⚠️ **INSTRUCTION:**\n"
            f"1. Send BDT via Send Money.\n"
            f"2. Send TrxID below."
        )
        await query.message.edit_text(msg, parse_mode="Markdown")
        return WAITING_TXID
    return WAITING_AMOUNT

async def deposit_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    user = update.effective_user

    admin_msg = (
        f"💵 **NEW DEPOSIT REQUEST (bKash)**\n\n"
        f"👤 **User:** {user.full_name} (`{user.id}`)\n"
        f"📝 **TrxID:** `{txid}`"
    )

    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accept $1", callback_data=f"dep_app_{user.id}_1"),
            InlineKeyboardButton("✅ Accept $2", callback_data=f"dep_app_{user.id}_2"),
            InlineKeyboardButton("✅ Accept $5", callback_data=f"dep_app_{user.id}_5")
        ],
        [InlineKeyboardButton("❌ Reject", callback_data=f"dep_rej_{user.id}")]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, admin_msg, parse_mode="Markdown", reply_markup=admin_kb)
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")

    await update.message.reply_text("✅ **Deposit Request Sent to Admin!**")
    return ConversationHandler.END

async def deposit_screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    user = update.effective_user

    admin_msg = (
        f"💵 **NEW DEPOSIT REQUEST (Binance)**\n\n"
        f"👤 **User:** {user.full_name} (`{user.id}`)"
    )

    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Accept $1", callback_data=f"dep_app_{user.id}_1"),
            InlineKeyboardButton("✅ Accept $2", callback_data=f"dep_app_{user.id}_2"),
            InlineKeyboardButton("✅ Accept $5", callback_data=f"dep_app_{user.id}_5")
        ],
        [InlineKeyboardButton("❌ Reject", callback_data=f"dep_rej_{user.id}")]
    ])

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(admin_id, photo=photo.file_id, caption=admin_msg, parse_mode="Markdown", reply_markup=admin_kb)
        except Exception as e:
            logging.error(f"Failed to notify admin {admin_id}: {e}")

    await update.message.reply_text("✅ **Deposit Request Sent to Admin!**")
    return ConversationHandler.END

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.message.edit_text("❌ Action Cancelled.")
    return ConversationHandler.END

# Admin Flow Handlers
async def admin_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("ENTER USER ID BAN TO BOT:")
    return ADMIN_BAN

async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.isdigit():
        uid = int(txt)
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
        await update.message.reply_text(f"✅ User `{uid}` Banned Successfully!")
    else:
        await update.message.reply_text("❌ Invalid User ID!")
    return ConversationHandler.END

async def admin_unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("ENTER USER ID UNBAN TO BOT:")
    return ADMIN_UNBAN

async def admin_unban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.isdigit():
        uid = int(txt)
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"✅ User `{uid}` Unbanned Successfully!")
    else:
        await update.message.reply_text("❌ Invalid User ID!")
    return ConversationHandler.END

async def admin_add_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("ENTER USER ID TO ADD BALANCE:")
    return ADMIN_ADD_BAL_USER

async def admin_add_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.isdigit():
        context.user_data["target_bal_user"] = int(txt)
        await update.message.reply_text("ENTER AMOUNT TO ADD ($):")
        return ADMIN_ADD_BAL_AMT
    await update.message.reply_text("❌ Invalid User ID!")
    return ConversationHandler.END

async def admin_add_bal_amt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        amt = float(txt)
        uid = context.user_data.get("target_bal_user")
        users_col.update_one({"user_id": uid}, {"$inc": {"balance": amt}})
        await update.message.reply_text(f"✅ Balance `${amt}` Added to User `{uid}`!")
    except ValueError:
        await update.message.reply_text("❌ Invalid Amount!")
    return ConversationHandler.END

async def admin_zero_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("ENTER USER ID TO ZERO BALANCE:")
    return ADMIN_ZERO_BAL_USER

async def admin_zero_bal_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.isdigit():
        uid = int(txt)
        users_col.update_one({"user_id": uid}, {"$set": {"balance": 0.0}})
        await update.message.reply_text(f"✅ Balance set to $0.00 for User `{uid}`!")
    else:
        await update.message.reply_text("❌ Invalid User ID!")
    return ConversationHandler.END

async def admin_rate_wa_cl_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("ENTER NEW RATE FOR CHILE WHATSAPP ($):")
    return ADMIN_RATE_WA_CL_SET

async def admin_rate_wa_cl_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    try:
        new_r = float(txt)
        set_rate(new_r)
        await update.message.reply_text(f"✅ Rate set to `${new_r}`!")
    except ValueError:
        await update.message.reply_text("❌ Invalid Rate!")
    return ConversationHandler.END

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("ENTER BROADCAST MESSAGE / PHOTO:")
    return ADMIN_BROADCAST

async def admin_broadcast_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_users = users_col.find()
    count = 0
    msg = update.message

    for u in all_users:
        uid = u["user_id"]
        try:
            if msg.photo:
                await context.bot.send_photo(uid, photo=msg.photo[-1].file_id, caption=msg.caption)
            else:
                await context.bot.send_message(uid, text=msg.text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await update.message.reply_text(f"✅ Broadcast Sent to {count} Users!")
    return ConversationHandler.END

# Callback Queries
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data.startswith("sub_app_"):
        parts = data.split("_")
        target_uid = int(parts[2])
        days = int(parts[3])

        current_u = get_user(target_uid)
        now = datetime.now()
        existing_exp = current_u.get("subscription_expiry") if current_u else None

        if existing_exp and existing_exp > now:
            new_exp = existing_exp + timedelta(days=days)
        else:
            new_exp = now + timedelta(days=days)

        users_col.update_one({"user_id": target_uid}, {"$set": {"subscription_expiry": new_exp}})

        await query.answer("Subscripton Approved!")
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ Approved Subscription for `{target_uid}` until {new_exp.strftime('%Y-%m-%d %H:%M')}")

        try:
            await context.bot.send_message(
                target_uid,
                f"🎉 **Your Subscription has been Approved!**\n⏳ Valid until: `{new_exp.strftime('%Y-%m-%d %H:%M')}`\n\nSend /start to access menu."
            )
        except Exception:
            pass
        return

    if data.startswith("sub_rej_"):
        parts = data.split("_")
        target_uid = int(parts[2])

        await query.answer("Subscription Rejected!")
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"❌ Rejected Subscription for `{target_uid}`")

        try:
            await context.bot.send_message(target_uid, "❌ **Your Subscription Request was Rejected by Admin.**")
        except Exception:
            pass
        return

    if data.startswith("dep_app_"):
        parts = data.split("_")
        target_uid = int(parts[2])
        amt = float(parts[3])

        users_col.update_one({"user_id": target_uid}, {"$inc": {"balance": amt}})

        await query.answer("Deposit Approved!")
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ Added `${amt}` to User `{target_uid}`")

        try:
            await context.bot.send_message(target_uid, f"🎉 **Deposit Approved!** `${amt}` added to your balance.")
        except Exception:
            pass
        return

    if data.startswith("dep_rej_"):
        parts = data.split("_")
        target_uid = int(parts[2])

        await query.answer("Deposit Rejected!")
        await query.message.edit_reply_markup(reply_markup=None)
        await query.message.reply_text(f"❌ Rejected Deposit for `{target_uid}`")

        try:
            await context.bot.send_message(target_uid, "❌ **Your Deposit Request was Rejected by Admin.**")
        except Exception:
            pass
        return

    if data == "admin_toggle_bot":
        if not is_admin(user_id):
            return
        curr = is_bot_active()
        set_bot_active(not curr)
        status_str = "OFF 🔴 (Maintenance)" if curr else "ON 🟢 (Active)"
        await query.answer(f"Bot Status Changed: {status_str}")
        await query.message.reply_text(f"⚙️ Bot Status updated to: **{status_str}**", parse_mode="Markdown")
        return

    if data.startswith("cancel_num_"):
        id_num = data.replace("cancel_num_", "")
        set_number_status(id_num, "bad")
        if id_num in active_orders:
            del active_orders[id_num]
        await query.answer("Number Cancelled!")
        await query.message.edit_text("❌ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙻𝙴𝙳 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝙻𝙻𝚈!**")
        return

# Message Reply Handler
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text

    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("❌ 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and not is_admin(user_id):
        await update.message.reply_text("🚧 **ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.**", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        await update.message.reply_text("❌ Subscription Required. Send /start")
        return

    if text == "💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴":
        bal = u_data.get("balance", 0.0)
        await update.message.reply_text(f"💳 **𝚈𝙾𝚄𝚁 𝙲𝚄𝚁𝚁𝙴𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${bal:.2f}`", parse_mode="Markdown")

    elif text == "👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴":
        bal = u_data.get("balance", 0.0)
        otps = u_data.get("otp_count", 0)
        exp_time = u_data.get("subscription_expiry")
        exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and not is_admin(user_id)) else "Unlimited (Admin)"

        prof = (
            f"👤 **𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴**\n\n"
            f"🆔 **USER ID:** `{user_id}`\n"
            f"📛 **NAME:** {user.full_name}\n"
            f"💳 **BALANCE:** `${bal:.2f}`\n"
            f"🔢 **TOTAL OTPs:** `{otps}`\n"
            f"⏳ **SUBSCRIPTION:** `{exp_str}`"
        )
        await update.message.reply_text(prof, parse_mode="Markdown")

    elif text == "🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁":
        cost = get_rate()
        bal = u_data.get("balance", 0.0)

        if bal < cost:
            await update.message.reply_text(
                f"❌ **INSUFFICIENT BALANCE!**\n\n"
                f"• Number Price: `${cost:.2f}`\n"
                f"• Your Balance: `${bal:.2f}`\n\n"
                f"Please deposit balance using 💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃 button.",
                parse_mode="Markdown"
            )
            return

        wait_msg = await update.message.reply_text("🔄 **Fetching Chile WhatsApp Number...**")
        res = buy_vak_number(service="wa", country="cl", max_price=0.079)

        if "error" in res:
            await wait_msg.edit_text("❌ **Stock Out! Please try again later.**")
            return

        if "tel" in res and "idNum" in res:
            raw_tel = str(res["tel"])
            tel = f"+{raw_tel}" if not raw_tel.startswith("+") else raw_tel
            id_num = str(res["idNum"])

            active_orders[id_num] = {
                "user_id": user_id,
                "phone": tel,
                "cost": cost,
                "time": datetime.now()
            }

            cancel_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ 𝙲𝙰𝙽𝙲𝙴𝙻 𝙽𝚄𝙼𝙱𝙴𝚁", callback_data=f"cancel_num_{id_num}")]
            ])

            num_msg = (
                f"✅ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙰𝚂𝚂𝙸𝙶𝙽𝙴𝙳!**\n\n"
                f"📱 **Number:** `{tel}`\n"
                f"💵 **Cost:** `${cost:.2f}`\n"
                f"⏳ **Status:** Waiting for OTP...\n\n"
                f"📌 Copy number and paste in WhatsApp."
            )
            await wait_msg.edit_text(num_msg, parse_mode="Markdown", reply_markup=cancel_kb)

            asyncio.create_task(poll_otp(context, user_id, id_num, tel, cost))
        else:
            await wait_msg.edit_text("❌ **Failed to fetch number. Try again.**")

    elif text == "⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻":
        if not is_admin(user_id):
            return

        bot_bal = get_vak_balance()
        tot_u = users_col.count_documents({})
        curr_rate = get_rate()
        status_str = "🟢 Active" if is_bot_active() else "🔴 Maintenance"

        adm_msg = (
            f"⚙️ **𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝚁𝙾𝙻 𝙿𝙰𝙽𝙴𝙻**\n\n"
            f"🌐 **Vak-SMS Balance:** `${bot_bal}`\n"
            f"👥 **Total Users:** `{tot_u}`\n"
            f"🏷️ **Chile WA Rate:** `${curr_rate}`\n"
            f"🚦 **Bot Status:** {status_str}"
        )

        adm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 BAN USER", callback_data="admin_ban_start"), InlineKeyboardButton("✅ UNBAN USER", callback_data="admin_unban_start")],
            [InlineKeyboardButton("➕ ADD BALANCE", callback_data="admin_add_bal_start"), InlineKeyboardButton("🧹 ZERO BALANCE", callback_data="admin_zero_bal_start")],
            [InlineKeyboardButton("🏷️ SET RATE", callback_data="admin_rate_wa_cl_start"), InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast_start")],
            [InlineKeyboardButton("🚦 TOGGLE BOT ON/OFF", callback_data="admin_toggle_bot")]
        ])
        await update.message.reply_text(adm_msg, parse_mode="Markdown", reply_markup=adm_kb)

# OTP Polling Background Task
async def poll_otp(context: ContextTypes.DEFAULT_TYPE, user_id: int, id_num: str, tel: str, cost: float):
    start_time = datetime.now()
    while (datetime.now() - start_time).seconds < 120:
        await asyncio.sleep(5)

        if id_num not in active_orders:
            return

        res = fetch_otp_code(id_num)
        if "smsCode" in res and res["smsCode"]:
            code = str(res["smsCode"])

            users_col.update_one(
                {"user_id": user_id},
                {"$inc": {"balance": -cost, "otp_count": 1}}
            )

            otp_msg = (
                f"🎉 **𝙾𝚃𝙿 𝚁𝙴𝙲𝙴𝙸𝚅𝙴𝙳!**\n\n"
                f"📱 **Number:** `{tel}`\n"
                f"🔑 **OTP Code:** `{code}`\n\n"
                f"💵 `${cost:.2f}` deducted from your balance."
            )
            try:
                await context.bot.send_message(user_id, otp_msg, parse_mode="Markdown")
            except Exception:
                pass

            if OTP_GROUP_ID:
                masked_p = mask_number(tel)
                grp_msg = (
                    f"🔥 **CHILE WHATSAPP OTP RECEIVED!**\n\n"
                    f"📱 **Number:** `{masked_p}`\n"
                    f"🔑 **OTP:** `{code}`"
                )
                try:
                    await context.bot.send_message(OTP_GROUP_ID, grp_msg, parse_mode="Markdown")
                except Exception:
                    pass

            del active_orders[id_num]
            return

    if id_num in active_orders:
        set_number_status(id_num, "bad")
        del active_orders[id_num]
        try:
            await context.bot.send_message(user_id, f"⏰ **OTP Timeout!** No code received for `{tel}`. No balance deducted.", parse_mode="Markdown")
        except Exception:
            pass

# Application Setup
def main():
    # Start Flask Server
    threading.Thread(target=run_flask, daemon=True).start()

    # Create Bot Application
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(30.0)
        .build()
    )

    # Conversation Handlers
    sub_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(sub_start, pattern="^buy_sub_start$")],
        states={
            SUB_PLAN_SELECT: [CallbackQueryHandler(sub_plan_selected, pattern="^sub_plan_")],
            SUB_METHOD_SELECT: [CallbackQueryHandler(sub_method_selected, pattern="^sub_pay_")],
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
                CallbackQueryHandler(deposit_amount_received, pattern="^pay_bkash$")
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

    admin_rate_wa_cl_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_rate_wa_cl_start, pattern="^admin_rate_wa_cl_start$")],
        states={ADMIN_RATE_WA_CL_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_wa_cl_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast_start$")],
        states={ADMIN_BROADCAST: [MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, admin_broadcast_process)]},
        fallbacks=[CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    # Register Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(sub_conv)
    app.add_handler(deposit_conv)
    app.add_handler(admin_ban_conv)
    app.add_handler(admin_unban_conv)
    app.add_handler(admin_add_bal_conv)
    app.add_handler(admin_zero_bal_conv)
    app.add_handler(admin_rate_wa_cl_conv)
    app.add_handler(admin_broadcast_conv)
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logging.info("🤖 Starting Bot Polling...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
