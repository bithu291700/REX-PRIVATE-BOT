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
    return "🚀 Rex Private Premium Bot is Active!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# In-Memory Active Orders
active_orders = {}

# Conversation States
WAITING_AMOUNT, WAITING_TXID, WAITING_SCREENSHOT = range(3)
SUB_AMOUNT, SUB_TXID, SUB_SCREENSHOT = range(3, 6)
ADMIN_BAN, ADMIN_UNBAN, ADMIN_ADD_BAL_USER, ADMIN_ADD_BAL_AMT, ADMIN_RATE_SET, ADMIN_BROADCAST = range(6, 12)

# Helper Functions: Formatting & Masking
def mask_number(phone_str: str) -> str:
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

def is_subscribed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    user = get_user(user_id)
    if user and user.get("subscription_expiry"):
        expiry = user["subscription_expiry"]
        if datetime.now() < expiry:
            return True
    return False

# Premium Keyboards
def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton("💳 Account Balance"), KeyboardButton("🛒 Buy Number")],
        [KeyboardButton("🌐 Set Country"), KeyboardButton("📱 Set Service")],
        [KeyboardButton("👤 My Profile"), KeyboardButton("💵 Add Balance")]
    ]
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton("⚙️ Admin Panel")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# VAK-SMS API Functions
def set_number_status(id_num: str, status: str):
    url = f"https://vak-sms.com/api/setStatus/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}&status={status}"
    try:
        return requests.get(url).json()
    except Exception as e:
        return {"error": str(e)}

def buy_vak_number(service: str = "wa", country: str = "hk"):
    url = f"https://vak-sms.com/api/getNumber/?apiKey={VAK_SMS_API_KEY}&service=wa&country=hk&maxPrice=0.07"
    try:
        res = requests.get(url).json()
        if isinstance(res, dict) and res.get("error") == "noNumber":
            return {"error": "Stock Out for current price tier!"}
            
        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            assigned_price = res.get("price")
            if assigned_price is not None:
                try:
                    price_val = float(assigned_price)
                    if price_val > 0.07:
                        id_num = str(res["idNum"])
                        set_number_status(id_num, "bad")
                        return {"error": f"Stock Out! Price (${price_val}) exceeded limit."}
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

# Handlers with Animation Effects
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await update.message.reply_text("❌ **Access Denied:** Apnar account-ti banned kora hoyeche.", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and user_id != ADMIN_ID:
        await update.message.reply_text("🚧 **Maintenance Mode:** Bot ekhon update-er kaaje ache. Doya kore kichu khon por chesta korun.", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        sub_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Activate Subscription (30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        msg = (
            f"✨ **Welcome, {user.full_name}!**\n\n"
            f"🔒 Apnar kache kono active subscription nei.\n"
            f"Bot-er premium features babohar korte subscription active korun.\n\n"
            f"💎 **Package Price:** `30 Tk`\n"
            f"⏳ **Duration:** `3 Days Access`\n\n"
            f"👇 Nicher button-e click kore subscription complete korun:"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("⚡ **Quick Action:**", reply_markup=sub_kb)
        return

    exp_time = u_data.get("subscription_expiry")
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin 👑)"

    # Animated loading simulation
    loading_msg = await update.message.reply_text("⚡ *Initializing secure session...*", parse_mode="Markdown")
    await asyncio.sleep(0.4)
    await loading_msg.edit_text("✨ *Loading dashboard interface...*", parse_mode="Markdown")
    await asyncio.sleep(0.4)

    welcome_msg = (
        f"🌟 **REX PRIVATE CONTROL CENTER** 🌟\n\n"
        f"┏ 👤 **User:** `{user.full_name}`\n"
        f"┣ 🌐 **Active Region:** `Hong Kong (HK)`\n"
        f"┣ 📱 **Active Gateway:** `WhatsApp (WA)`\n"
        f"┣ 💳 **Wallet Balance:** `${u_data.get('balance', 0.0):.4f}` USDT\n"         f"┗ ⏳ **Subscription:** `{exp_str}`\n\n"         f"👇 *Select an option from the menu below to begin:*"     )     await loading_msg.edit_text(welcome_msg, parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))  async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):     user = update.effective_user     user_id = user.id     u_data = get_or_create_user(user_id, user.full_name)      if u_data.get("is_banned", False):         await update.message.reply_text("❌ Apnar account-ti banned kora hoyeche.", reply_markup=ReplyKeyboardRemove())         return      if not is_bot_active() and user_id != ADMIN_ID:         await update.message.reply_text("🚧 **Bot ekhon Maintenance Mode-e ache.**", parse_mode="Markdown")         return      if not is_subscribed(user_id):         sub_kb = InlineKeyboardMarkup([             [InlineKeyboardButton("💎 Activate Subscription (30 Tk / 3 Days)", callback_data="buy_sub_start")]         ])         await update.message.reply_text("❌ Apnar subscription expired! Doya kore subscription renew korun.", reply_markup=sub_kb)         return      text = update.message.text.strip()      if text == "💳 Account Balance":         bot_bal = u_data.get("balance", 0.0)         msg = (             f"💳 **WALLET BALANCE OVERVIEW**\n\n"             f"💎 **Available Balance:** `${bot_bal:.4f}` USDT\n"
        )
        if user_id == ADMIN_ID:
            site_bal = get_vak_balance()
            msg += f"🏦 **VAK-SMS Reserves:** `${site_bal:.4f}` USD"         await update.message.reply_text(msg, parse_mode="Markdown")         return      if text == "👤 My Profile":         bot_bal = u_data.get("balance", 0.0)         otp_cnt = u_data.get("otp_count", 0)         exp_time = u_data.get("subscription_expiry")         exp_str = exp_time.strftime("\%Y-\%m-\%d \%H:\%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin 👑)"                  profile_msg = (             f"👤 **USER PROFILE CREDENTIALS**\n\n"             f"┏ 🆔 **Telegram ID:** `{user_id}`\n"             f"┣ 📛 **Display Name:** `{user.full_name}`\n"             f"┣ 💵 **Current Balance:** `${bot_bal:.4f}` USDT\n"
            f"┣ 📩 **Successful OTPs:** `{otp_cnt}`\n"
            f"┗ ⏳ **Validity Status:** `{exp_str}`"
        )
        await update.message.reply_text(profile_msg, parse_mode="Markdown")
        return

    if text == "🌐 Set Country":
        country_kb = [
            [KeyboardButton("Country: HK (Hong Kong)")],
            [KeyboardButton("🔙 Main Menu")]
        ]
        await update.message.reply_text("🌐 **Select Country Gateway:**", reply_markup=ReplyKeyboardMarkup(country_kb, resize_keyboard=True))
        return

    if text.startswith("Country:"):
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_country": "hk"}})
        await update.message.reply_text("✅ Gateway region updated: `HONG KONG (HK)`", parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))
        return

    if text == "📱 Set Service":
        service_kb = [
            [KeyboardButton("Service: WA (WhatsApp)")],
            [KeyboardButton("🔙 Main Menu")]
        ]
        await update.message.reply_text("📱 **Select Application Service:**", reply_markup=ReplyKeyboardMarkup(service_kb, resize_keyboard=True))
        return

    if text.startswith("Service:"):
        users_col.update_one({"user_id": user_id}, {"$set": {"selected_service": "wa"}})
        await update.message.reply_text("✅ Target service updated: `WHATSAPP (WA)`", parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))
        return

    if text == "🔙 Main Menu":
        await start(update, context)
        return

    if text == "🛒 Buy Number":
        country = "hk"
        service = "wa"
        bot_rate = get_rate(service)
        user_bal = u_data.get("balance", 0.0)

        if user_bal < bot_rate:
            await update.message.reply_text(
                f"❌ **Insufficient Funds!**\n"
                f"• Required: `${bot_rate}` USDT\n"                 f"• Your Balance: `${user_bal:.4f}` USDT\n\n"
                f"💡 Please top up your wallet using 'Add Balance'."
            )
            return

        # Animated Processing Sequence
        status_msg = await update.message.reply_text("⚡ *Connecting to Hong Kong secure node...*")
        await asyncio.sleep(0.4)
        await status_msg.edit_text("🔄 *Allocating WhatsApp verification number...*")
        
        res = buy_vak_number(service, country)

        if isinstance(res, dict) and "tel" in res and "idNum" in res:
            phone_num = str(res["tel"])
            # Ensure number starts with + sign correctly
            if not phone_num.startswith("+"):
                phone_num = f"+{phone_num}"
                
            id_num = str(res["idNum"])

            inline_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Check Live OTP Status", callback_data=f"check_otp_{id_num}")],
                [InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_num_{id_num}")]
            ])

            # Premium Number Display with Code Block for Easy One-Tap Copy with '+' Sign
            sent_msg = await status_msg.edit_text(
                f"🎉 **NUMBER ACQUIRED SUCCESSFULLY!**\n\n"
                f"📱 **Mobile Number:** `{phone_num}` *(Tap code to copy)*\n"
                f"🆔 **Order Reference:** `{id_num}`\n"
                f"🌍 **Location:** `Hong Kong (HK)`\n"
                f"💬 **Platform:** `WhatsApp`\n"
                f"💵 **Service Rate:** `${bot_rate}` USDT *(Deducted only after OTP arrival)*\n\n"
                f"⏳ *Listening for incoming verification codes in real-time...*",
                parse_mode="Markdown",
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

            asyncio.create_task(auto_check_otp(context, user_id, id_num, phone_num, sent_msg.message_id))
        else:
            err_msg = res.get("error", "Stock Out") if isinstance(res, dict) else "Gateway Error"
            await status_msg.edit_text(f"❌ **Acquisition Failed:** `{err_msg}`", parse_mode="Markdown")
        return

    if text == "⚙️ Admin Panel" and user_id == ADMIN_ID:
        await send_admin_panel(update, context)
        return

async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_str = "🟢 Active (ONLINE)" if is_bot_active() else "🔴 Maintenance (OFFLINE)"
    admin_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Active Subscribed Users", callback_data="admin_view_users")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_start"), InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_start")],
        [InlineKeyboardButton("💵 Update Rate", callback_data="admin_rate_start"), InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_bal_start")],
        [InlineKeyboardButton("📢 Broadcast Notice", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(f"Bot Status: {status_str}", callback_data="admin_toggle_bot")]
    ])
    if update.message:
        await update.message.reply_text("🛠 **ADMIN CONTROL DASHBOARD:**", reply_markup=admin_kb, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.message.reply_text("🛠 **ADMIN CONTROL DASHBOARD:**", reply_markup=admin_kb, parse_mode="Markdown")

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "admin_view_users" and user_id == ADMIN_ID:
        try:
            now = datetime.now()
            subscribed_users = list(users_col.find({"subscription_expiry": {"$gt": now}}))
            if not subscribed_users:
                await query.message.reply_text("📋 No active subscribers currently registered.")
                return
            
            msg = f"👥 **ACTIVE SUBSCRIBERS LIST ({len(subscribed_users)}):**\n\n"
            for u in subscribed_users:
                uid = u.get("user_id", "N/A")
                raw_name = str(u.get("full_name", "User"))
                safe_name = raw_name.replace("*", "").replace("_", "").replace("`", "")
                bal = u.get("balance", 0.0)
                otp_cnt = u.get("otp_count", 0)
                msg += f"• **{safe_name}** (`{uid}`)\n  └ 💰 Bal: `${bal:.4f}` | 📩 OTP: `{otp_cnt}`\n\n"
            await query.message.reply_text(msg, parse_mode="Markdown")
        except Exception as e:
            await query.message.reply_text(f"❌ Error: {str(e)}")

    elif data == "admin_toggle_bot" and user_id == ADMIN_ID:
        current_status = is_bot_active()
        new_status = not current_status
        set_bot_active(new_status)
        status_text = "🟢 **Bot operational status switched to ONLINE!**" if new_status else "🔴 **Bot switched to MAINTENANCE MODE!**"
        
        status_str = "🟢 Active (ONLINE)" if new_status else "🔴 Maintenance (OFFLINE)"
        admin_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("👥 Active Subscribed Users", callback_data="admin_view_users")],
            [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban_start"), InlineKeyboardButton("✅ Unban User", callback_data="admin_unban_start")],
            [InlineKeyboardButton("💵 Update Rate", callback_data="admin_rate_start"), InlineKeyboardButton("➕ Add Balance", callback_data="admin_add_bal_start")],
            [InlineKeyboardButton("📢 Broadcast Notice", callback_data="admin_broadcast_start")],
            [InlineKeyboardButton(f"Bot Status: {status_str}", callback_data="admin_toggle_bot")]
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
            await query.message.reply_text("⏳ No verification code received yet. Please check again in a few seconds.")

    elif data.startswith("cancel_num_"):
        id_num = data.split("_")[2]
        if id_num in active_orders:
            set_number_status(id_num, "bad")
            active_orders.pop(id_num, None)
            await query.edit_message_text(
                f"{query.message.text}\n\n❌ **Order cancelled successfully (No balance charged).**",
                parse_mode="Markdown"
            )
        else:
            await query.message.reply_text("❌ Order is no longer active or code was already received.")

    elif data.startswith("approve_dep_"):
        parts = data.split("_")
        target_id = int(parts[2])
        amount = float(parts[3])
        users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amount}})
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **Approved & Balance Credited!**")
        await context.bot.send_message(chat_id=target_id, text=f"🎉 **Your deposit of `${amount}` USDT has been successfully credited!**")

    elif data.startswith("reject_dep_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **Deposit Request Rejected.**")
        await context.bot.send_message(chat_id=target_id, text="❌ Your deposit request was declined by admin.")

    elif data.startswith("approve_sub_"):
        target_id = int(data.split("_")[2])
        expiry_date = datetime.now() + timedelta(days=3)
        users_col.update_one({"user_id": target_id}, {"$set": {"subscription_expiry": expiry_date}})
        await query.edit_message_caption(caption=query.message.caption + "\n\n✅ **Subscription Activated (3 Days)!**")
        await context.bot.send_message(
            chat_id=target_id,
            text="🎉 **Subscription Activated Successfully!** All bot features are now unlocked for 3 days.",
            reply_markup=get_main_keyboard(target_id)
        )

    elif data.startswith("reject_sub_"):
        target_id = int(data.split("_")[2])
        await query.edit_message_caption(caption=query.message.caption + "\n\n❌ **Subscription Request Rejected.**")
        await context.bot.send_message(chat_id=target_id, text="❌ Your subscription request was declined.")

async def process_otp_success(context, id_num: str, otp: str):
    if id_num not in active_orders:
        return

    order_info = active_orders.pop(id_num)
    uid = order_info["user_id"]
    cost = order_info["cost"]
    phone = order_info["phone"]
    msg_id = order_info["msg_id"]

    users_col.update_one(
        {"user_id": uid},
        {"$inc": {"balance": -cost, "otp_count": 1}}
    )
    
    updated_user = get_user(uid)
    rem_bal = updated_user.get("balance", 0.0) if updated_user else 0.0
    set_number_status(id_num, "end")

    success_text = (
        f"✅ **VERIFICATION CODE RECEIVED!**\n\n"
        f"📱 **Number:** `{phone}`\n"
        f"🔑 **OTP Code:** `{otp}` *(Tap code to copy)*\n\n"
        f"💵 **Cost Deducted:** `${cost}` USDT\n"
        f"💰 **Remaining Wallet:** `${rem_bal:.4f}` USDT"
    )

    try:
        await context.bot.edit_message_text(
            chat_id=uid,
            message_id=msg_id,
            text=success_text,
            parse_mode="Markdown"
        )
    except Exception:
        await context.bot.send_message(chat_id=uid, text=success_text, parse_mode="Markdown")

    masked_phone = mask_number(phone)
    group_forward_msg = (
        f"🇭🇰 **Number:** `{masked_phone}`\n"
        f"🔑 **OTP Code:** `{otp}`\n"
        f"💬 **Info:** `WhatsApp Verification Code: {otp}`"
    )

    if OTP_GROUP_ID:
        try:
            await context.bot.send_message(chat_id=OTP_GROUP_ID, text=group_forward_msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Failed forwarding OTP to group: {e}")

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

# Subscription Conversation Flow
async def sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bkash_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌸 bKash Personal", callback_data="pay_bkash_sub")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]
    ])
    await query.message.reply_text("💳 **Select Payment Channel:**", reply_markup=bkash_kb)
    return SUB_AMOUNT

async def sub_bkash_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("📥 **Enter Subscription Fee Amount (`30` Tk):**", reply_markup=cancel_kb)
    return SUB_AMOUNT

async def sub_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text != "30":
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text("❌ Subscription fee is exactly **30 Tk**. Please type `30`:", reply_markup=cancel_kb)
        return SUB_AMOUNT

    msg = (
        f"💎 **Subscription Package:** `3 Days Access`\n"
        f"💰 **Total Payable:** `30 Tk`\n\n"
        f"👇 **Send Money to bKash Personal Number:**\n"
        f"📱 Number: `{ADMIN_BKASH}`\n\n"
        f"After payment, reply with your **TrxID**:"
    )
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_kb)
    return SUB_TXID

async def sub_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["sub_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("📸 **Now upload your bKash Payment Screenshot:**", reply_markup=cancel_kb)
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
        f"🔔 **NEW SUBSCRIPTION REQUEST**\n\n"
        f"👤 **User:** `{user.full_name}` (`{user.id}`)\n"
        f"💰 **Amount:** `30 Tk`\n"
        f"🧾 **TrxID:** `{txid}`"
    )

    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="Markdown", reply_markup=admin_kb)
    await update.message.reply_text("✅ **Subscription request submitted to admin!** Verification will complete shortly.")
    return ConversationHandler.END

# Deposit Conversation Flow
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💛 Binance Pay", callback_data="pay_binance")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]
    ])
    await update.message.reply_text("💳 **Select Deposit Method:**", reply_markup=payment_kb)
    return WAITING_AMOUNT

async def deposit_binance_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.reply_text("📥 **Enter USDT amount to deposit (Minimum: `1` USDT):**", reply_markup=cancel_kb)
    return WAITING_AMOUNT

async def deposit_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount < 1.0:
            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
            await update.message.reply_text("❌ Minimum deposit amount is **1 USDT**. Please enter a valid amount:", reply_markup=cancel_kb)
            return WAITING_AMOUNT

        context.user_data["dep_amount"] = amount
        msg = (
            f"💰 **Deposit Amount:** `{amount}` USDT\n\n"
            f"👇 **Transfer via Binance Pay ID:**\n"
            f"🆔 **Pay ID:** `{BINANCE_ID}`\n\n"
            f"After transferring, send your **Order ID / TxID**:"
        )
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=cancel_kb)
        return WAITING_TXID
    except ValueError:
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
        await update.message.reply_text("❌ Please enter a valid number (e.g. `1` or `5`).", reply_markup=cancel_kb)
        return WAITING_AMOUNT

async def deposit_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txid = update.message.text.strip()
    context.user_data["dep_txid"] = txid
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]])
    await update.message.reply_text("📸 **Now upload your Binance payment screenshot:**", reply_markup=cancel_kb)
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
        f"📥 **NEW DEPOSIT REQUEST**\n\n"
        f"👤 **User:** `{user.full_name}` (`{user.id}`)\n"
        f"💰 **Amount:** `${amount}` USDT\n"
        f"🧾 **TxID:** `{txid}`"
    )

    await context.bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="Markdown", reply_markup=admin_kb)
    await update.message.reply_text("✅ **Deposit request submitted to admin!** Balance will be updated soon.")
    return ConversationHandler.END

async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.message.edit_text("❌ Process cancelled.")
    elif update.message:
        await update.message.reply_text("❌ Process cancelled.")
    return ConversationHandler.END

# Admin Conversation Handlers
async def admin_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("🚫 **Enter User ID to Ban:**")
    return ADMIN_BAN

async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
        await update.message.reply_text(f"✅ User `{uid}` has been banned.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")
    return ConversationHandler.END

async def admin_unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("✅ **Enter User ID to Unban:**")
    return ADMIN_UNBAN

async def admin_unban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
        await update.message.reply_text(f"✅ User `{uid}` has been unbanned.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID.")
    return ConversationHandler.END

async def admin_add_bal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("➕ **Enter User ID for Balance Addition:**")
    return ADMIN_ADD_BAL_USER

async def admin_add_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
        context.user_data["target_add_uid"] = uid
        await update.message.reply_text(f"💰 **Enter USDT amount to add for user `{uid}`:**", parse_mode="Markdown")
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
        
        await update.message.reply_text(f"✅ Added `${amt}` USDT to User `{uid}`. New Balance: `${new_bal:.4f}` USDT", parse_mode="Markdown")
        await context.bot.send_message(chat_id=uid, text=f"🎉 **Admin added `${amt}` USDT to your wallet balance!**")
    except ValueError:
        await update.message.reply_text("❌ Invalid Amount.")
    return ConversationHandler.END

async def admin_rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("💵 **Enter new WhatsApp Bot Rate in USDT (e.g. `0.075`):**")
    return ADMIN_RATE_SET

async def admin_rate_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip())
        set_rate("wa", rate)
        await update.message.reply_text(f"✅ WhatsApp Bot Rate updated to: `${rate}` USDT", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid Format! Please enter a valid number.")
    return ConversationHandler.END

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("📢 **Enter Broadcast Message Content:**")
    return ADMIN_BROADCAST

async def admin_broadcast_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_text = update.message.text.strip()
    users = list(users_col.find())
    count = 0
    for u in users:
        uid = u.get("user_id")
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 **ANNOUNCEMENT:**\n\n{msg_text}", parse_mode="Markdown")
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await update.message.reply_text(f"✅ Broadcast successfully sent to `{count}` users!", parse_Mode="Markdown")
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
        fallbacks=[CommandHandler("cancel", cancel_flow), CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    dep_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💵 Add Balance$"), deposit_start)],
        states={
            WAITING_AMOUNT: [
                CallbackQueryHandler(deposit_binance_selected, pattern="^pay_binance$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_received)
            ],
            WAITING_TXID: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_txid_received)],
            WAITING_SCREENSHOT: [MessageHandler(filters.PHOTO, deposit_screenshot_received)]
        },
        fallbacks=[CommandHandler("cancel", cancel_flow), CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    admin_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_ban_start, pattern="^admin_ban_start$"),
            CallbackQueryHandler(admin_unban_start, pattern="^admin_unban_start$"),
            CallbackQueryHandler(admin_add_bal_start, pattern="^admin_add_bal_start$"),
            CallbackQueryHandler(admin_rate_start, pattern="^admin_rate_start$"),
            CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast_start$"),
        ],
        states={
            ADMIN_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_process)],
            ADMIN_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unban_process)],
            ADMIN_ADD__BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user)], # Wait fixed below if needed
            ADMIN_ADD_BAL_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user)],
            ADMIN_ADD_BAL_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_amt)],
            ADMIN_RATE_SET: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_process)],
            ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_process)],
        },
        fallbacks=[CommandHandler("cancel", cancel_php := "cancel", fallback_method := cancel_flow), CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$")]
    )

    # Re-writing clean handlers mapping
    app.add_handler(CommandHandler("start", start))
    app.add_handler(sub_handler)
    app.add_handler(dep_handler)
    app.add_handler(admin_handler)
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))

    print("🚀 Rex Private Premium Bot Running...")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
