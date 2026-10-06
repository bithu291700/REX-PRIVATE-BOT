import logging
import os
import threading
import asyncio
import re
from datetime import datetime
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

# Support multiple admins (comma-separated string parsed into a list of ints)
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "123456789")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")
BINANCE_ID = os.getenv("BINANCE_ID", "907194603")
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB Setup
if not MONGODB_URI:
    logging.error("❌ MONGODB_URI Environment Variable missing!")
client = MongoClient(MONGODB_URI)
db = client["chile_wa_bot_db"]

users_col = db["users"]
settings_col = db["settings"]

# Flask Web Server
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Chile WA Telegram Bot is Active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# In-Memory Active Orders
active_orders = {}

# Conversation States
WAITING_AMOUNT, WAITING_TXID, WAITING_SCREENSHOT = range(3)
(
    ADMIN_BAN,
    ADMIN_UNBAN,
    ADMIN_ADD_BAL_USER,
    ADMIN_ADD_BAL_AMT,
    ADMIN_ZERO_BAL_USER,
    ADMIN_RATE_WA_CL_SET,
    ADMIN_BROADCAST,
) = range(3, 10)

# Helper Functions
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def mask_number(phone_str: str) -> str:
    clean_num = re.sub(r"[^\d+]", "", str(phone_str))
    if len(clean_num) <= 6:
        return clean_num
    prefix = clean_num[:4] if clean_num.startswith("+") else clean_num[:3]
    suffix = clean_num[-4:]
    masked_part = "*" * (len(clean_num) - len(suffix) - len(prefix))
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
    return 0.10  # Default rate for Chile WA

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
    url = f"[https://vak-sms.com/api/setStatus/?apiKey=](https://vak-sms.com/api/setStatus/?apiKey=){VAK_SMS_API_KEY}&idNum={id_num}&status={status}"
    try:
        return requests.get(url).json()
    except Exception as e:
        return {"error": str(e)}

def get_vak_balance():
    url = f"[https://vak-sms.com/api/getBalance/?apiKey=](https://vak-sms.com/api/getBalance/?apiKey=){VAK_SMS_API_KEY}"
    try:
        res = requests.get(url).json()
        return float(res.get("balance", 0.0))
    except Exception:
        return 0.0

def buy_vak_number(max_price: float = 0.087):
    current_panel_bal = get_vak_balance()
    if current_panel_bal < max_price:
        return {"error": "Stock Out!"}

    url = f"[https://vak-sms.com/api/getNumber/?apiKey=](https://vak-sms.com/api/getNumber/?apiKey=){VAK_SMS_API_KEY}&service=wa&country=cl&maxPrice={max_price}"
    try:
        res = requests.get(url).json()
        
        if isinstance(res, dict) and res.get("error") in ["noNumber", "noBalance"]:
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

def fetch_otp_code(id_num: str):
    url = f"[https://vak-sms.com/api/getSmsCode/?apiKey=](https://vak-sms.com/api/getSmsCode/?apiKey=){VAK_SMS_API_KEY}&idNum={id_num}"
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

    welcome_msg = (
        f"👋 **𝚆𝙴𝙻𝙲𝙾𝙼𝙴 𝙲𝙷𝙸𝙻𝙴 𝚆𝙷𝙰𝚃𝚂𝙰𝙿𝙿 𝙱𝙾𝚃!**\n\n"
        f"⚙️ ** 𝚃𝙶𝚂 𝚆𝙰 𝙽𝚄𝙼𝙱𝙴𝚁 **"
    )
    await update.message.reply_text(welcome_msg, parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    u_data = get_or_create_user(user_id, update.effective_user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("❌ 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.")
        return

    if not is_bot_active() and not is_admin(user_id):
        await update.message.reply_text("🚧 **ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.** ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="Markdown")
        return

    # User Buttons
    if text == "💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴":
        bot_bal = u_data.get("balance", 0.0)
        msg = f"💰 **𝙼𝚈 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${bot_bal:.4f}` USDT"
        
        # কেবল অ্যাডমিন হলে উপলব্ধ কতটি নম্বর আছে তা দেখাবে
        if is_admin(user_id):
            site_bal = get_vak_balance()
            available_numbers = int(site_bal // 0.079)
            msg += f"\n📊 **𝙰𝚅𝙰𝙸𝙻𝙰𝙱𝙻𝙴 𝙽𝚄𝙼𝙱𝙴𝚁𝚂 :** `{available_numbers}` Pcs"
            
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    elif text == "🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁":
        if user_id in active_orders:
            await update.message.reply_text("❌ 𝚈𝙾𝚄 𝙰𝙻𝚁𝙴𝙰𝙳𝚈 𝙷𝙰𝚅𝙴 𝙰𝙽 𝙰𝙲𝚃𝙸𝚅𝙴 𝙽𝚄𝙼𝙱𝙴𝚁. 𝙲𝙰𝙽𝙲𝙴𝙻 𝙾𝚁 𝙵𝙸𝙽𝙸𝚂𝙷 𝙸𝚃 𝙵𝙸𝚁𝚂𝚃.")
            return

        rate = get_rate()
        user_bal = u_data.get("balance", 0.0)

        if user_bal < rate:
            await update.message.reply_text(f"❌ 𝙸𝙽𝚂𝚄𝙵𝙵𝙸𝙲𝙸𝙴𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴!\n⚡ 𝙽𝙴𝙴𝙳: `${rate:.2f}` USDT\n💰 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${user_bal:.2f}` USDT")
            return

        await update.message.reply_text("⏳ ** 𝚆𝙰𝙸𝚃 𝙵𝙾𝚁 𝙽𝚄𝙼𝙱𝙴𝚁. **", parse_mode="Markdown")

        res = buy_vak_number()
        if "error" in res or "tel" not in res or "idNum" not in res:
            await update.message.reply_text("❌ **Stock Out!**", parse_mode="Markdown")
            return

        phone_num = str(res["tel"])
        id_num = str(res["idNum"])

        active_orders[user_id] = {
            "idNum": id_num,
            "phone": phone_num,
            "cost": rate,
            "status": "WAITING_OTP",
            "cancel_task": None,
            "poll_task": None
        }

        # Auto cancel timer after 15 minutes (900s)
        async def auto_cancel():
            await asyncio.sleep(900)
            if user_id in active_orders and active_orders[user_id]["status"] == "WAITING_OTP":
                set_number_status(id_num, "bad")
                del active_orders[user_id]
                try:
                    await context.bot.send_message(user_id, f"⌛ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙻𝙴𝙳 𝙳𝚄𝙴 𝚃𝙾 𝚃𝙸𝙼𝙴𝙾𝚄𝚃 (𝟷𝟻 𝙼𝙸𝙽):** `{phone_num}`", parse_mode="Markdown")
                except Exception:
                    pass

        # OTP Polling Task
        async def poll_otp():
            while user_id in active_orders and active_orders[user_id]["status"] == "WAITING_OTP":
                await asyncio.sleep(1)
                sms_res = fetch_otp_code(id_num)
                
                if isinstance(sms_res, dict) and sms_res.get("smsCode"):
                    otp_code = str(sms_res["smsCode"])
                    order_info = active_orders[user_id]
                    order_info["status"] = "COMPLETED"

                    if order_info["cancel_task"]:
                        order_info["cancel_task"].cancel()

                    users_col.update_one(
                        {"user_id": user_id},
                        {
                            "$inc": {
                                "balance": -order_info["cost"],
                                "otp_count": 1
                            }
                        }
                    )

                    set_number_status(id_num, "end")
                    del active_orders[user_id]

                    otp_msg = (
                        f"✅ **𝙾𝚃𝙿 𝚁𝙴𝙲𝙴𝙸𝚅𝙴𝙳 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝙻𝙻𝚈!**\n\n"
                        f"📱 **𝙽𝚄𝙼𝙱𝙴𝚁:** `{phone_num}`\n"
                        f"💬 **𝙾𝚃𝙿 𝙲𝙾𝙳𝙴:** `{otp_code}`\n\n"
                        f"💰 **𝙳𝙴𝙳𝚄𝙲𝚃𝙴𝙳:** `${rate:.2f}` USDT"
                    )
                    
                    full_sms = sms_res.get("sms", "")
                    if full_sms:
                        inline_kb = InlineKeyboardMarkup([
                            [InlineKeyboardButton("📋 𝙲𝙾𝙿𝚈 𝚂𝙼𝚂", callback_data=f"copy_sms:{user_id}")]
                        ])
                        context.user_data[f"full_sms_{user_id}"] = full_sms
                        await context.bot.send_message(user_id, otp_msg, parse_mode="Markdown", reply_markup=inline_kb)
                    else:
                        await context.bot.send_message(user_id, otp_msg, parse_mode="Markdown")

                    if OTP_GROUP_ID:
                        try:
                            m_num = mask_number(phone_num)
                            group_text = (
                                f"🎉 **𝙽𝙴𝚆 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝙻 𝙾𝚃𝙿!**\n\n"
                                f"👤 **𝚄𝚂𝙴𝚁 ID:** `{user_id}`\n"
                                f"📱 **𝙽𝚄𝙼𝙱𝙴𝚁:** `{m_num}`\n"
                                f"💬 **𝙾𝚃𝙿 𝙲𝙾𝙳𝙴:** `{otp_code}`"
                            )
                            await context.bot.send_message(chat_id=OTP_GROUP_ID, text=group_text, parse_mode="Markdown")
                        except Exception as e:
                            logging.error(f"Failed to send to group: {e}")
                    break

        cancel_task = asyncio.create_task(auto_cancel())
        poll_task = asyncio.create_task(poll_otp())
        
        active_orders[user_id]["cancel_task"] = cancel_task
        active_orders[user_id]["poll_task"] = poll_task

        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 𝙲𝙰𝙽𝙲𝙴𝙻 𝙽𝚄𝙼𝙱𝙴𝚁", callback_data="cancel_number")]
        ])

        buying_msg = (
            f"✅ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙿𝚄𝚁𝙲𝙷𝙰𝚂𝙴𝙳!**\n\n"
            f"📱 **𝙽𝚄𝙼𝙱𝙴𝚁:** `{phone_num}`\n"
            f"💰 **𝙿𝚁𝙸𝙲𝙴:** `${rate:.2f}` USDT\n\n"
            f"⏳ **𝚆𝙰𝙸𝚃𝙸𝙽𝙶 𝙵𝙾𝚁 𝙾𝚃𝙿 (𝙰𝚄𝚃𝙾 𝙿𝙾𝙻𝙻𝙸𝙽𝙶)...**"
        )
        await update.message.reply_text(buying_msg, parse_mode="Markdown", reply_markup=inline_kb)

    elif text == "👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴":
        p_msg = (
            f"👤 **𝚄𝚂𝙴𝚁 𝙿𝚁𝙾𝙵𝙸𝙻𝙴**\n\n"
            f"🆔 **𝚄𝚂𝙴𝚁 ID:** `{user_id}`\n"
            f"📛 **𝙽𝙰𝙼𝙴:** {u_data.get('full_name', 'User')}\n"
            f"💰 **𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${u_data.get('balance', 0.0):.4f}` USDT\n"
            f"📊 **𝚃𝙾𝚃𝙰𝙻 𝙾𝚃𝙿 𝙱𝙾𝚄𝙶𝙷𝚃:** {u_data.get('otp_count', 0)}"
        )
        await update.message.reply_text(p_msg, parse_mode="Markdown")

    elif text == "💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃":
        await update.message.reply_text("✨ 𝙿𝙻𝙴𝙰𝚂𝙴 𝚄𝚂𝙴 `/deposit` 𝙲𝙾𝙼𝙼𝙰𝙽𝙳 𝚃𝙾 𝚂𝚃𝙰𝚁𝚃 𝙳𝙴𝙿𝙾𝚂𝙸𝚃!")

    elif text == "⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻" and is_admin(user_id):
        curr_status = is_bot_active()
        status_text = "🟢 ACTIVE" if curr_status else "🔴 OFF (MAINTENANCE)"

        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 BAN USER", callback_data="admin_ban"), InlineKeyboardButton("✅ UNBAN USER", callback_data="admin_unban")],
            [InlineKeyboardButton("➕ ADD BALANCE", callback_data="admin_add_bal"), InlineKeyboardButton("🧹 ZERO BALANCE", callback_data="admin_zero_bal")],
            [InlineKeyboardButton("⚙️ SET RATE", callback_data="admin_set_rate")],
            [InlineKeyboardButton(f"🤖 BOT STATUS: {status_text}", callback_data="admin_toggle_bot")],
            [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast")]
        ])
        await update.message.reply_text("⚙ **𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝚁𝙾𝙻 𝙿𝙰𝙽𝙴𝙻**", parse_mode="Markdown", reply_markup=admin_kb)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "cancel_number":
        if user_id in active_orders:
            order = active_orders[user_id]
            id_num = order["idNum"]
            phone_num = order["phone"]

            if order["cancel_task"]:
                order["cancel_task"].cancel()
            if order["poll_task"]:
                order["poll_task"].cancel()

            set_number_status(id_num, "bad")
            del active_orders[user_id]

            await query.edit_message_text(f"❌ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙻𝙴𝙳:** `{phone_num}`", parse_mode="Markdown")
        else:
            await query.edit_message_text("⚠️ **𝙽𝙾 𝙰𝙲𝚃𝙸𝚅𝙴 𝙽𝚄𝙼𝙱𝙴𝚁 𝚃𝙾 𝙲𝙰𝙽𝙲𝙴𝙻.**", parse_mode="Markdown")

    elif data.startswith("copy_sms:"):
        target_uid = int(data.split(":")[1])
        sms_text = context.user_data.get(f"full_sms_{target_uid}", "No SMS Text Available")
        await query.message.reply_text(f"📋 **𝙵𝚄𝙻𝙻 𝚂𝙼𝚂:**\n`{sms_text}`", parse_mode="Markdown")

    elif is_admin(user_id):
        if data == "admin_toggle_bot":
            curr = is_bot_active()
            new_status = not curr
            set_bot_active(new_status)
            status_text = "🟢 ACTIVE" if new_status else "🔴 OFF (MAINTENANCE)"
            
            admin_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 BAN USER", callback_data="admin_ban"), InlineKeyboardButton("✅ UNBAN USER", callback_data="admin_unban")],
                [InlineKeyboardButton("➕ ADD BALANCE", callback_data="admin_add_bal"), InlineKeyboardButton("🧹 ZERO BALANCE", callback_data="admin_zero_bal")],
                [InlineKeyboardButton("⚙️ SET RATE", callback_data="admin_set_rate")],
                [InlineKeyboardButton(f"🤖 BOT STATUS: {status_text}", callback_data="admin_toggle_bot")],
                [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast")]
            ])
            await query.edit_message_reply_markup(reply_markup=admin_kb)

        elif data == "admin_ban":
            await query.message.reply_text("SEND USER ID TO BAN:")
            return ADMIN_BAN

        elif data == "admin_unban":
            await query.message.reply_text("SEND USER ID TO UNBAN:")
            return ADMIN_UNBAN

        elif data == "admin_add_bal":
            await query.message.reply_text("SEND USER ID TO ADD BALANCE:")
            return ADMIN_ADD_BAL_USER

        elif data == "admin_zero_bal":
            await query.message.reply_text("SEND USER ID TO ZERO BALANCE:")
            return ADMIN_ZERO_BAL_USER

        elif data == "admin_set_rate":
            curr_rate = get_rate()
            await query.message.reply_text(f"CURRENT CHILE WA RATE IS: `${curr_rate}` USDT\nSEND NEW RATE FOR CHILE WA:")
            return ADMIN_RATE_WA_CL_SET

        elif data == "admin_broadcast":
            await query.message.reply_text("SEND BROADCAST MESSAGE TO ALL USERS:")
            return ADMIN_BROADCAST

# Deposit Conversation Handlers
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u_data = get_or_create_user(user_id, update.effective_user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("❌ 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.")
        return ConversationHandler.END

    dep_msg = (
        f"💵 **𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚅𝙸𝙰 𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈**\n\n"
        f"🆔 **𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈 ID:** `{BINANCE_ID}`\n\n"
        f"✍️ **𝚂𝙴𝙽𝙳 𝚃𝙷𝙴 𝙰𝙼𝙾𝚄𝙽𝚃 (𝚄𝚂𝙳𝚃) 𝚈𝙾𝚄 𝙷𝙰𝚅𝙴 𝚂𝙴𝙽𝚃:**"
    )
    await update.message.reply_text(dep_msg, parse_mode="Markdown")
    return WAITING_AMOUNT

async def deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError()
        context.user_data["dep_amount"] = amount
        await update.message.reply_text("📥 **𝚂𝙴𝙽𝙳 𝚈𝙾𝚄𝚁 𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈 𝚃𝚇𝙸𝙳 / 𝙾𝚁𝙳𝙴𝚁 ID:**", parse_mode="Markdown")
        return WAITING_TXID
    except ValueError:
        await update.message.reply_text("❌ INVALID AMOUNT. PLEASE ENTER A NUMBER (E.G. 5.0):")
        return WAITING_AMOUNT

async def deposit_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text
    context.user_data["dep_txid"] = txid
    await update.message.reply_text("📸 **𝚂𝙴𝙽𝙳 𝙰 𝚂𝙲𝚁𝙴𝙴𝙽𝚂𝙷𝙾𝚃 𝙾𝙵 𝚃𝙷𝙴 𝙿𝙰𝚈𝙼𝙴𝙽𝚃:**", parse_mode="Markdown")
    return WAITING_SCREENSHOT

async def deposit_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    photo = update.message.photo[-1]
    photo_file_id = photo.file_id

    amount = context.user_data.get("dep_amount", 0.0)
    txid = context.user_data.get("dep_txid", "N/A")

    await update.message.reply_text("✅ **𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚁𝙴𝚀𝚄𝙴𝚂𝚃 𝚂𝚄𝙱𝙼𝙸𝚃𝚃𝙴𝙳!**\n𝙰𝚍𝚖𝚒𝚗 𝚠𝚒𝚕𝚕 𝚟𝚎𝚛𝚒𝚏𝚢 𝚊𝚗𝚍 𝚊𝚍𝚍 𝚢𝚘𝚞𝚛 𝚋𝚊𝚕𝚊𝚗𝚌𝚎 𝚜𝚘𝚘𝚗.", parse_mode="Markdown")

    for admin_id in ADMIN_IDS:
        try:
            admin_msg = (
                f"📥 **𝙽𝙴𝚆 𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚁𝙴𝚀𝚄𝙴𝚂𝚃!**\n\n"
                f"👤 **𝚄𝚂𝙴𝚁:** {user.full_name} (`{user_id}`)\n"
                f"💰 **𝙰𝙼𝙾𝚄𝙽𝚃:** `${amount:.2f}` USDT\n"
                f"🆔 **𝚃𝚇𝙸𝙳:** `{txid}`"
            )
            approve_kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ APPROVE", callback_data=f"app_dep:{user_id}:{amount}"),
                    InlineKeyboardButton("❌ REJECT", callback_data=f"rej_dep:{user_id}")
                ]
            ])
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=admin_msg,
                parse_mode="Markdown",
                reply_markup=approve_kb
            )
        except Exception as e:
            logging.error(f"Failed to send deposit to admin {admin_id}: {e}")

    return ConversationHandler.END

# Admin Conversation Handlers
async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"is_banned": True}})
        await update.message.reply_text(f"✅ USER `{target_id}` HAS BEEN BANNED.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ INVALID USER ID.")
    return ConversationHandler.END

async def admin_unban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"✅ USER `{target_id}` HAS BEEN UNBANNED.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ INVALID USER ID.")
    return ConversationHandler.END

async def admin_add_bal_user_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        context.user_data["target_user_id"] = target_id
        await update.message.reply_text("SEND AMOUNT TO ADD:")
        return ADMIN_ADD_BAL_AMT
    except ValueError:
        await update.message.reply_text("❌ INVALID USER ID.")
        return ConversationHandler.END

async def admin_add_bal_amt_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text)
        target_id = context.user_data.get("target_user_id")
        users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amt}})
        await update.message.reply_text(f"✅ ADDED `${amt:.2f}` USDT TO USER `{target_id}`.", parse_mode="Markdown")
        try:
            await context.bot.send_message(target_id, f"🎉 **𝙰𝚙𝚗𝚊𝚛 `${amt:.2f}` USDT 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚜𝚑𝚘𝚏𝚘𝚕𝚋𝚑𝚊𝚋𝚎 𝚓𝚞𝚔𝚝𝚘 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!**", parse_mode="Markdown")
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ INVALID AMOUNT.")
    return ConversationHandler.END

async def admin_zero_bal_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_id = int(update.message.text)
        users_col.update_one({"user_id": target_id}, {"$set": {"balance": 0.0}})
        await update.message.reply_text(f"✅ BALANCE ZEROED FOR USER `{target_id}`.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ INVALID USER ID.")
    return ConversationHandler.END

async def admin_set_rate_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_rate = float(update.message.text)
        set_rate(new_rate)
        await update.message.reply_text(f"✅ CHILE WA RATE UPDATED TO: `${new_rate}` USDT", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ INVALID RATE AMOUNT.")
    return ConversationHandler.END

async def admin_broadcast_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = update.message.text
    users = users_col.find({})
    count = 0
    for u in users:
        try:
            await context.bot.send_message(u["user_id"], msg_text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"📢 BROADCAST SENT TO {count} USERS.")
    return ConversationHandler.END

async def admin_deposit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    admin_id = query.from_user.id

    if not is_admin(admin_id):
        return

    parts = data.split(":")
    action = parts[0]
    target_uid = int(parts[1])

    if action == "app_dep":
        amt = float(parts[2])
        users_col.update_one({"user_id": target_uid}, {"$inc": {"balance": amt}})
        await query.edit_message_caption(caption=f"{query.message.caption}\n\n✅ **APPROVED BY ADMIN**", parse_mode="Markdown")
        try:
            await context.bot.send_message(target_uid, f"🎉 **𝙰𝚙𝚗𝚊𝚛 `${amt:.2f}` USDT 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚜𝚑𝚘𝚏𝚘𝚕𝚋𝚑𝚊𝚋𝚎 𝚓𝚞𝚔𝚝𝚘 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!**", parse_mode="Markdown")
        except Exception:
            pass
    elif action == "rej_dep":
        await query.edit_message_caption(caption=f"{query.message.caption}\n\n❌ **REJECTED BY ADMIN**", parse_mode="Markdown")
        try:
            await context.bot.send_message(target_uid, "❌ **𝙰𝚙𝚗𝚊𝚛 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚛𝚎𝚀𝚞𝚎𝚜𝚝 𝚝𝚒 𝚛𝚎𝚓𝚎𝚌𝚝 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!**", parse_mode="Markdown")
        except Exception:
            pass

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Deposit Conversation Handler
    deposit_handler = ConversationHandler(
        entry_points=[CommandHandler("deposit", deposit_start)],
        states={
            WAITING_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)],
            WAITING_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_txid)],
            WAITING_SCREENSHOT: [MessageHandler(filters.PHOTO, deposit_screenshot)],
        },
        fallbacks=[]
    )

    # Admin Action Conversation Handler
    admin_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(handle_callback_query, pattern="^admin_")
        ],
        states={
            ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_process)],
            ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban_process)],
            ADMIN_ADD_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user_process)],
            ADMIN_ADD_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_amt_process)],
            ADMIN_ZERO_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_zero_bal_process)],
            ADMIN_RATE_WA_CL_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_rate_process)],
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_process)],
        },
        fallbacks=[]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(deposit_handler)
    app.add_handler(admin_handler)
    app.add_handler(CallbackQueryHandler(admin_deposit_callback, pattern="^(app_dep|rej_dep):"))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logging.info("Starting Telegram Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
