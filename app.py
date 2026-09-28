import asyncio
from datetime import datetime, timedelta
import logging
import os
import re
import threading
from flask import Flask
import requests
from pymongo import MongoClient
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

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
(
    ADMIN_BAN,
    ADMIN_UNBAN,
    ADMIN_ADD_BAL_USER,
    ADMIN_ADD_BAL_AMT,
    ADMIN_RATE_SET,
    ADMIN_BROADCAST,
) = range(6, 12)


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
        "subscription_expiry": None,
    }
    users_col.insert_one(user_data)
    return user_data
  else:
    users_col.update_one(
        {"user_id": user_id}, {"$set": {"full_name": full_name}}
    )
    return user


def get_rate(service_code: str = "wa"):
  doc = settings_col.find_one({"type": "rates"})
  if doc and service_code in doc.get("rates", {}):
    return float(doc["rates"][service_code])
  return 0.10


def set_rate(service_code: str, rate: float):
  settings_col.update_one(
      {"type": "rates"}, {"$set": {f"rates.{service_code}": rate}}, upsert=True
  )


def is_bot_active() -> bool:
  doc = settings_col.find_one({"type": "bot_status"})
  if doc:
    return doc.get("is_active", True)
  return True


def set_bot_active(status: bool):
  settings_col.update_one(
      {"type": "bot_status"}, {"$set": {"is_active": status}}, upsert=True
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
      [KeyboardButton("💳 Account Balance"), KeyboardButton("🛒 Buy Number")],
      [KeyboardButton("🌐 Set Country"), KeyboardButton("📱 Set Service")],
      [KeyboardButton("👤 Profile"), KeyboardButton("💵 Deposit")],
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
      return {"error": "Stock Out for $0.075 Price Tier!"}

    if isinstance(res, dict) and "tel" in res and "idNum" in res:
      assigned_price = res.get("price")
      if assigned_price is not None:
        try:
          price_val = float(assigned_price)
          if price_val > 0.07:
            id_num = str(res["idNum"])
            set_number_status(id_num, "bad")
            return {
                "error": (
                    f"Stock Out! Price (${price_val}) exceeded $0.075 limit."
                )
            }
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


# Handlers with Ultra-Premium Animations
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  user = update.effective_user
  user_id = user.id

  u_data = get_or_create_user(user_id, user.full_name)

  if u_data.get("is_banned", False):
    await update.message.reply_text(
        "❌ Apnar account-ti banned kora hoyeche.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return

  if not is_bot_active() and user_id != ADMIN_ID:
    await update.message.reply_text(
        "🚧 **Bot ekhon Maintenance Mode-e ache.** Doya kore kichu khon por"
        " chesta korun.",
        parse_mode="Markdown",
    )
    return

  # Smooth Loading Animation for Start
  msg = await update.message.reply_text(
      "⚡ **Initializing Secure Gateway...** `[░░░░░░░░░░]` 0%"
  )
  await asyncio.sleep(0.3)
  await msg.edit_text(
      "⚡ **Initializing Secure Gateway...** `[████░░░░░░]` 40%"
  )
  await asyncio.sleep(0.3)
  await msg.edit_text(
      "⚡ **Initializing Secure Gateway...** `[██████████]` 100%\n\n✨ *System"
      " Ready.*"
  )
  await asyncio.sleep(0.4)
  try:
    await msg.delete()
  except Exception:
    pass

  if not is_subscribed(user_id):
    sub_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "💳 Buy Subscription (30 Tk / 3 Days)", callback_data="buy_sub_start"
        )
    ]])
    msg_sub = (
        f"👋 **Hello {user.full_name}!**\n\n"
        f"❌ Apnar kache kono active subscription nei!\n"
        f"Bot babohar korte apnake subscription kinte hobe.\n\n"
        f"📌 **Price:** `30 Tk`\n"
        f"⏳ **Validity:** `3 Days`\n\n"
        f"Nicher button-e click kore subscription kinun:"
    )
    await update.message.reply_text(
        msg_sub, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )
    await update.message.reply_text(
        "👇 **Buy Subscription:**", reply_markup=sub_kb
    )
    return

  exp_time = u_data.get("subscription_expiry")
  exp_str = (
      exp_time.strftime("%Y-%m-%d %H:%M")
      if (exp_time and user_id != ADMIN_ID)
      else "Unlimited (Admin)"
  )

  welcome_msg = (
      "💎 **WELCOME TO REX PRIVATE GATEWAY** 💎\n"
      "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
      f"👋 **Hello, {user.full_name}!**\n\n"
      "⚙️ **Active Profile Setup:**\n"
      "• Country: `HONG KONG (HK)`\n"
      "• Service: `WHATSAPP (WA)`\n"
      f"• Bot Balance: `${u_data.get('balance', 0.0):.4f} USDT`\n"
      f"• Subscription Valid Till: `{exp_str}`\n\n"
      "👇 *Select an option from the menu below:*"
  )
  await update.message.reply_text(
      welcome_msg, parse_mode="Markdown", reply_markup=get_main_keyboard(user_id)
  )


async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
  user = update.effective_user
  user_id = user.id

  u_data = get_or_create_user(user_id, user.full_name)

  if u_data.get("is_banned", False):
    await update.message.reply_text(
        "❌ Apnar account-ti banned kora hoyeche.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return

  if not is_bot_active() and user_id != ADMIN_ID:
    await update.message.reply_text(
        "🚧 **Bot ekhon Maintenance Mode-e ache.** Doya kore kichu khon por"
        " chesta korun.",
        parse_mode="Markdown",
    )
    return

  if not is_subscribed(user_id):
    sub_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "💳 Buy Subscription (30 Tk / 3 Days)", callback_data="buy_sub_start"
        )
    ]])
    await update.message.reply_text(
        "❌ Apnar subscription expired! Doya kore subscription kinun.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        "👇 **Buy Subscription:**", reply_markup=sub_kb
    )
    return

  text = update.message.text.strip()

  if text == "💳 Account Balance":
    bot_bal = u_data.get("balance", 0.0)
    msg = f"💰 **Apnar Bot Balance:** `${bot_bal:.4f}` USDT"
    if user_id == ADMIN_ID:
      site_bal = get_vak_balance()
      msg += f"\n🏦 **VAK-SMS Site Balance:** `${site_bal:.4f}` USD"
    await update.message.reply_text(msg, parse_mode="Markdown")
    return

  if text == "👤 Profile":
    bot_bal = u_data.get("balance", 0.0)
    otp_cnt = u_data.get("otp_count", 0)
    exp_time = u_data.get("subscription_expiry")
    exp_str = (
        exp_time.strftime("%Y-%m-%d %H:%M")
        if (exp_time and user_id != ADMIN_ID)
        else "Unlimited (Admin)"
    )
    profile_msg = (
        "👤 **Elite User Profile:**\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 **User ID:** `{user_id}`\n"
        f"📛 **Name:** {user.full_name}\n"
        f"💵 **Balance:** `${bot_bal:.4f}` USDT\n"
        f"📩 **Total OTP Received:** `{otp_cnt}`\n"
        f"📅 **Subscription Valid:** `{exp_str}`"
    )
    await update.message.reply_text(profile_msg, parse_mode="Markdown")
    return

  if text == "🌐 Set Country":
    country_kb = [
        [KeyboardButton("Country: HK (Hong Kong)")],
        [KeyboardButton("🔙 Main Menu")],
    ]
    await update.message.reply_text(
        "🌐 **Bortomane shudhu Hong Kong selected ache:**",
        reply_markup=ReplyKeyboardMarkup(country_kb, resize_keyboard=True),
    )
    return

  if text.startswith("Country:"):
    users_col.update_one(
        {"user_id": user_id}, {"$set": {"selected_country": "hk"}}
    )
    await update.message.reply_text(
        "✅ Country set: `HONG KONG (HK)`",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(user_id),
    )
    return

  if text == "📱 Set Service":
    service_kb = [
        [KeyboardButton("Service: WA (WhatsApp)")],
        [KeyboardButton("🔙 Main Menu")],
    ]
    await update.message.reply_text(
        "📱 **Bortomane shudhu WhatsApp selected ache:**",
        reply_markup=ReplyKeyboardMarkup(service_kb, resize_keyboard=True),
    )
    return

  if text.startswith("Service:"):
    users_col.update_one(
        {"user_id": user_id}, {"$set": {"selected_service": "wa"}}
    )
    await update.message.reply_text(
        "✅ Service set: `WHATSAPP (WA)`",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(user_id),
    )
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
          f"❌ Porjapto balance nei! Proyojon: `${bot_rate}` USDT, Apnar ache:"
          f" `${user_bal:.4f}` USDT.\nDoya kore Deposit korun."
      )
      return

    # Animated Processing Status
    status_msg = await update.message.reply_text(
        "⚡ `[Connecting to Gateway...]` 🔄"
    )
    await asyncio.sleep(0.4)
    await status_msg.edit_text(
        "⚡ `[Allocating Number from Hong Kong...]` 📶"
    )

    res = buy_vak_number(service, country)

    if isinstance(res, dict) and "tel" in res and "idNum" in res:
      raw_phone_num = str(res["tel"])
      # Ensure Plus (+) sign format so it's copyable easily
      phone_num = (
          f"+{raw_phone_num}"
          if not raw_phone_num.startswith("+")
          else raw_phone_num
      )
      id_num = str(res["idNum"])

      inline_kb = InlineKeyboardMarkup([
          [
              InlineKeyboardButton(
                  "📩 Check Active OTP", callback_data=f"check_otp_{id_num}"
              )
          ],
          [
              InlineKeyboardButton(
                  "❌ Cancel Number", callback_data=f"cancel_num_{id_num}"
              )
          ],
      ])

      sent_msg = await update.message.reply_text(
          "💎 **NUMBER ACQUIRED SUCCESSFULLY** 💎\n"
          "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
          f"📱 **Number:** `{phone_num}`\n"
          f"🆔 **ID Num:** `{id_num}`\n"
          f"🌍 **Country:** `HK`\n"
          f"💬 **Service:** `WA`\n"
          f"💵 **Rate:** `${bot_rate}` USDT *(OTP ashlei balance katbe)*\n\n"
          "💡 *(Tap the number above to copy with the plus sign!)*\n"
          "⏳ *Waiting for incoming OTP...*",
          parse_mode="Markdown",
          reply_markup=inline_kb,
      )

      active_orders[id_num] = {
          "user_id": user_id,
          "service": service,
          "country": country,
          "cost": bot_rate,
          "phone": phone_num,
          "msg_id": sent_msg.message_id,
      }

      try:
        await status_msg.delete()
      except Exception:
        pass

      asyncio.create_task(
          auto_check_otp(
              context, user_id, id_num, str(phone_num), sent_msg.message_id
          )
      )
    else:
      err_msg = (
          res.get("error", "Stock Out") if isinstance(res, dict) else "Error"
      )
      await update.message.reply_text(
          f"❌ **Number kena shombhov hoyni:** `{err_msg}`"
      )
    return

  if text == "⚙️ Admin Panel" and user_id == ADMIN_ID:
    await send_admin_panel(update, context)
    return


async def send_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
  status_str = "🟢 ON (Active)" if is_bot_active() else "🔴 OFF (Maintenance)"
  admin_kb = InlineKeyboardMarkup([
      [InlineKeyboardButton("👥 View All Users", callback_data="admin_view_users")],
      [
          InlineKeyboardButton(
              "🚫 Ban User", callback_data="admin_ban_start"
          ),
          InlineKeyboardButton(
              "✅ Unban User", callback_data="admin_unban_start"
          ),
      ],
      [
          InlineKeyboardButton(
              "💵 Set WA Rate", callback_data="admin_rate_start"
          ),
          InlineKeyboardButton(
              "➕ Add Balance", callback_data="admin_add_bal_start"
          ),
      ],
      [
          InlineKeyboardButton(
              "📢 Broadcast Message", callback_data="admin_broadcast_start"
          )
      ],
      [
          InlineKeyboardButton(
              f"Bot Status: {status_str}", callback_data="admin_toggle_bot"
          )
      ],
  ])
  if update.message:
    await update.message.reply_text(
        "🛠 **Admin Control Panel:**",
        reply_markup=admin_kb,
        parse_mode="Markdown",
    )
  elif update.callback_query:
    await update.callback_query.message.reply_text(
        "🛠 **Admin Control Panel:**",
        reply_markup=admin_kb,
        parse_mode="Markdown",
    )


# Callback Handler for General & Admin View/Toggle Actions
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  await query.answer()
  data = query.data
  user_id = query.from_user.id

  if data == "admin_view_users" and user_id == ADMIN_ID:
    try:
      now = datetime.now()
      subscribed_users = list(
          users_col.find({"subscription_expiry": {"$gt": now}})
      )

      if not subscribed_users:
        await query.message.reply_text(
            "📋 Currently, there are no active subscribed users."
        )
        return

      msg = f"👥 **Active Subscribed Users ({len(subscribed_users)}):**\n\n"
      for u in subscribed_users:
        uid = u.get("user_id", "N/A")
        raw_name = str(u.get("full_name", "User"))
        safe_name = (
            raw_name.replace("*", "")
            .replace("_", "")
            .replace("`", "")
            .replace("[", "")
            .replace("]", "")
        )
        bal = u.get("balance", 0.0)
        otp_cnt = u.get("otp_count", 0)
        msg += (
            f"• **{safe_name}** (`{uid}`)\n  └ 💰 Balance: `${bal:.4f}` USDT |"
            f" 📩 OTP Rcv: `{otp_cnt}`\n\n"
        )

      await query.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
      await query.message.reply_text(f"❌ Error loading users: {str(e)}")

  elif data == "admin_toggle_bot" and user_id == ADMIN_ID:
    current_status = is_bot_active()
    new_status = not current_status
    set_bot_active(new_status)
    status_text = (
        "🟢 **Bot ON (Active) kora hoyeche!**"
        if new_status
        else "🔴 **Bot OFF (Maintenance Mode) kora hoyeche!**"
    )

    status_str = "🟢 ON (Active)" if new_status else "🔴 OFF (Maintenance)"
    admin_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 View All Users", callback_data="admin_view_users"
            )
        ],
        [
            InlineKeyboardButton(
                "🚫 Ban User", callback_data="admin_ban_start"
            ),
            InlineKeyboardButton(
                "✅ Unban User", callback_data="admin_unban_start"
            ),
        ],
        [
            InlineKeyboardButton(
                "💵 Set WA Rate", callback_data="admin_rate_start"
            ),
            InlineKeyboardButton(
                "➕ Add Balance", callback_data="admin_add_bal_start"
            ),
        ],
        [
            InlineKeyboardButton(
                "📢 Broadcast Message", callback_data="admin_broadcast_start"
            )
        ],
        [
            InlineKeyboardButton(
                f"Bot Status: {status_str}", callback_data="admin_toggle_bot"
            )
        ],
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
      await query.message.reply_text(
          "⏳ Ekhono OTP asheni, ektu por abar chesta korun."
      )

  elif data.startswith("cancel_num_"):
    id_num = data.split("_")[2]
    if id_num in active_orders:
      set_number_status(id_num, "bad")
      active_orders.pop(id_num, None)
      await query.edit_message_text(
          f"{query.message.text}\n\n❌ **Number-ti cancel kora hoyeche (Kono"
          " balance katini).**",
          parse_mode="Markdown",
      )
    else:
      await query.message.reply_text(
          "❌ Ei order-ti ar active nei ba already OTP ashe geche."
      )

  elif data.startswith("approve_dep_"):
    parts = data.split("_")
    target_id = int(parts[2])
    amount = float(parts[3])
    users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amount}})
    await query.edit_message_caption(
        caption=query.message.caption + "\n\n✅ **Approved & Balance Added!**"
    )
    await context.bot.send_message(
        chat_id=target_id,
        text=f"🎉 **Apnar `${amount}` USDT deposit shofolbhabe jukto kora hoyeche!**",
    )

  elif data.startswith("reject_dep_"):
    target_id = int(data.split("_")[2])
    await query.edit_message_caption(
        caption=query.message.caption + "\n\n❌ **Deposit Rejected!**"
    )
    await context.bot.send_message(
        chat_id=target_id, text="❌ Apnar deposit request-ti batil kora hoyeche."
    )

  elif data.startswith("approve_sub_"):
    target_id = int(data.split("_")[2])
    expiry_date = datetime.now() + timedelta(days=3)
    users_col.update_one(
        {"user_id": target_id}, {"$set": {"subscription_expiry": expiry_date}}
    )
    await query.edit_message_caption(
        caption=(
            query.message.caption
            + "\n\n✅ **Subscription Approved (3 Days Active)!**"
        )
    )

    await context.bot.send_message(
        chat_id=target_id,
        text=(
            "🎉 **Apnar Subscription Approved hoyeche!** 3 Diner jonno bot-er"
            " sob features active kora hoyeche."
        ),
        reply_markup=get_main_keyboard(target_id),
    )

  elif data.startswith("reject_sub_"):
    target_id = int(data.split("_")[2])
    await query.edit_message_caption(
        caption=query.message.caption + "\n\n❌ **Subscription Rejected!**"
    )
    await context.bot.send_message(
        chat_id=target_id, text="❌ Apnar subscription request-ti batil kora hoyeche."
    )


async def process_otp_success(context, id_num: str, otp: str):
  if id_num not in active_orders:
    return

  order_info = active_orders.pop(id_num)
  uid = order_info["user_id"]
  cost = order_info["cost"]
  phone = order_info["phone"]
  msg_id = order_info["msg_id"]

  users_col.update_one(
      {"user_id": uid}, {"$inc": {"balance": -cost, "otp_count": 1}}
  )

  updated_user = get_user(uid)
  rem_bal = updated_user.get("balance", 0.0) if updated_user else 0.0
  set_number_status(id_num, "end")

  success_text = (
      "💎 **OTP RECEIVED SUCCESSFULLY** 💎\n"
      "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
      f"📱 **Number:** `{phone}`\n"
      f"🔑 **OTP Code:** `{otp}`\n\n"
      f"💵 **Balance Deducted:** `${cost}` USDT\n"
      f"💰 **Remaining Balance:** `${rem_bal:.4f}` USDT"
  )

  try:
    await context.bot.edit_message_text(
        chat_id=uid,
        message_id=msg_id,
        text=success_text,
        parse_mode="Markdown",
    )
  except Exception:
    await context.bot.send_message(
        chat_id=uid, text=success_text, parse_mode="Markdown"
    )

  masked_phone = mask_number(phone)
  group_forward_msg = (
      f"🇭🇰 **Number:** `{masked_phone}`\n"
      f"🔑 **OTP:** `{otp}`\n"
      f"💬 **Message:** `Your WhatsApp code: {otp}`"
  )

  if OTP_GROUP_ID:
    try:
      await context.bot.send_message(
          chat_id=OTP_GROUP_ID, text=group_forward_msg, parse_mode="Markdown"
      )
      logging.info(f"OTP Forwarded to Group {OTP_GROUP_ID} successfully.")
    except Exception as e:
      logging.error(f"Failed to forward OTP to group: {e}")


async def auto_check_otp(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    id_num: str,
    phone_num: str,
    msg_id: int,
):
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
      [InlineKeyboardButton("🌸 Bkash", callback_data="pay_bkash_sub")],
      [InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")],
  ])
  await query.message.reply_text(
      "💳 **Payment Method Select Korun:**", reply_markup=bkash_kb
  )
  return SUB_AMOUNT


async def sub_bkash_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  await query.answer()
  cancel_kb = InlineKeyboardMarkup(
      [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
  )
  await query.message.reply_text(
      "📥 **Subscription Amount (30 Tk) Likhun:**", reply_markup=cancel_kb
  )
  return SUB_AMOUNT


async def sub_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
  text = update.message.text.strip()
  if text != "30":
    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
    )
    await update.message.reply_text(
        "❌ Subscription fee shudhu **30** Tk. Doya kore `30` likhun.",
        reply_markup=cancel_kb,
    )
    return SUB_AMOUNT

  msg = (
      f"💰 **Amount:** `30` Tk\n"
      f"⏳ **Validity:** `3 Days`\n\n"
      f"👇 **Nicher Bkash Personal Number-e Send Money Korun:**\n"
      f"📱 Bkash Number: `{ADMIN_BKASH}`\n\n"
      f"Taka pathanor por apnar **TrxID**-ti likhe message din:"
  )
  cancel_kb = InlineKeyboardMarkup(
      [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
  )
  await update.message.reply_text(
      msg, parse_mode="Markdown", reply_markup=cancel_kb
  )
  return SUB_TXID


async def sub_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
  txid = update.message.text.strip()
  context.user_data["sub_txid"] = txid
  cancel_kb = InlineKeyboardMarkup(
      [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
  )
  await update.message.reply_text(
      "📸 **Ekhon Bkash Payment-er Screenshot (Photo) Pathan:**",
      reply_markup=cancel_kb,
  )
  return SUB_SCREENSHOT


async def sub_screenshot_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  user = update.effective_user
  photo = update.message.photo[-1]
  txid = context.user_data.get("sub_txid")

  admin_kb = InlineKeyboardMarkup([[
      InlineKeyboardButton(
          "✅ Approve", callback_data=f"approve_sub_{user.id}"
      ),
      InlineKeyboardButton("❌ Reject", callback_data=f"reject_sub_{user.id}"),
  ]])

  caption = (
      f"🔔 **Notun Subscription Request!**\n\n"
      f"👤 **User:** {user.full_name} (`{user.id}`)\n"
      f"💰 **Amount:** `30 Tk`\n"
      f"🧾 **TrxID:** `{txid}`"
  )

  await context.bot.send_photo(
      chat_id=ADMIN_ID,
      photo=photo.file_id,
      caption=caption,
      parse_mode="Markdown",
      reply_markup=admin_kb,
  )
  await update.message.reply_text(
      "✅ **Apnar subscription request admin-er kache pathano hoyeche!** Admin"
      " approve korlei bot active hoye jaabe."
  )
  return ConversationHandler.END


# Deposit Conversation Flow
async def deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  payment_kb = InlineKeyboardMarkup([
      [InlineKeyboardButton("💛 Binance Pay", callback_data="pay_binance")],
      [InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")],
  ])
  await update.message.reply_text(
      "💳 **Payment Method select korunk:**", reply_markup=payment_kb
  )
  return WAITING_AMOUNT


async def deposit_binance_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  query = update.callback_query
  await query.answer()
  cancel_kb = InlineKeyboardMarkup(
      [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
  )
  await query.message.reply_text(
      "📥 **Apni koto USDT pathaben ta likhe janan (Minimum: `1` USDT, jemon:"
      " `1`, `2.5`, `5`):**",
      reply_markup=cancel_kb,
  )
  return WAITING_AMOUNT


async def deposit_amount_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  try:
    amount = float(update.message.text.strip())
    if amount < 1.0:
      cancel_kb = InlineKeyboardMarkup(
          [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
      )
      await update.message.reply_text(
          "❌ Minimum deposit amount **1 USDT**. Doya kore 1 ba tar besi amount"
          " likhun.",
          reply_markup=cancel_kb,
      )
      return WAITING_AMOUNT

    context.user_data["dep_amount"] = amount

    msg = (
        f"💰 **Deposit Amount:** `{amount}` USDT\n\n"
        f"👇 **Nicher Binance Pay ID-te Binance app theke Pay/Send Money"
        f" Korun:**\n"
        f"🆔 **Binance Pay ID:** `{BINANCE_ID}`\n\n"
        f"⚠️ **Note:** Minimum deposit 1 USDT. Binance Pay-er madhyome kono"
        f" extra fee charai pathano jabe.\n\n"
        f"Dollar pathanor por apnar **Order ID / TxID**-ti likhe message din:"
    )
    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
    )
    await update.message.reply_text(
        msg, parse_mode="Markdown", reply_markup=cancel_kb
    )
    return WAITING_TXID
  except ValueError:
    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
    )
    await update.message.reply_text(
        "❌ Sothik shongkha likhun (jemon: `1` ba `5`).", reply_markup=cancel_kb
    )
    return WAITING_AMOUNT


async def deposit_txid_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
  txid = update.message.text.strip()
  context.user_data["dep_txid"] = txid
  cancel_kb = InlineKeyboardMarkup(
      [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow_cb")]]
  )
  await update.message.reply_text(
      "📸 **Ekhon apnar payment-er screenshot (Photo) Pathan:**",
      reply_markup=cancel_kb,
  )
  return WAITING_SCREENSHOT


async def deposit_screenshot_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  user = update.effective_user
  photo = update.message.photo[-1]
  amount = context.user_data.get("dep_amount")
  txid = context.user_data.get("dep_txid")

  admin_kb = InlineKeyboardMarkup([[
      InlineKeyboardButton(
          "✅ Approve", callback_data=f"approve_dep_{user.id}_{amount}"
      ),
      InlineKeyboardButton("❌ Reject", callback_data=f"reject_dep_{user.id}"),
  ]])

  caption = (
      f"📥 **Notun Deposit Request!**\n\n"
      f"👤 **User:** {user.full_name} (`{user.id}`)\n"
      f"💰 **Amount:** `${amount}` USDT\n"
      f"🧾 **TxID:** `{txid}`"
  )

  await context.bot.send_photo(
      chat_id=ADMIN_ID,
      photo=photo.file_id,
      caption=caption,
      parse_mode="Markdown",
      reply_markup=admin_kb,
  )
  await update.message.reply_text(
      "✅ **Apnar deposit request admin-er kache pathano hoyeche!** Jaachai kore"
      " druto balance jukto kora hobe."
  )
  return ConversationHandler.END


async def cancel_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
  if update.callback_query:
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("❌ Process batil kora hoyeche.")
  elif update.message:
    await update.message.reply_text("❌ Process batil kora hoyeche.")
  return ConversationHandler.END


# Admin Actions Conversation Handlers
async def admin_ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  await query.answer()
  await query.message.reply_text(
      "🚫 **Banned korte chawa User ID-ti likhe pathan:**"
  )
  return ADMIN_BAN


async def admin_ban_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
  try:
    uid = int(update.message.text.strip())
    users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
    await update.message.reply_text(
        f"✅ User `{uid}`-ke banned kora hoyeche.", parse_mode="Markdown"
    )
  except ValueError:
    await update.message.reply_text("❌ Invalid User ID.")
  return ConversationHandler.END


async def admin_unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  await query.answer()
  await query.message.reply_text(
      "✅ **Unban korte chawa User ID-ti likhe pathan:**"
  )
  return ADMIN_UNBAN


async def admin_unban_process(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  try:
    uid = int(update.message.text.strip())
    users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
    await update.message.reply_text(
        f"✅ User `{uid}`-ke unban kora hoyeche.", parse_mode="Markdown"
    )
  except ValueError:
    await update.message.reply_text("❌ Invalid User ID.")
  return ConversationHandler.END


async def admin_add_bal_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  query = update.callback_query
  await query.answer()
  await query.message.reply_text(
      "➕ **Balance add korte chawa User ID-ti pathan:**"
  )
  return ADMIN_ADD_BAL_USER


async def admin_add_bal_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
  try:
    uid = int(update.message.text.strip())
    context.user_data["target_add_uid"] = uid
    await update.message.reply_text(
        f"💰 **User `{uid}`-er jonno koto USDT balance add korben ta likhun:**",
        parse_mode="Markdown",
    )
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

    await update.message.reply_text(
        f"✅ Successfully added `${amt}` USDT to User `{uid}`. Notun Balance:"
        f" `${new_bal:.4f}` USDT",
        parse_mode="Markdown",
    )
    await context.bot.send_message(
        chat_id=uid,
        text=f"🎉 **Admin apnar account-e `${amt}` USDT balance add koreche!**",
    )
  except ValueError:
    await update.message.reply_text("❌ Invalid Amount.")
  return ConversationHandler.END


async def admin_rate_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  query = update.callback_query
  await query.answer()
  await query.message.reply_text(
      "💵 **WhatsApp (WA)-er notun Bot Rate USDT-te likhun (jemon: `0.075` ba"
      " `0.10`):**"
  )
  return ADMIN_RATE_SET


async def admin_rate_process(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  try:
    raw_val = update.message.text.strip()
    rate = float(raw_val)
    set_rate("wa", rate)
    await update.message.reply_text(
        f"✅ WhatsApp Bot Rate update kora hoyeche: `${rate}` USDT",
        parse_mode="Markdown",
    )
  except ValueError:
    await update.message.reply_text(
        "❌ Invalid Rate Format! (Sothik number likhun, jemon: `0.075`)."
    )
  return ConversationHandler.END


async def admin_broadcast_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  query = update.callback_query
  await query.answer()
  await query.message.reply_text(
      "📢 **Sob user-der jonno Broadcast Message-ti likhe pathan:**"
  )
  return ADMIN_BROADCAST


async def admin_broadcast_process(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
  msg_text = update.message.text.strip()
  users = list(users_col.find())

  count = 0
  for u in users:
    uid = u.get("user_id")
    try:
      await context.bot.send_message(
          chat_id=uid, text=f"📢 **Notice:**\n\n{msg_text}", parse_mode="Markdown"
      )
      count += 1
      await asyncio.sleep(0.05)
    except Exception:
      pass

  await update.message.reply_text(
      f"✅ Total `{count}` jon user-er kache broadcast message pathano hoyeche!",
      parse_mode="Markdown",
  )
  return ConversationHandler.END


def main():
  threading.Thread(target=run_flask, daemon=True).start()

  try:
    loop = asyncio.get_event_loop()
  except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

  app = Application.builder().token(BOT_TOKEN).build()

  # Subscription Flow Handler
  sub_handler = ConversationHandler(
      entry_points=[
          CallbackQueryHandler(sub_start, pattern="^buy_sub_start$")
      ],
      states={
          SUB_AMOUNT: [
              CallbackQueryHandler(sub_bkash_selected, pattern="^pay_bkash_sub$"),
              MessageHandler(
                  filters.TEXT & ~filters.COMMAND, sub_amount_received
              ),
          ],
          SUB_TXID: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, sub_txid_received)
          ],
          SUB_SCREENSHOT: [
              MessageHandler(filters.PHOTO, sub_screenshot_received)
          ],
      },
      fallbacks=[
          CommandHandler("cancel", cancel_flow),
          CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$"),
      ],
  )

  # Deposit Flow Handler
  dep_handler = ConversationHandler(
      entry_points=[MessageHandler(filters.Regex("^💵 Deposit$"), deposit_start)],
      states={
          WAITING_AMOUNT: [
              CallbackQueryHandler(
                  deposit_binance_selected, pattern="^pay_binance$"
              ),
              MessageHandler(
                  filters.TEXT & ~filters.COMMAND, deposit_amount_received
              ),
          ],
          WAITING_TXID: [
              MessageHandler(
                  filters.TEXT & ~filters.COMMAND, deposit_txid_received
              )
          ],
          WAITING_SCREENSHOT: [
              MessageHandler(filters.PHOTO, deposit_screenshot_received)
          ],
      },
      fallbacks=[
          CommandHandler("cancel", cancel_flow),
          CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$"),
      ],
  )

  # Admin Conversation Handler
  admin_handler = ConversationHandler(
      entry_points=[
          CallbackQueryHandler(admin_ban_start, pattern="^admin_ban_start$"),
          CallbackQueryHandler(admin_unban_start, pattern="^admin_unban_start$"),
          CallbackQueryHandler(
              admin_add_bal_start, pattern="^admin_add_bal_start$"
          ),
          CallbackQueryHandler(admin_rate_start, pattern="^admin_rate_start$"),
          CallbackQueryHandler(
              admin_broadcast_start, pattern="^admin_broadcast_start$"
          ),
      ],
      states={
          ADMIN_BAN: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_process)
          ],
          ADMIN_UNBAN: [
              MessageHandler(
                  filters.TEXT & ~filters.COMMAND, admin_unban_process
              )
          ],
          ADMIN_ADD_BAL_USER: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_user)
          ],
          ADMIN_ADD_BAL_AMT: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_bal_amt)
          ],
          ADMIN_RATE_SET: [
              MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_process)
          ],
          ADMIN_BROADCAST: [
              MessageHandler(
                  filters.TEXT & ~filters.COMMAND, admin_broadcast_process
              )
          ],
      },
      fallbacks=[
          CommandHandler("cancel", cancel_flow),
          CallbackQueryHandler(cancel_flow, pattern="^cancel_flow_cb$"),
      ],
  )

  app.add_handler(CommandHandler("start", start))
  app.add_handler(sub_handler)
  app.add_handler(dep_handler)
  app.add_handler(admin_handler)

  # GLOBAL CallbackQueryHandler for non-state inline buttons
  app.add_handler(CallbackQueryHandler(handle_callbacks))

  app.add_handler(
      MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages)
  )

  print("Rex Private Bot Running...")
  app.run_polling(close_loop=False)


if __name__ == "__main__":
  main()
