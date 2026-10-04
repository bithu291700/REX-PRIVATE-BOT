import logging
import os
import threading
import asyncio
import re
from datetime import datetime, timedelta
from flask import Flask, request
from pymongo import MongoClient
import requests

from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
import logging
import os
import threading
import asyncio
import re
from datetime import datetime, timedelta
from flask import Flask, request
from pymongo import MongoClient
import requests

from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

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
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

# MongoDB Setup
if not MONGODB_URI:
    logging.error("❌ MONGODB_URI Environment Variable missing!")
client = MongoClient(MONGODB_URI)
db = client["vaksms_bot_db"]

users_col = db["users"]
settings_col = db["settings"]

# Aiogram Setup
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Flask Web Server
flask_app = Flask("")

@flask_app.route("/")
def home():
    return "Rex Private Telegram Bot is Active (Aiogram Webhook)!", 200

@flask_app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    if request.method == "POST":
        json_data = request.get_json(force=True)
        update = types.Update(**json_data)
        asyncio.run(dp.feed_update(bot, update))
        return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# In-Memory Active Orders
active_orders = {}

# FSM States
class GroupVerifyStates(StatesGroup):
    username = State()
    screenshot = State()

class SubStates(StatesGroup):
    amount = State()
    txid = State()
    screenshot = State()

class DepositStates(StatesGroup):
    amount = State()
    txid = State()
    screenshot = State()

class AdminStates(StatesGroup):
    ban = State()
    unban = State()
    add_bal_user = State()
    add_bal_amt = State()
    zero_bal_user = State()
    rate_wa_hk = State()
    rate_wa_cl = State()
    rate_tg_hk = State()
    rate_tg_cl = State()
    broadcast = State()

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
    defaults = {"wa_hk": 0.10, "wa_cl": 0.10, "tg_hk": 0.12, "tg_cl": 0.12}
    return defaults.get(key, 0.10)

def set_rate(service_code: str, country_code: str, rate: float):
    key = f"{service_code.lower()}_{country_code.lower()}"
    settings_col.update_one({"type": "rates"}, {"$set": {f"rates.{key}": rate}}, upsert=True)

def is_bot_active() -> bool:
    doc = settings_col.find_one({"type": "bot_status"})
    return doc.get("is_active", True) if doc else True

def set_bot_active(status: bool):
    settings_col.update_one({"type": "bot_status"}, {"$set": {"is_active": status}}, upsert=True)

def is_subscribed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    user = get_user(user_id)
    if user and user.get("subscription_expiry"):
        if datetime.now() < user["subscription_expiry"]:
            return True
    return False

def get_main_keyboard(user_id):
    keyboard = [
        [KeyboardButton(text="💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴"), KeyboardButton(text="🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁")],
        [KeyboardButton(text="🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂"), KeyboardButton(text="📱 𝚂𝙴𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴")],
        [KeyboardButton(text="👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴"), KeyboardButton(text="💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃")]
    ]
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton(text="⚙️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

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
                    if float(assigned_price) > max_price:
                        set_number_status(str(res["idNum"]), "bad")
                        return {"error": "Stock Out!"}
                except ValueError:
                    pass
        return res
    except Exception:
        return {"error": "Stock Out!"}

def get_vak_balance():
    url = f"https://vak-sms.com/api/getBalance/?apiKey={VAK_SMS_API_KEY}"
    try:
        return requests.get(url).json().get("balance", 0.0)
    except Exception:
        return 0.0

def fetch_otp_code(id_num: str):
    url = f"https://vak-sms.com/api/getSmsCode/?apiKey={VAK_SMS_API_KEY}&idNum={id_num}"
    try:
        return requests.get(url).json()
    except Exception as e:
        return {"error": str(e)}

# Handlers
@dp.message(F.text == "/start")
async def start_cmd(message: types.Message):
    user = message.from_user
    user_id = user.id
    u_data = get_or_create_user(user_id, user.full_name)

    if u_data.get("is_banned", False):
        await message.answer("❌ 𝙱𝙰𝙽 𝙱𝚈 𝙰𝙳𝙼𝙸𝙽 𝙲𝙾𝙽𝚃𝙰𝙲𝚃 𝙰𝙳𝙼𝙸𝙽.", reply_markup=ReplyKeyboardRemove())
        return

    if not is_bot_active() and user_id != ADMIN_ID:
        await message.answer("🚧 **ʙᴏᴛ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴀɪɴɪɴɢ ʙʏ ᴀᴅᴍɪɴ.** ᴘʟᴇᴀsᴇ ᴛʀʏ sᴏᴍᴇ ᴛɪᴍᴇ.", parse_mode="Markdown")
        return

    if not is_subscribed(user_id):
        if not u_data.get("is_group_verified", False):
            verify_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Verify Group Membership", callback_data="start_group_verify")]
            ])
            msg = (
                f"👋 **Hello {user.full_name}!**\n\n"
                f"❌ 𝚈𝙾𝚄 𝙳𝙾𝙽'𝚃 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚃𝙷𝙴 𝙱𝙾𝚃!\n"
                f"ʙᴏᴛ ʙᴇʙᴏʜᴀʀ ᴋᴏʀᴛᴇ ᴄʜᴀɪʟᴇ prothomti amader **Private Group**-e join thakte hobe.\n\n"
                f"📌 Nicher button-e click kore apnar group join-er proof (Username & Screenshot) admin-er kache pathan:"
            )
            await message.answer(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
            await message.answer("👇 **Verification:**", reply_markup=verify_kb)
            return

        sub_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽(30 Tk / 3 Days)", callback_data="buy_sub_start")]
        ])
        msg = (
            f"👋 **Hello {user.full_name}!**\n\n"
            f"❌ 𝚈𝙾𝚄 𝙳𝙾𝙽'𝚃 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚃𝙷𝙴 𝙱𝙾𝚃!\n"
            f"ʙᴏᴛ ʙᴇʙᴏʜᴀʀ ᴋᴏʀᴛᴇ ᴄʜᴀɪʟᴇ sᴜʙsᴄʀɪᴘᴛɪᴏɴ ɴɪᴛᴇ ʜᴏʙᴇ.\n\n"
            f"📌 **𝗣𝗥𝗜𝗖𝗘:** `30 Tk`\n"
            f"⏳ **𝗩𝗔𝗟𝗜𝗗𝗜𝗧𝗬:** `3 Days`\n\n"
            f"ɴɪᴄʜᴇʀ ᴍᴇɴᴜ ᴛʜᴇᴋᴇ ᴄʟɪᴄᴋ ᴋᴏʀᴇ sᴜʙsᴄ𝚁𝙸𝙿𝚃𝙸𝙾𝙽 ᴋɪɴᴜɴ:"
        )
        await message.answer(msg, parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        await message.answer("👇 **𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽:**", reply_markup=sub_kb)
        return

    exp_time = u_data.get("subscription_expiry")
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user_id != ADMIN_ID) else "Unlimited (Admin)"
    curr_country_code = u_data.get("selected_country", "hk")
    curr_country = curr_country_code.upper()
    country_flag = get_country_flag(curr_country_code)
    curr_service = u_data.get("selected_service", "tg").upper()

    welcome_msg = (
        f"👋 **𝚆𝙴𝙻𝙲𝙾𝙼𝙴 𝚁𝙴𝚇 𝙿𝚁𝙸𝚅𝙰𝚃𝙴 𝙱𝙾𝚃!**\n\n"
        f"⚙️ **𝚁𝙴𝙲𝙴𝙽𝚃 𝚂𝙴𝚃𝚄𝙿:**\n"
        f"• 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂: `{curr_country}` {country_flag}\n"
        f"• Service: `{curr_service}`\n"
        f"• 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${u_data.get('balance', 0.0):.4f} USDT`\n"
        f"• 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚅𝙰𝙻𝙸𝙳 𝚃𝙸𝙻𝙻: `{exp_str}`\n\n"
        f"𝙺𝙰𝙹 𝙺𝙾𝚁𝚃𝙴 𝙽𝙸𝙲𝙷𝙴 𝙳𝙴𝙰 𝙼𝙴𝙽𝚄 𝚄𝚂𝙴 𝙺𝙾𝚁𝙴𝙽:"
    )
    await message.answer(welcome_msg, parse_mode="Markdown", reply_markup=get_main_keyboard(user_id))

@dp.message(F.text == "💳 𝙰𝙲𝙲𝙾𝚄𝙽𝚃 𝙱𝙰𝙻𝙰𝙽𝙲𝙴")
async def account_balance(message: types.Message):
    u_data = get_or_create_user(message.from_user.id, message.from_user.full_name)
    bot_bal = u_data.get("balance", 0.0)
    msg = f"💰 **𝙼𝚈 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${bot_bal:.4f}` USDT"
    if message.from_user.id == ADMIN_ID:
        site_bal = get_vak_balance()
        msg += f"\n🏦 **𝙿𝙰𝙽𝙴𝙻 𝙱𝙰𝙻𝙰𝙽𝙲𝙴 :** `${site_bal:.4f}` USD"
    await message.answer(msg, parse_mode="Markdown")

@dp.message(F.text == "👤 𝙼𝚈 𝙿𝚁𝙾𝙵𝙸𝙻𝙴")
async def my_profile(message: types.Message):
    user = message.from_user
    u_data = get_or_create_user(user.id, user.full_name)
    bot_bal = u_data.get("balance", 0.0)
    otp_cnt = u_data.get("otp_count", 0)
    exp_time = u_data.get("subscription_expiry")
    exp_str = exp_time.strftime("%Y-%m-%d %H:%M") if (exp_time and user.id != ADMIN_ID) else "Unlimited (Admin)"
    profile_msg = (
        f"👤 **Apnar Profile Info:**\n\n"
        f"🆔 **User ID:** `{user.id}`\n"
        f"📛 **Name:** {user.full_name}\n"
        f"💵 **Balance:** `${bot_bal:.4f}` USDT\n"
        f"📩 **Total OTP Received:** `{otp_cnt}`\n"
        f"📅 **Subscription Valid:** `{exp_str}`"
    )
    await message.answer(profile_msg, parse_mode="Markdown")

@dp.message(F.text.in_({"🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝚈", "🌐 𝚂𝙴𝚃 𝙲𝙾𝚄𝙽𝚃𝚁𝙸𝙴𝚂"}))
async def set_country_menu(message: types.Message):
    country_kb = [
        [KeyboardButton(text="COUNTRY: HK 🇭🇰 (HONG KONG)"), KeyboardButton(text="COUNTRY: CHILE 🇨🇱 (CL)")],
        [KeyboardButton(text="🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")]
    ]
    await message.answer("🌐 **SELECT YOUR COUNTRY:**", reply_markup=ReplyKeyboardMarkup(keyboard=country_kb, resize_keyboard=True))

@dp.message(F.text.contains("HK"))
async def set_hk_country(message: types.Message):
    users_col.update_one({"user_id": message.from_user.id}, {"$set": {"selected_country": "hk"}})
    await message.answer("✅ Country set: `HONG KONG (HK)` 🇭🇰", parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text.or_(F.text.contains("CHILE"), F.text.contains("CL")))
async def set_cl_country(message: types.Message):
    if "CHILE" in message.text or "CL" in message.text:
        users_col.update_one({"user_id": message.from_user.id}, {"$set": {"selected_country": "cl"}})
        await message.answer("✅ Country set: `CHILE (CL)` 🇨🇱", parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text == "📱 𝚂𝙴𝚃 𝚂𝙴𝚁𝚅𝙸𝙲𝙴")
async def set_service_menu(message: types.Message):
    service_kb = [
        [KeyboardButton(text="𝚂𝙴𝚁𝚅𝙸𝙲𝙴: TG (𝚃𝙴𝙻𝙴𝙶𝚁𝙰𝙼)")],
        [KeyboardButton(text="𝚂𝙴𝚁𝚅𝙸𝙲𝙴: WA (𝚆𝙷𝙰𝚃𝚂𝙰𝙿𝙿)")],
        [KeyboardButton(text="🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")]
    ]
    await message.answer("📱 **SELECT YOUR SERVICE:**", reply_markup=ReplyKeyboardMarkup(keyboard=service_kb, resize_keyboard=True))

@dp.message(F.text.contains("TG") | F.text.contains("TELEGRAM"))
async def set_tg_service(message: types.Message):
    users_col.update_one({"user_id": message.from_user.id}, {"$set": {"selected_service": "tg"}})
    await message.answer("✅ 𝚂𝙴𝚁𝚅𝙸𝙲𝙴 𝚂𝙴𝚃: `TELEGRAM (TG)`", parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text.contains("WA") | F.text.contains("WHATSAPP"))
async def set_wa_service(message: types.Message):
    users_col.update_one({"user_id": message.from_user.id}, {"$set": {"selected_service": "wa"}})
    await message.answer("✅ 𝚂𝙴𝚁𝚅𝙸𝙲𝙴 𝚂𝙴𝚃: `WHATSAPP (WA)`", parse_mode="Markdown", reply_markup=get_main_keyboard(message.from_user.id))

@dp.message(F.text == "🔙 𝙼𝙰𝙸𝙽 𝙼𝙴𝙽𝚄")
async def main_menu(message: types.Message):
    await start_cmd(message)

@dp.message(F.text == "🛒 𝙱𝚈 𝙽𝚄𝙼𝙱𝙴𝚁")
async def buy_number_handler(message: types.Message):
    user_id = message.from_user.id
    u_data = get_or_create_user(user_id, message.from_user.full_name)

    user_has_active = any(order.get("user_id") == user_id for order in active_orders.values())
    if user_has_active:
        await message.answer("⚠️ **অলরেডি একটি নম্বর কেনা রয়েছে!**\nনতুন নম্বর কেনার আগে আগের নম্বরটি ব্যবহার সম্পন্ন করুন অথবা Cancel করুন.")
        return

    country = u_data.get("selected_country", "hk")
    service = u_data.get("selected_service", "tg")
    country_flag = get_country_flag(country)

    if country == "hk" and service == "wa":
        max_price_limit = 0.07
    elif country == "cl" and service == "wa":
        max_price_limit = 0.079
    elif country == "cl":
        max_price_limit = 0.087
    else:
        max_price_limit = 0.075

    bot_rate = get_rate(service_code=service, country_code=country)
    user_bal = u_data.get("balance", 0.0)

    if user_bal < bot_rate:
        await message.answer(f"❌ 𝚂𝙾𝚁𝚁𝚈 𝙳𝙾 𝙽𝙾𝚃𝙴 𝙰𝙽𝙰𝙵 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${bot_rate}` USDT, 𝚈𝙾𝚄𝚁 𝙱𝙰𝙻𝙰𝙽𝙲𝙴: `${user_bal:.4f}` USDT.\n𝙳𝙸𝙿𝙾𝚂𝙸𝚃 𝙺𝙾𝚁𝚄𝙽.")
        return

    status_msg = await message.answer(f"⏳ `{country.upper()}` {country_flag} BUYING NUMBER... WAIT A FEW SECONDS.")
    res = buy_vak_number(service=service, country=country, max_price=max_price_limit)

    if isinstance(res, dict) and "tel" in res and "idNum" in res:
        raw_phone = str(res["tel"])
        phone_num = f"+{raw_phone}" if not raw_phone.startswith("+") else raw_phone
        id_num = str(res["idNum"])

        inline_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📩 Check Active OTP", callback_data=f"check_otp_{id_num}")],
            [InlineKeyboardButton(text="❌ Cancel Number", callback_data=f"cancel_num_{id_num}")]
        ])

        sent_msg = await message.answer(
            f"✅ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙱𝚄𝙸𝙻𝙳 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝚈!**\n\n"
            f"📱 **Number:** `<code>{phone_num}</code>`\n"
            f"🆔 **ID Num:** `{id_num}`\n"
            f"🌍 **Country:** `{country.upper()}` {country_flag}\n"
            f"💬 **Service:** `{service.upper()}`\n"
            f"💵 **Rate:** `${bot_rate}` USDT *(𝙊𝙏𝙋 𝘼𝙎𝙇𝙀𝙄 𝘽𝘼𝙇𝘼𝙽𝙲𝙴 𝙆𝘼𝙏𝘽𝙀)*\n\n"
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

        asyncio.create_task(auto_check_otp(user_id, id_num, str(phone_num), sent_msg.message_id))
    else:
        err_msg = res.get("error", "Stock Out!") if isinstance(res, dict) else "Stock Out!"
        await message.answer(f"❌ `{err_msg}`")

@dp.message(F.text == "⚙️️ 𝙰𝙳𝙼𝙸𝙽 𝙿𝙰𝙽𝙴𝙻")
async def admin_panel_text(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await send_admin_panel(message)

async def send_admin_panel(message_or_callback):
    status_str = "🟢 ON (Active)" if is_bot_active() else "🔴 OFF (Maintenance)"
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 𝗩𝗜𝗘𝗪 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥", callback_data="admin_view_users")],
        [InlineKeyboardButton(text="🚫 𝗕𝗔𝗡 𝗨𝗦𝗘𝗥", callback_data="admin_ban_start"), InlineKeyboardButton(text="✅ Unban User", callback_data="admin_unban_start")],
        [InlineKeyboardButton(text="💵 SET HK WA PRICE", callback_data="admin_rate_wa_hk_start"), InlineKeyboardButton(text="💵 SET CL WA PRICE", callback_data="admin_rate_wa_cl_start")],
        [InlineKeyboardButton(text="💵 SET HK TG PRICE", callback_data="admin_rate_tg_hk_start"), InlineKeyboardButton(text="💵 SET CL TG PRICE", callback_data="admin_rate_tg_cl_start")],
        [InlineKeyboardButton(text="➕ Add Balance", callback_data="admin_add_bal_start"), InlineKeyboardButton(text="🔄 𝗭𝙴𝚁𝙾 𝗕𝙰𝙻𝙰𝙽𝙲𝙴", callback_data="admin_zero_bal_start")],
        [InlineKeyboardButton(text="📢 𝗕𝗥𝗢𝙳𝙲𝙰𝚂𝚃 𝙰𝙻𝙻", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(text=f"𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦: {status_str}", callback_data="admin_toggle_bot")]
    ])
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer("🛠 **Admin Control Panel:**", reply_markup=admin_kb, parse_mode="Markdown")
    elif isinstance(message_or_callback, types.CallbackQuery):
        await message_or_callback.message.answer("🛠 **Admin Control Panel:**", reply_markup=admin_kb, parse_mode="Markdown")

# Callback Query Routers
@dp.callback_query(F.data == "admin_view_users")
async def admin_view_users_cb(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return
    await query.answer()
    try:
        subscribed_users = list(users_col.find({"subscription_expiry": {"$gt": datetime.now()}}))
        if not subscribed_users:
            await query.message.answer("📋 Currently, there are no active subscribed users.")
            return
        msg = f"👥 **Active Subscribed Users ({len(subscribed_users)}):**\n\n"
        for u in subscribed_users:
            uid = u.get("user_id", "N/A")
            safe_name = str(u.get("full_name", "User")).replace("*", "").replace("_", "").replace("`", "")
            bal = u.get("balance", 0.0)
            otp_cnt = u.get("otp_count", 0)
            msg += f"• **{safe_name}** (`{uid}`)\n  └ 💰 Balance: `${bal:.4f}` USDT | 📩 OTP Rcv: `{otp_cnt}`\n\n"
        await query.message.answer(msg, parse_mode="Markdown")
    except Exception as e:
        await query.message.answer(f"❌ Error loading users: {str(e)}")

@dp.callback_query(F.data == "admin_toggle_bot")
async def admin_toggle_bot_cb(query: types.CallbackQuery):
    if query.from_user.id != ADMIN_ID:
        return
    await query.answer()
    new_status = not is_bot_active()
    set_bot_active(new_status)
    status_text = "🟢 **Bot ON (Active) kora hoyeche!**" if new_status else "🔴 **Bot OFF (Maintenance Mode) kora hoyeche!**"
    status_str = "🟢 ON (Active)" if new_status else "🔴 OFF (Maintenance)"
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 𝗩𝗜𝗘𝗪 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥", callback_data="admin_view_users")],
        [InlineKeyboardButton(text="🚫 𝗕𝗔𝗡 𝗨𝗦𝗘𝗥", callback_data="admin_ban_start"), InlineKeyboardButton(text="✅ Unban User", callback_data="admin_unban_start")],
        [InlineKeyboardButton(text="💵 SET HK WA PRICE", callback_data="admin_rate_wa_hk_start"), InlineKeyboardButton(text="💵 SET CL WA PRICE", callback_data="admin_rate_wa_cl_start")],
        [InlineKeyboardButton(text="💵 SET HK TG PRICE", callback_data="admin_rate_tg_hk_start"), InlineKeyboardButton(text="💵 SET CL TG PRICE", callback_data="admin_rate_tg_cl_start")],
        [InlineKeyboardButton(text="➕ Add Balance", callback_data="admin_add_bal_start"), InlineKeyboardButton(text="🔄 𝗭𝙴𝚁𝙾 𝗕𝙰𝙻𝙰𝙽𝙲𝙴", callback_data="admin_zero_bal_start")],
        [InlineKeyboardButton(text="📢 𝗕𝗥𝗢𝙳𝙲𝙰𝚂𝚃 𝙰𝙻𝙻", callback_data="admin_broadcast_start")],
        [InlineKeyboardButton(text=f"𝗕𝗢𝗧 𝗦𝗧𝗔𝗧𝗨𝗦: {status_str}", callback_data="admin_toggle_bot")]
    ])
    try:
        await query.message.edit_reply_markup(reply_markup=admin_kb)
    except Exception:
        pass
    await query.message.answer(status_text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("check_otp_"))
async def check_otp_cb(query: types.CallbackQuery):
    await query.answer()
    id_num = query.data.split("_")[2]
    res = fetch_otp_code(id_num)
    if isinstance(res, dict) and "smsCode" in res and res["smsCode"]:
        await process_otp_success(id_num, res["smsCode"])
    else:
        await query.message.answer("⏳ 𝙰𝙺𝙷𝙾𝙽𝙾 𝙾𝚃𝙿 𝙰𝚂𝙴𝙽𝙸, 𝙰𝙺𝚃𝚄 𝙿𝙾𝚁𝙴 𝙰𝙱𝙰𝚁 𝚃𝚁𝙸 𝙺𝙾𝚁𝚄𝙽.")

@dp.callback_query(F.data.startswith("cancel_num_"))
async def cancel_num_cb(query: types.CallbackQuery):
    await query.answer()
    id_num = query.data.split("_")[2]
    if id_num in active_orders:
        set_number_status(id_num, "bad")
        active_orders.pop(id_num, None)
        try:
            await query.message.edit_text("❌ **𝙽𝚄𝙼𝙱𝙴𝚁 𝙲𝙰𝙽𝙲𝙴𝙻𝙴𝙳(𝙱𝙰𝙻𝙰𝙽𝙲𝙴 𝙺𝙰𝚃𝙰 𝙷𝙾𝚈𝙽𝙸).**", reply_markup=None)
        except Exception:
            await query.message.delete()
    else:
        await query.message.answer("❌ 𝙳𝙾𝙽'𝚃 𝙰𝙲𝚃𝙸𝚅𝙴 𝙾𝚁𝙳𝙴𝚁 𝙽𝙰𝙷𝙾𝙻𝙴 𝙾𝚃𝙿 𝙰𝙻𝚁𝙴𝙰𝙳𝚈 𝚁𝙴𝙲𝙴𝙸𝚅𝙴𝙳 𝙺𝙾𝚁𝙰 𝙷𝙾𝙸𝙲𝙷𝙴.")

@dp.callback_query(F.data.startswith("approve_dep_"))
async def approve_dep_cb(query: types.CallbackQuery):
    parts = query.data.split("_")
    target_id = int(parts[2])
    amount = float(parts[3])
    users_col.update_one({"user_id": target_id}, {"$inc": {"balance": amount}})
    await query.message.edit_caption(caption=(query.message.caption or "") + "\n\n✅ **Approved & Balance Added!**")
    await bot.send_message(chat_id=target_id, text=f"🎉 **Apnar `${amount}` USDT deposit shofolbhabe jukto kora hoyeche!**")

@dp.callback_query(F.data.startswith("reject_dep_"))
async def reject_dep_cb(query: types.CallbackQuery):
    target_id = int(query.data.split("_")[2])
    await query.message.edit_caption(caption=(query.message.caption or "") + "\n\n❌ **Deposit Rejected!**")
    await bot.send_message(chat_id=target_id, text="❌ Apnar deposit request-ti batil kora hoyeche.")

@dp.callback_query(F.data.startswith("approve_sub_"))
async def approve_sub_cb(query: types.CallbackQuery):
    target_id = int(query.data.split("_")[2])
    expiry_date = datetime.now() + timedelta(days=3)
    users_col.update_one({"user_id": target_id}, {"$set": {"subscription_expiry": expiry_date}})
    await query.message.edit_caption(caption=(query.message.caption or "") + "\n\n✅ **Subscription Approved (3 Days Active)!**")
    await bot.send_message(chat_id=target_id, text="🎉 **Apnar Subscription Approved hoyeche!** 3 Diner jonno bot-er sob features active kora hoyeche.", reply_markup=get_main_keyboard(target_id))

@dp.callback_query(F.data.startswith("reject_sub_"))
async def reject_sub_cb(query: types.CallbackQuery):
    target_id = int(query.data.split("_")[2])
    await query.message.edit_caption(caption=(query.message.caption or "") + "\n\n❌ **Subscription Rejected!**")
    await bot.send_message(chat_id=target_id, text="❌ Apnar subscription request-ti batil kora hoyeche.")

@dp.callback_query(F.data.startswith("verify_approve_"))
async def verify_approve_cb(query: types.CallbackQuery):
    target_id = int(query.data.split("_")[2])
    users_col.update_one({"user_id": target_id}, {"$set": {"is_group_verified": True}})
    await query.message.edit_caption(caption=(query.message.caption or "") + "\n\n✅ **Group Membership Verified!**")
    sub_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 𝙱𝚄𝚈 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽(30 Tk / 3 Days)", callback_data="buy_sub_start")]])
    await bot.send_message(chat_id=target_id, text="🎉 **Apnar Group Verification Admin কর্তৃক Approved হয়েছে!** এখন আপনি নিচের বাটন থেকে সাবস্ক্রিপশন কিনতে পারবেন:", reply_markup=sub_kb)

@dp.callback_query(F.data.startswith("verify_reject_"))
async def verify_reject_cb(query: types.CallbackQuery):
    target_id = int(query.data.split("_")[2])
    users_col.update_one({"user_id": target_id}, {"$set": {"is_group_verified": False}})
    await query.message.edit_caption(caption=(query.message.caption or "") + "\n\n❌ **Group Membership Unverified!**")
    await bot.send_message(chat_id=target_id, text="❌ আপনার গ্রুপ ভেরিফিকেশন প্রুফ সঠিক পাওয়া যায়নি। দয়া করে সঠিক স্ক্রিনশট ও ইউজারনেম দিয়ে পুনরায় চেষ্টা করুন।")

@dp.callback_query(F.data == "cancel_flow_cb")
async def cancel_flow_cb(query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await query.answer()
    await query.message.answer("❌ Process batil kora hoyeche.")

# Group Verification Conversation Handlers
@dp.callback_query(F.data == "start_group_verify")
async def group_verify_start(query: types.CallbackQuery, state: FSMContext):
    await query.answer()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.answer("✍️ **Doya kore apnar Telegram Username-ti likhe pathan (jemon: `@username`):**", reply_markup=cancel_kb)
    await state.set_state(GroupVerifyStates.username)

@dp.message(GroupVerifyStates.username, F.text)
async def group_verify_username(message: types.Message, state: FSMContext):
    await state.update_data(verify_username=message.text.strip())
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    await message.answer("📸 **Ekhon apnar Private Group-e add achen tar Screenshot (Photo) pathan:**", reply_markup=cancel_kb)
    await state.set_state(GroupVerifyStates.screenshot)

@dp.message(GroupVerifyStates.screenshot, F.photo)
async def group_verify_screenshot(message: types.Message, state: FSMContext):
    user = message.from_user
    photo = message.photo[-1]
    data = await state.get_data()
    username = data.get("verify_username")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Verified", callback_data=f"verify_approve_{user.id}"),
            InlineKeyboardButton(text="❌ Unverified", callback_data=f"verify_reject_{user.id}")
        ]
    ])
    caption = (
        f"🔍 **New Private Group Verification Request!**\n\n"
        f"👤 **User:** {user.full_name} (`{user.id}`)\n"
        f"📌 **Username:** `{username}`"
    )
    await bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="Markdown", reply_markup=admin_kb)
    await message.answer("✅ **Apnar verification request admin-er kache pathano hoyeche!** Admin check kore verify korlei apnake subscription option dewa hobe.")
    await state.clear()

# Subscription Flow Handlers
@dp.callback_query(F.data == "buy_sub_start")
async def sub_start(query: types.CallbackQuery, state: FSMContext):
    await query.answer()
    bkash_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌸 𝙱𝙺𝙰𝚂𝙷", callback_data="pay_bkash_sub")],
        [InlineKeyboardButton(text="❌ 𝙲𝙰𝙽𝙲𝙴𝙻", callback_data="cancel_flow_cb")]
    ])
    await query.message.answer("💳 **𝙿𝙰𝚈𝙼𝙴𝙽𝚃 𝙼𝙴𝚃𝙷𝙾𝙳 𝚂𝙴𝙻𝙴𝙲𝚃 𝙺𝙾𝚁𝚄𝙽:**", reply_markup=bkash_kb)
    await state.set_state(SubStates.amount)

@dp.callback_query(SubStates.amount, F.data == "pay_bkash_sub")
async def sub_bkash_selected(query: types.CallbackQuery, state: FSMContext):
    await query.answer()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.answer("📥 **𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙰𝙼𝙾𝚄𝙽𝚃 (30 Tk) 𝙻𝙸𝙺𝙷𝚄𝙽:**", reply_markup=cancel_kb)

@dp.message(SubStates.amount, F.text)
async def sub_amount_received(message: types.Message, state: FSMContext):
    text = message.text.strip()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    if text != "30":
        await message.answer("❌ 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝙵𝙴𝙴 𝚂𝚄𝙳𝙷𝚄 **30** Tk. 𝙴𝙽𝚃𝙴𝚁 `30` likhun.", reply_markup=cancel_kb)
        return
    msg = (
        f"💰 **𝙰𝙼𝙾𝚄𝙽𝚃:** `30` Tk\n"
        f"⏳ **𝚅𝙰𝙻𝙸𝙳𝙸𝚃𝙸:** `3 Days`\n\n"
        f"👇 **𝚂𝙴𝙽𝙳 𝙱𝙺𝙰𝚂𝙷 𝙿𝙴𝚁𝚂𝙾𝙽𝙰𝙻 𝙽𝚄𝙼𝙱𝙴𝚁:**\n"
        f"📱 𝙱𝙺𝙰𝚂𝙷 𝙽𝚄𝙼𝙱𝙴𝚁: `{ADMIN_BKASH}`\n\n"
        f"𝚃𝙰𝙺𝙰 𝙳𝙴𝙰 𝚂𝙴𝚂𝙴 𝚃𝚁𝚇 𝙸𝙳 **TrxID**-𝚃𝙸 𝙻𝙸𝙺𝙷𝙴 𝙿𝙰𝚃𝙷𝙰𝙽:"
    )
    await message.answer(msg, parse_mode="Markdown", reply_markup=cancel_kb)
    await state.set_state(SubStates.txid)

@dp.message(SubStates.txid, F.text)
async def sub_txid_received(message: types.Message, state: FSMContext):
    await state.update_data(sub_txid=message.text.strip())
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    await message.answer("📸 **𝙱𝙺𝙰𝚂𝙷 𝙿𝙰𝚈𝙼𝙴𝙽𝚃 𝚂𝙲𝚁𝙴𝙴𝙽𝚂𝙷𝙾𝚃(Photo) 𝙳𝙸𝙽:**", reply_markup=cancel_kb)
    await state.set_state(SubStates.screenshot)

@dp.message(SubStates.screenshot, F.photo)
async def sub_screenshot_received(message: types.Message, state: FSMContext):
    user = message.from_user
    photo = message.photo[-1]
    data = await state.get_data()
    txid = data.get("sub_txid")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 𝙰𝙿𝙿𝚁𝙾𝚅𝙴𝙳", callback_data=f"approve_sub_{user.id}"),
            InlineKeyboardButton(text="❌ 𝚁𝙴𝙹𝙴𝙲𝚃𝙴𝙳", callback_data=f"reject_sub_{user.id}")
        ]
    ])
    caption = (
        f"🔔 **𝙽𝙴𝚆 𝚂𝚄𝙱𝚂𝙲𝚁𝙸𝙿𝚃𝙸𝙾𝙽 𝚁𝙴𝙹𝚄𝙴𝚂𝚃!**\n\n"
        f"👤 **𝚄𝚂𝙴𝚁:** {user.full_name} (`{user.id}`)\n"
        f"💰 **𝙰𝙼𝙾𝚄𝙽𝚃:** `30 Tk`\n"
        f"🧾 **𝚃𝚁𝚇𝙸𝙳:** `{txid}`"
    )
    await bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="Markdown", reply_markup=admin_kb)
    await message.answer("✅ **Apnar subscription request admin-er kache pathano hoyeche!** Admin approve korlei bot active hoye jaabe.")
    await state.clear()

# Deposit Flow Handlers
@dp.message(F.text == "💵 𝙳𝙸𝙿𝙾𝚂𝙸𝚃")
async def deposit_start(message: types.Message, state: FSMContext):
    payment_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💛 Binance Pay", callback_data="pay_binance")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]
    ])
    await message.answer("💳 **Payment Method select korunk:**", reply_markup=payment_kb)
    await state.set_state(DepositStates.amount)

@dp.callback_query(DepositStates.amount, F.data == "pay_binance")
async def deposit_binance_selected(query: types.CallbackQuery, state: FSMContext):
    await query.answer()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.answer("📥 **Apni koto USDT pathaben ta likhe janan (Minimum: `1` USDT, jemon: `1`, `2.5`, `5`):**", reply_markup=cancel_kb)

@dp.message(DepositStates.amount, F.text)
async def deposit_amount_received(message: types.Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    try:
        amount = float(message.text.strip())
        if amount < 1.0:
            await message.answer("❌ Minimum deposit amount **1 USDT**. Doya kore 1 ba tar besi amount likhun.", reply_markup=cancel_kb)
            return
        await state.update_data(dep_amount=amount)
        msg = (
            f"💰 **Deposit Amount:** `{amount}` USDT\n\n"
            f"👇 **Nicher Binance Pay ID-te Binance app theke Pay/Send Money Korun:**\n"
            f"🆔 **Binance Pay ID:** `{BINANCE_ID}`\n\n"
            f"⚠️ **Note:** Minimum deposit 1 USDT. Binance Pay-er madhyome kono extra fee charai pathano jabe.\n\n"
            f"Dollar pathanor por apnar **Order ID / TxID**-ti likhe message din:"
        )
        await message.answer(msg, parse_mode="Markdown", reply_markup=cancel_kb)
        await state.set_state(DepositStates.txid)
    except ValueError:
        await message.answer("❌ Sothik shongkha likhun (jemon: `1` ba `5`).", reply_markup=cancel_kb)

@dp.message(DepositStates.txid, F.text)
async def deposit_txid_received(message: types.Message, state: FSMContext):
    await state.update_data(dep_txid=message.text.strip())
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    await message.answer("📸 **Ekhon apnar payment-er screenshot (Photo) Pathan:**", reply_markup=cancel_kb)
    await state.set_state(DepositStates.screenshot)

@dp.message(DepositStates.screenshot, F.photo)
async def deposit_screenshot_received(message: types.Message, state: FSMContext):
    user = message.from_user
    photo = message.photo[-1]
    data = await state.get_data()
    amount = data.get("dep_amount")
    txid = data.get("dep_txid")

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_dep_{user.id}_{amount}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_dep_{user.id}")
        ]
    ])
    caption = (
        f"📥 **Notun Deposit Request!**\n\n"
        f"👤 **User:** {user.full_name} (`{user.id}`)\n"
        f"💰 **Amount:** `${amount}` USDT\n"
        f"🧾 **TxID:** `{txid}`"
    )
    await bot.send_photo(chat_id=ADMIN_ID, photo=photo.file_id, caption=caption, parse_mode="Markdown", reply_markup=admin_kb)
    await message.answer("✅ **Apnar deposit request admin-er kache pathano hoyeche!** Jaachai kore druto balance jukto kora hobe.")
    await state.clear()

# Admin FSM Handlers
@dp.callback_query(F.data == "admin_ban_start")
async def admin_ban_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    await query.message.answer("🚫 **Banned korte chawa User ID-ti likhe pathan:**")
    await state.set_state(AdminStates.ban)

@dp.message(AdminStates.ban, F.text)
async def admin_ban_process(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": True}})
        await message.answer(f"✅ User `{uid}`-ke banned kora hoyeche.", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Invalid User ID.")
    await state.clear()

@dp.callback_query(F.data == "admin_unban_start")
async def admin_unban_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    await query.message.answer("✅ **Unban korte chawa User ID-ti likhe pathan:**")
    await state.set_state(AdminStates.unban)

@dp.message(AdminStates.unban, F.text)
async def admin_unban_process(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        users_col.update_one({"user_id": uid}, {"$set": {"is_banned": False}})
        await message.answer(f"✅ User `{uid}`-ke unban kora hoyeche.", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Invalid User ID.")
    await state.clear()

@dp.callback_query(F.data == "admin_add_bal_start")
async def admin_add_bal_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    await query.message.answer("➕ **Balance add korte chawa User ID-ti pathan:**")
    await state.set_state(AdminStates.add_bal_user)

@dp.message(AdminStates.add_bal_user, F.text)
async def admin_add_bal_user_msg(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        await state.update_data(target_add_uid=uid)
        await message.answer(f"💰 **User `{uid}`-er jonno koto USDT balance add korben ta likhun:**", parse_mode="Markdown")
        await state.set_state(AdminStates.add_bal_amt)
    except ValueError:
        await message.answer("❌ Invalid User ID.")
        await state.clear()

@dp.message(AdminStates.add_bal_amt, F.text)
async def admin_add_bal_amt_msg(message: types.Message, state: FSMContext):
    try:
        amt = float(message.text.strip())
        data = await state.get_data()
        uid = data.get("target_add_uid")
        users_col.update_one({"user_id": uid}, {"$inc": {"balance": amt}})
        u = get_user(uid)
        new_bal = u.get("balance", 0.0) if u else amt
        await message.answer(f"✅ Successfully added `${amt}` USDT to User `{uid}`. Notun Balance: `${new_bal:.4f}` USDT", parse_mode="Markdown")
        await bot.send_message(chat_id=uid, text=f"🎉 **Admin apnar account-e `${amt}` USDT balance add koreche!**")
    except ValueError:
        await message.answer("❌ Invalid Amount.")
    await state.clear()

@dp.callback_query(F.data == "admin_zero_bal_start")
async def admin_zero_bal_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    await query.message.answer("🔄 **Je user-er balance 0 (zero) korte chan, tar User ID-ti pathan:**", parse_mode="Markdown")
    await state.set_state(AdminStates.zero_bal_user)

@dp.message(AdminStates.zero_bal_user, F.text)
async def admin_zero_bal_process(message: types.Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        result = users_col.update_one({"user_id": uid}, {"$set": {"balance": 0.0}})
        if result.matched_count > 0:
            await message.answer(f"✅ Successfully User `{uid}`-er balance **0 USDT** kora hoyeche.", parse_mode="Markdown")
            try:
                await bot.send_message(chat_id=uid, text="⚠️ **Admin apnar account-er balance 0 kore diyeche.**")
            except Exception:
                pass
        else:
            await message.answer(f"❌ Database-e `{uid}` ID-er kono user pawa jayni.")
    except ValueError:
        await message.answer("❌ Invalid User ID! Sothik shongkha likhun.")
    await state.clear()

# Rate Setters
@dp.callback_query(F.data == "admin_rate_wa_hk_start")
async def rate_wa_hk_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    await query.message.answer("💵 **Hong Kong (HK) WhatsApp (WA)-er notun Bot Rate USDT-te likhun (jemon: `0.075` ba `0.10`):**")
    await state.set_state(AdminStates.rate_wa_hk)

@dp.message(AdminStates.rate_wa_hk, F.text)
async def rate_wa_hk_proc(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text.strip())
        set_rate("wa", "hk", rate)
        await message.answer(f"✅ Hong Kong WA Rate update kora hoyeche: `${rate}` USDT", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Invalid Rate Format!")
    await state.clear()

@dp.callback_query(F.data == "admin_rate_wa_cl_start")
async def rate_wa_cl_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    await query.message.answer("💵 **Chile (CL) WhatsApp (WA)-er notun Bot Rate USDT-te likhun (jemon: `0.087` ba `0.10`):**")
    await state.set_state(AdminStates.rate_wa_cl)

@dp.message(AdminStates.rate_wa_cl, F.text)
async def rate_wa_cl_proc(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text.strip())
        set_rate("wa", "cl", rate)
        await message.answer(f"✅ Chile WA Rate update kora hoyeche: `${rate}` USDT", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Invalid Rate Format!")
    await state.clear()

@dp.callback_query(F.data == "admin_rate_tg_hk_start")
async def rate_tg_hk_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    await query.message.answer("💵 **Hong Kong (HK) Telegram (TG)-er notun Bot Rate USDT-te likhun (jemon: `0.10` ba `0.12`):**")
    await state.set_state(AdminStates.rate_tg_hk)

@dp.message(AdminStates.rate_tg_hk, F.text)
async def rate_tg_hk_proc(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text.strip())
        set_rate("tg", "hk", rate)
        await message.answer(f"✅ Hong Kong TG Rate update kora hoyeche: `${rate}` USDT", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Invalid Rate Format!")
    await state.clear()

@dp.callback_query(F.data == "admin_rate_tg_cl_start")
async def rate_tg_cl_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    await query.message.answer("💵 **Chile (CL) Telegram (TG)-er notun Bot Rate USDT-te likhun (jemon: `0.10` ba `0.12`):**")
    await state.set_state(AdminStates.rate_tg_cl)

@dp.message(AdminStates.rate_tg_cl, F.text)
async def rate_tg_cl_proc(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text.strip())
        set_rate("tg", "cl", rate)
        await message.answer(f"✅ Chile TG Rate update kora hoyeche: `${rate}` USDT", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Invalid Rate Format!")
    await state.clear()

# Broadcast
@dp.callback_query(F.data == "admin_broadcast_start")
async def admin_broadcast_start(query: types.CallbackQuery, state: FSMContext):
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_flow_cb")]])
    await query.message.answer("📢 **Sobai ke broadcast korte chawa message-ti (Text/Photo) ekhane pathan:**", reply_markup=cancel_kb)
    await state.set_state(AdminStates.broadcast)

@dp.message(AdminStates.broadcast)
async def admin_broadcast_process(message: types.Message, state: FSMContext):
    all_users = list(users_col.find())
    success_count = 0
    fail_count = 0
    status_msg = await message.answer(f"⏳ **Broadcast Process Shuru Hoche... Total Users: {len(all_users)}**")

    for u in all_users:
        uid = u.get("user_id")
        if not uid: continue
        try:
            if message.photo:
                await bot.send_photo(chat_id=uid, photo=message.photo[-1].file_id, caption=message.caption or "", caption_entities=message.caption_entities)
            else:
                await bot.send_message(chat_id=uid, text=message.text or "", entities=message.entities)
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail_count += 1

    result_text = (
        f"📢 **Broadcast Shes Huyeche!**\n\n"
        f"✅ **Success:** `{success_count}` Users\n"
        f"❌ **Failed/Blocked:** `{fail_count}` Users"
    )
    await status_msg.edit_text(result_text, parse_mode="Markdown")
    await state.clear()

# Background Tasks & Processors
async def process_otp_success(id_num: str, otp: str):
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

    users_col.update_one({"user_id": uid}, {"$inc": {"balance": -cost, "otp_count": 1}})
    updated_user = get_user(uid)
    rem_bal = updated_user.get("balance", 0.0) if updated_user else 0.0
    set_number_status(id_num, "end")

    success_text = (
        f"✅ **𝙾𝚃𝙿 𝚁𝙴𝙲𝙴𝙸𝚅𝙴 𝚂𝚄𝙲𝙲𝙴𝚂𝚂𝙵𝚄𝚈!**\n\n"
        f"📱 **𝙽𝚄𝙼𝙱𝙴𝚁:** `<code>{phone}</code>`\n"
        f"🔑 **𝙾𝚃𝙿 𝙲𝙾𝙳𝙴:** `<code>{otp}</code>`\n\n"
        f"💵 **𝙱𝙰𝙻𝙰𝙽𝙲𝙴 𝙳𝙴𝙳𝙸𝙲𝙰𝚃𝙴𝙳:** `${cost}` USDT\n"
        f"💰 **𝚁𝙴𝙼𝙰𝙸𝙽𝙸𝙽𝙶 𝙱𝙰𝙻𝙰𝙽𝙲𝙴:** `${rem_bal:.4f}` USDT"
    )

    try:
        await bot.edit_message_text(chat_id=uid, message_id=msg_id, text=success_text, parse_mode="HTML")
    except Exception:
        await bot.send_message(chat_id=uid, text=success_text, parse_mode="HTML")

    masked_phone = mask_number(phone)
    group_forward_msg = (
        f"🌐 **COUNTRY:** `{country_code.upper()}` {country_flag}\n"
        f"📱 **𝙽𝚄𝙼𝙱𝙴𝚁:** `{masked_phone}`\n"
        f"🔑 **𝙾𝚃𝙿:** `{otp}`\n"
        f"💬 **Message:** `YOUR {service_type} CODE: {otp}`"
    )
    if OTP_GROUP_ID:
        try:
            await bot.send_message(chat_id=OTP_GROUP_ID, text=group_forward_msg, parse_mode="Markdown")
            logging.info(f"OTP Forwarded to Group {OTP_GROUP_ID} successfully.")
        except Exception as e:
            logging.error(f"Failed to forward OTP to group: {e}")

async def auto_check_otp(user_id: int, id_num: str, phone_num: str, msg_id: int):
    for _ in range(35):
        await asyncio.sleep(6)
        if id_num not in active_orders:
            break
        res = fetch_otp_code(id_num)
        if isinstance(res, dict) and "smsCode" in res and res["smsCode"]:
            await process_otp_success(id_num, res["smsCode"])
            break

async def setup_webhook():
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{BOT_TOKEN}"
        await bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook set to: {webhook_url}")

# Server Entry Point (Webhook Mode Only)
if __name__ == "__main__":
    if RENDER_EXTERNAL_URL:
        asyncio.run(setup_webhook())
    logging.info("🤖 Starting Flask Webhook Server for Aiogram Bot...")
    run_flask()
