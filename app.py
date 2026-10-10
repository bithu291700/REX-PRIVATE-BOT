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

# Support multiple admins (comma-separated string parsed into a list of ints)
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "123456789")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

OTP_GROUP_ID = os.getenv("OTP_GROUP_ID")
BINANCE_ID = os.getenv("BINANCE_ID", "1102671249")
ADMIN_BKASH = "01858582881"
MONGODB_URI = os.getenv("MONGODB_URI")

# MongoDB Setup
if not MONGODB_URI:
    logging.error("❌ MONGODB_URI Environment Variable missing!")
client = MongoClient(MONGODB_URI)
db = client["chile_wa_bot_db"]

users_col = db["users"]
settings_col = db["settings"]
deposits_col = db["pending_deposits"]

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
SUB_DAYS, SUB_TXID, SUB_SCREENSHOT = range(3, 6)
(
    ADMIN_BAN,
    ADMIN_UNBAN,
    ADMIN_ADD_BAL_USER,
    ADMIN_ADD_BAL_AMT,
    ADMIN_ZERO_BAL_USER,
    ADMIN_RATE_WA_CL_SET,
    ADMIN_BROADCAST,
) = range(6, 13)

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
            "subscription_expiry": None,
            "sub_days": 5
        }
        users_col.insert_one(user_data)
        return user_data
    else:
        users_col.update_one({"user_id": user_id}, {"$set": {"full_name": full_name}})
        return user

def is_subscribed(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    user = get_user(user_id)
    if user and user.get("subscription_expiry"):
        expiry = user["subscription_expiry"]
        if datetime.now() < expiry:
            return True
    return False

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
    url = f"https://vak-sms.com/api/setStatus/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}&status={status}"
    try:
        return requests.get(url).json()
    except Exception as e:
        return {"error": str(e)}

def get_vak_balance():
    url = f"https://vak-sms.com/api/getBalance/?apiKey={VAK_SMS_API_KEY}"
    try:
        res = requests.get(url).json()
        return float(res.get("balance", 0.0))
    except Exception:
        return 0.0

def buy_vak_number(max_price: float = 0.087):
    current_panel_bal = get_vak_balance()
    if current_panel_bal < max_price:
        return {"error": "Stock Out!"}

    url = f"https://vak-sms.com/api/getNumber/?apiKey={VAK_SMS_API_KEY}&service=wa&country=cl&maxPrice={max_price}"
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
        await update.message.reply_text("🚧 **ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍ𝙸𝙽.** ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 5 Days (50 Tk)", callback_data="buy_sub_5")],
            [InlineKeyboardButton("💳 7 Days (70 Tk)", callback_data="buy_sub_7")]
        ])
        msg = (
            f"👋 Hello {user.full_name}!\n\n"
            f"❌ **YOU DO NOT HAVE AN ACTIVE SUBSCRIPTION!**\n"
            f"Bot bebohar korte chaile subscription nite hobe.\n\n"
            f"📌 PRICE & VALIDITY:\n"
            f"• 5 Days = 50 Tk\n"
            f"• 7 Days = 70 Tk\n\n"
            f"Nicher button-e click kore subscription kinun:"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 **BUY SUBSCRIPTION:**", parse_mode="Markdown", reply_markup=sub_kb)
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
        await update.message.reply_text("🚧 **ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅ𝙼𝙸𝙽.** ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 5 Days (50 Tk)", callback_data="buy_sub_5")],
            [InlineKeyboardButton("💳 7 Days (70 Tk)", callback_data="buy_sub_7")]
        ])
        await update.message.reply_text("❌ **SUBSCRIPTION EXPIRED! BUY NEW SUBSCRIPTION.**", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("👇 **BUY SUBSCRIPTION:**", parse_mode="Markdown", reply_markup=sub_kb)
        return

    # User Buttons
    if text == "💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴":
        bot_bal = u_data.get("balance", 0.0)
        msg = f"💰 **𝙼𝚈 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${bot_bal:.4f}` USDT"
        
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

        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 𝙲𝙰𝙽𝙲𝙴𝙻 𝙽𝚄𝙼𝙱𝙴𝚁", callback_data="cancel_number")]
        ])

        buying_msg = (
            f"✅ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙿𝚄𝚁𝙲𝙷𝙰𝚂𝙴𝙳!**\n\n"
            f"📱 **𝙽𝚄𝙼𝙱𝙴𝚁:** `{phone_num}`\n"
            f"💰 **𝙿𝚁𝙸𝙲𝙴:** `${rate:.2f}` USDT\n\n"
            f"⏳ **𝚆𝙰𝙸𝚃𝙸𝙽𝙶 𝙵𝙾𝚁 𝙾𝚃𝙿 (𝙰𝚄𝚃𝙾 𝙿𝙾𝙻𝙻𝙸𝙽𝙶)...**"
        )
        sent_msg = await update.message.reply_text(buying_msg, parse_mode="Markdown", reply_markup=inline_kb)

        active_orders[user_id] = {
            "idNum": id_num,
            "phone": phone_num,
            "cost": rate,
            "status": "WAITING_OTP",
            "message_id": sent_msg.message_id,
            "cancel_task": None,
            "poll_task": None
        }

        async def auto_cancel():
            await asyncio.sleep(300)
            if user_id in active_orders and active_orders[user_id]["status"] == "WAITING_OTP":
                set_number_status(id_num, "bad")
                msg_id = active_orders[user_id]["message_id"]
                del active_orders[user_id]
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=msg_id,
                        text=f"⌛ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙻𝙴𝙳 𝙳𝚄𝙴 𝚃𝙾 𝚃𝙸𝙼𝙴𝙾𝚄𝚃 (𝟻 𝙼𝙸𝙽):** `{phone_num}`",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass

        async def poll_otp():
            while user_id in active_orders and active_orders[user_id]["status"] == "WAITING_OTP":
                await asyncio.sleep(1)
                sms_res = fetch_otp_code(id_num)
                
                if isinstance(sms_res, dict) and sms_res.get("smsCode"):
                    otp_code = str(sms_res["smsCode"])
                    order_info = active_orders[user_id]
                    order_info["status"] = "COMPLETED"
                    order_msg_id = order_info["message_id"]

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
                    inline_kb_copy = None
                    if full_sms:
                        inline_kb_copy = InlineKeyboardMarkup([
                            [InlineKeyboardButton("📋 𝙲𝙾𝙿𝚈 𝚂𝙼𝚂", callback_data=f"copy_sms:{user_id}")]
                        ])
                        context.user_data[f"full_sms_{user_id}"] = full_sms

                    try:
                        await context.bot.edit_message_text(
                            chat_id=user_id,
                            message_id=order_msg_id,
                            text=otp_msg,
                            parse_mode="Markdown",
                            reply_markup=inline_kb_copy
                        )
                    except Exception as e:
                        logging.error(f"Failed to edit message: {e}")

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

    elif text == "👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴":
        exp_time = u_data.get("subscription_expiry")
        exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and not is_admin(user_id)) else "Unlimited (Admin)"
        p_msg = (
            f"👤 **𝚄𝚂𝙴𝚁 𝙿𝚁𝙾𝙵𝙸𝙻𝙴**\n\n"
            f"🆔 **𝚄𝚂𝙴𝚁 ID:** `{user_id}`\n"
            f"📛 **𝙽𝙰𝙼𝙴:** {u_data.get('full_name', 'User')}\n"
            f"💵 **𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${u_data.get('balance', 0.0):.4f}` USDT\n"
            f"📊 **𝚃𝙾𝚃𝙰𝙻 𝙾𝚃𝙿 𝙱𝙾𝚄𝙶𝙷𝚃:** {u_data.get('otp_count', 0)}\n"
            f"📅 **𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚅𝙰𝙻𝙸𝙳:** {exp_str}"
        )
        await update.message.reply_text(p_msg, parse_mode="Markdown")

    elif text == "💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃":
        dep_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟡 BINANCE PAY", callback_data="pay_binance")]
        ])
        await update.message.reply_text("💳 **SELECT DEPOSIT METHOD:**", parse_mode="Markdown", reply_markup=dep_kb)

    elif text == "⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻" and is_admin(user_id):
        curr_status = is_bot_active()
        status_text = "🟢 ACTIVE" if curr_status else "🔴 OFF (MAINTENANCE)"

        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚫 BAN USER", callback_data="admin_ban"), InlineKeyboardButton("✅ UNBAN USER", callback_data="admin_unban")],
            [InlineKeyboardButton("➕ ADD BALANCE", callback_data="admin_add_bal"), InlineKeyboardButton("🧹 ZERO BALANCE", callback_data="admin_zero_bal")],
            [InlineKeyboardButton("📊 VIEW USERS", callback_data="admin_view_users"), InlineKeyboardButton("⚙️ SET RATE", callback_data="admin_set_rate")],
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
                [InlineKeyboardButton("📊 VIEW USERS", callback_data="admin_view_users"), InlineKeyboardButton("⚙️ SET RATE", callback_data="admin_set_rate")],
                [InlineKeyboardButton(f"🤖 BOT STATUS: {status_text}", callback_data="admin_toggle_bot")],
                [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast")]
            ])
            await query.edit_message_reply_markup(reply_markup=admin_kb)

        elif data == "admin_view_users":
            all_users = list(users_col.find({}))
            total_users = len(all_users)
            
            if total_users == 0:
                await query.message.reply_text("📋 **NO USERS FOUND.**", parse_mode="Markdown")
                return

            text_msg = f"📊 **TOTAL BOT USERS:** `{total_users}`\n\n"
            for u in all_users:
                text_msg += (
                    f"👤 **Name:** {u.get('full_name', 'N/A')}\n"
                    f"🆔 **ID:** `{u.get('user_id')}`\n"
                    f"💰 **Balance:** `${u.get('balance', 0.0):.4f}` USDT\n"
                    f"📥 **OTP Bought:** {u.get('otp_count', 0)}\n"
                    f"🚫 **Status:** {'Banned' if u.get('is_banned') else 'Active'}\n"
                    f"----------------------------\n"
                )

            if len(text_msg) > 4000:
                for i in range(0, len(text_msg), 4000):
                    await query.message.reply_text(text_msg[i:i+4000], parse_mode="Markdown")
            else:
                await query.message.reply_text(text_msg, parse_mode="Markdown")

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

# Subscription Purchase Flow Handlers
async def sub_start_5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    users_col.update_one({"user_id": user_id}, {"$set": {"sub_days": 5}})
    
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_sub_flow")]])
    msg = (
        f"💰 **PLAN:** 5 Days\n"
        f"💰 **AMOUNT:** 50 Tk\n\n"
        f"👇 **SEND BKASH PERSONAL NUMBER:**\n"
        f"📱 **BKASH NUMBER:** `{ADMIN_BKASH}`\n\n"
        f"Taka dewa sese apnar TrxID-ti likhe pathan:"
    )
    await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_start_7(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    users_col.update_one({"user_id": user_id}, {"$set": {"sub_days": 7}})
    
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_sub_flow")]])
    msg = (
        f"💰 **PLAN:** 7 Days\n"
        f"💰 **AMOUNT:** 70 Tk\n\n"
        f"👇 **SEND BKASH PERSONAL NUMBER:**\n"
        f"📱 **BKASH NUMBER:** `{ADMIN_BKASH}`\n\n"
        f"Taka dewa sese apnar TrxID-ti likhe pathan:"
    )
    await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["sub_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_sub_flow")]])
    await update.message.reply_text("📸 **Ekhon apnar payment-er screenshot (Photo) Pathan:**", parse_mode="Markdown", reply_markup=cancel_kb)
    return SUB_SCREENSHOT

async def sub_screenshot_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    photo = update.message.photo[-1]
    txid = context.user_data.get("sub_txid")
    user_doc = users_col.find_one({"user_id": user.id})
    days = user_doc.get("sub_days", 5) if user_doc else 5
    price_str = f"{50 if days == 5 else 70} Tk"

    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ APPROVED", callback_data=f"approve_sub_{user.id}"),
            InlineKeyboardButton("❌ REJECTED", callback_data=f"reject_sub_{user.id}")
        ]
    ])

    caption = (
        f"🔔 **NEW SUBSCRIPTION REQUEST!**\n\n"
        f"👤 **USER:** {user.full_name} (`{user.id}`)\n"
        f"💳 **METHOD:** BKASH\n"
        f"📅 **PLAN:** {days} Days\n"
        f"💰 **AMOUNT:** {price_str}\n"
        f"🧾 **TrxID:** `{txid}`"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin_id, photo=photo.file_id, caption=caption, parse_mode="Markdown", reply_markup=admin_kb)
        except Exception:
            pass

    await update.message.reply_text("✅ Apnar subscription request admin-er kache pathano hoyeche! Admin approve korlei bot active hoye jaabe.")
    return ConversationHandler.END

async def cancel_sub_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    return ConversationHandler.END

# Deposit Conversation Handlers
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user_id = query.from_user.id
        u_data = get_or_create_user(user_id, query.from_user.full_name)
    else:
        user_id = update.effective_user.id
        u_data = get_or_create_user(user_id, update.effective_user.full_name)

    if u_data.get("is_banned", False):
        if query:
            await query.message.reply_text("❌ 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.")
        else:
            await update.message.reply_text("❌ 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.")
        return ConversationHandler.END

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ CANCEL DEPOSIT", callback_data="cancel_deposit")]
    ])

    dep_msg = (
        f"💵 **𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚅𝙸𝙰 𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈**\n\n"
        f"🆔 **𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈 ID:** `{BINANCE_ID}`\n"
        f"⚠️ **𝙼𝙸𝙽𝙸𝙼𝚄𝙼 𝙳𝙴𝙿𝙾𝚂𝙸𝚃:** `$0.11` USDT\n\n"
        f"✍️ **𝚂𝙴𝙽𝙳 𝚃𝙷𝙴 𝙰𝙼𝙾𝚄𝙽𝚃 (𝚄𝚂𝙳𝚃) 𝚈𝙾𝚄 𝙷𝙰𝚅𝙴 𝚂𝙴𝙽𝚃:**"
    )
    if query:
        await query.message.reply_text(dep_msg, parse_mode="Markdown", reply_markup=cancel_kb)
    else:
        await update.message.reply_text(dep_msg, parse_mode="Markdown", reply_markup=cancel_kb)
        
    return WAITING_AMOUNT

async def deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ CANCEL DEPOSIT", callback_data="cancel_deposit")]
    ])
    try:
        amount = float(text)
        if amount < 0.11:
            await update.message.reply_text("❌ **MINIMUM DEPOSIT IS $0.11 USDT!**\nPlease enter an amount equal to or greater than $0.11:", reply_markup=cancel_kb)
            return WAITING_AMOUNT

        context.user_data["dep_amount"] = amount
        await update.message.reply_text("📥 **𝚂𝙴𝙽𝙳 𝚈𝙾𝚄𝚁 𝙱𝙸𝙽𝙰𝙽𝙲𝙴 𝙿𝙰𝚈 𝚃𝚇𝙸𝙳 / 𝙾𝚁𝙳𝙴𝚁 ID:**", parse_mode="Markdown", reply_markup=cancel_kb)
        return WAITING_TXID
    except ValueError:
        await update.message.reply_text("❌ INVALID AMOUNT. PLEASE ENTER A NUMBER (E.G. 0.50):", reply_markup=cancel_kb)
        return WAITING_AMOUNT

async def deposit_txid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text
    context.user_data["dep_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ CANCEL DEPOSIT", callback_data="cancel_deposit")]
    ])
    await update.message.reply_text("📸 **𝚂𝙴𝙽𝙳 𝙰 𝚂𝙲𝚁𝙴𝙴𝙽𝚂𝙷𝙾𝚃 𝙾𝙵 𝚃𝙷𝙴 𝙿𝙰𝚈𝙼𝙴𝙽𝚃:**", parse_mode="Markdown", reply_markup=cancel_kb)
    return WAITING_SCREENSHOT

async def deposit_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    photo = update.message.photo[-1]
    photo_file_id = photo.file_id

    amount = context.user_data.get("dep_amount", 0.0)
    txid = context.user_data.get("dep_txid", "N/A")

    await update.message.reply_text("✅ **𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝚁𝙴𝚀𝚄𝙴𝚂𝚃 𝚂𝚄𝙱𝙼𝙸𝚃𝚃𝙴𝙳!**\n𝙰𝚍𝚖𝚒𝚗 𝚠𝚒𝚕𝚕 𝚟𝚎𝚛𝚒𝚏𝚢 𝚊𝚗𝚍 𝚊𝚍𝚍 𝚢𝚘𝚞𝚛 𝚋𝚊𝚕𝚊𝚗𝚌𝚎 𝚜𝚘𝚘𝚗.", parse_mode="Markdown")

    dep_id = f"{user_id}_{int(datetime.now().timestamp())}"
    admin_messages = []

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
                    InlineKeyboardButton("✅ APPROVE", callback_data=f"app_dep:{dep_id}:{user_id}:{amount}"),
                    InlineKeyboardButton("❌ REJECT", callback_data=f"rej_dep:{dep_id}:{user_id}")
                ]
            ])
            sent_m = await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo_file_id,
                caption=admin_msg,
                parse_mode="Markdown",
                reply_markup=approve_kb
            )
            admin_messages.append({"chat_id": admin_id, "message_id": sent_m.message_id})
        except Exception as e:
            logging.error(f"Failed to send deposit to admin {admin_id}: {e}")

    deposits_col.insert_one({
        "dep_id": dep_id,
        "user_id": user_id,
        "amount": amount,
        "status": "PENDING",
        "admin_messages": admin_messages
    })

    return ConversationHandler.END

async def deposit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ **𝙳𝙴𝙿𝙾𝚂𝙸𝚃 𝙿𝚁𝙾𝙲𝙴𝚂𝚂 𝙲𝙰𝙽𝙲𝙴𝙻𝙻𝙴𝙳!**", parse_mode="Markdown")
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
            await context.bot.send_message(target_id, f"🎉 **𝙰𝚙𝚗𝚊𝚛 `${amt:.2f}` USDT 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚜𝚑𝚘𝚏𝚘𝚕b𝚑𝚊𝚋𝚎 𝚓𝚞𝚔𝚝𝚘 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!**", parse_mode="Markdown")
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
    dep_id = parts[1]
    target_uid = int(parts[2])

    dep_doc = deposits_col.find_one({"dep_id": dep_id})
    if not dep_doc or dep_doc.get("status") != "PENDING":
        await query.message.reply_text("⚠️ **THIS DEPOSIT REQUEST WAS ALREADY PROCESSED.**", parse_mode="Markdown")
        return

    admin_messages = dep_doc.get("admin_messages", [])

    if action == "app_dep":
        amt = float(parts[3])
        users_col.update_one({"user_id": target_uid}, {"$inc": {"balance": amt}})
        deposits_col.update_one({"dep_id": dep_id}, {"$set": {"status": "APPROVED"}})

        for amsg in admin_messages:
            try:
                await context.bot.edit_message_caption(
                    chat_id=amsg["chat_id"],
                    message_id=amsg["message_id"],
                    caption=f"{query.message.caption}\n\n✅ **APPROVED BY ADMIN**",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Failed to sync approve status to admin {amsg['chat_id']}: {e}")

        try:
            await context.bot.send_message(target_uid, f"🎉 **𝙰𝚙𝚗𝚊𝚛 `${amt:.2f}` USDT 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚜𝚑𝚘𝚏𝚘𝚕𝚋𝚑𝚊𝚋𝚎 𝚓𝚞𝚔𝚝𝚘 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!**", parse_mode="Markdown")
        except Exception:
            pass

    elif action == "rej_dep":
        deposits_col.update_one({"dep_id": dep_id}, {"$set": {"status": "REJECTED"}})

        for amsg in admin_messages:
            try:
                await context.bot.edit_message_caption(
                    chat_id=amsg["chat_id"],
                    message_id=amsg["message_id"],
                    caption=f"{query.message.caption}\n\n❌ **REJECTED BY ADMIN**",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logging.error(f"Failed to sync reject status to admin {amsg['chat_id']}: {e}")

        try:
            await context.bot.send_message(target_uid, "❌ **𝙰𝚙𝚗𝚊𝚛 𝚍𝚎𝚙𝚘𝚜𝚒𝚝 𝚛𝚎𝚀𝚞𝚎𝚜𝚝 𝚝𝚒 𝚛𝚎𝚓𝚎𝚌𝚝 𝚔𝚘𝚛𝚊 𝚑𝚘𝚢𝚎𝚌𝚑𝚎!**", parse_mode="Markdown")
        except Exception:
            pass

# Subscription Admin Approval Callbacks
async def handle_sub_approval_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    admin_id = query.from_user.id

    if not is_admin(admin_id):
        return

    if data.startswith("approve_sub_"):
        target_id = int(data.split("_")[2])
        user_doc = users_col.find_one({"user_id": target_id})
        days = user_doc.get("sub_days", 5) if user_doc else 5
        expiry_date = datetime.now() + timedelta(days=days)
        users_col.update_one({"user_id": target_id}, {"$set": {"subscription_expiry": expiry_date}})
        
        try:
            await query.edit_message_caption(caption=query.message.caption + f"\n\n✅ **Subscription Approved ({days} Days Active)!**", parse_mode="Markdown")
        except Exception:
            pass

        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"🎉 **Apnar Subscription Approved hoyeche!** {days} Diner jonno bot-er sob features active kora hoyeche.",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard(target_id)
            )
        except Exception:
            pass

    elif data.startswith("reject_sub_"):
        target_id = int(data.split("_")[2])
        try:
            await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **Subscription Rejected!**", parse_mode="Markdown")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=target_id, text="❌ **Apnar subscription request-ti batil kora hoyeche.**", parse_mode="Markdown")
        except Exception:
            pass

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Subscription Flow Handler
    sub_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(sub_start_5, pattern="^buy_sub_5$"),
            CallbackQueryHandler(sub_start_7, pattern="^buy_sub_7$")
        ],
        states={
            SUB_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, sub_txid_received)],
            SUB_SCREENSHOT: [MessageHandler(filters.PHOTO, sub_screenshot_received)]
        },
        fallbacks=[CallbackQueryHandler(cancel_sub_flow, pattern="^cancel_sub_flow$")]
    )

    # Deposit Conversation Handler
    deposit_handler = ConversationHandler(
        entry_points=[
            CommandHandler("deposit", deposit_start),
            CallbackQueryHandler(deposit_start, pattern="^pay_binance$")
        ],
        states={
            WAITING_AMOUNT: [
                CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount)
            ],
            WAITING_TXID: [
                CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_txid)
            ],
            WAITING_SCREENSHOT: [
                CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$"),
                MessageHandler(filters.PHOTO, deposit_screenshot)
            ],
        },
        fallbacks=[
            CallbackQueryHandler(deposit_cancel, pattern="^cancel_deposit$")
        ]
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
    app.add_handler(sub_conv)
    app.add_handler(deposit_handler)
    app.add_handler(admin_handler)
    app.add_handler(CallbackQueryHandler(admin_deposit_callback, pattern="^(app_dep|rej_dep):"))
    app.add_handler(CallbackQueryHandler(handle_sub_approval_callbacks, pattern="^(approve_sub_|reject_sub_)"))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    logging.info("Starting Telegram Bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
