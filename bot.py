#!/usr/bin/env python3
"""
Aadhaar EID/UID Retrieval & PDF Download Telegram Bot
JSON storage edition — Railway ready
"""

# ============================================================
# 🔑 PUT YOUR BOT TOKEN HERE 👇 (or set BOT_TOKEN env var on Railway)
# ============================================================
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
# ============================================================

import os
import sys
import logging
import json
import base64
import io
import time
import uuid
import requests
import pikepdf
import sqlite3  # kept only if any legacy import; unused
import asyncio
import concurrent.futures
from datetime import datetime
from typing import Dict, Any, Optional, Tuple
from threading import Lock
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    JobQueue,
)
from telegram.request import HTTPXRequest
from telegram.constants import ParseMode

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ---------- States ----------
(NAME, MOBILE, EID_CAPTCHA, EID_OTP, DOWNLOAD_CAPTCHA, DOWNLOAD_OTP) = range(6)
(ADMIN_SET_LIMIT, ADMIN_BROADCAST) = range(10, 12)

# ---------- Endpoints ----------
BASE_URL = "https://tathya.uidai.gov.in"
EID_ENDPOINT = f"{BASE_URL}/retrieveEidUid/ext/v1/generic/retrieveuideid"
CAPTCHA_ENDPOINT = f"{BASE_URL}/audioCaptchaService/api/captcha/v3/generation"
DOWNLOAD_OTP_ENDPOINT = f"{BASE_URL}/unifiedAppAuthService/api/v2/generate/aadhaar/otp"
DOWNLOAD_PDF_ENDPOINT = f"{BASE_URL}/downloadAadhaarService/api/aadhaar/download"

# ---------- Config ----------
ADMIN_IDS = [8311745018]
BOT_OWNER = "@arsh3"
DEFAULT_FREE_LIMIT = 5
DEFAULT_FREE_WINDOW_ENABLED = True

PLANS = {
    "basic": {"aadhaars": 10, "price_inr": 130, "price_usdt": 2},
    "standard": {"aadhaars": 20, "price_inr": 250, "price_usdt": 3},
    "premium": {"aadhaars": 50, "price_inr": 550, "price_usdt": 6},
    "enterprise": {"aadhaars": 100, "price_inr": 1000, "price_usdt": 10},
    "ultimate": {"aadhaars": 200, "price_inr": 1800, "price_usdt": 18},
}

BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en_IN",
    "appid": "MYAADHAAR",
    "content-type": "application/json",
    "origin": "https://myaadhaar.uidai.gov.in",
    "referer": "https://myaadhaar.uidai.gov.in/",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
    "connection": "keep-alive",
}

user_sessions: Dict[int, "UserSession"] = {}

# ============================================================
# JSON STORAGE (replaces SQLite)
# ============================================================
DB_FILE = os.getenv("DB_FILE", "/data/bot_data.json")
db_lock = Lock()


def _default_data():
    return {
        "users": {},
        "settings": {
            "free_window_enabled": DEFAULT_FREE_WINDOW_ENABLED,
            "free_limit": DEFAULT_FREE_LIMIT,
            "maintenance_mode": False,
        },
    }


def _empty_user():
    return {
        "credits": 0,
        "total_downloads": 0,
        "last_download_date": "",
        "daily_downloads": 0,
        "joined_date": datetime.now().isoformat(),
        "plan_type": None,
        "is_premium": 0,
    }


def _load():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # backfill missing keys just in case
        data.setdefault("users", {})
        data.setdefault("settings", _default_data()["settings"])
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return _default_data()


def _save(data):
    try:
        os.makedirs(os.path.dirname(DB_FILE) or ".", exist_ok=True)
    except Exception:
        pass
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DB_FILE)


def init_db():
    _save(_load())
    logger.info(f"💾 Storage file: {DB_FILE}")


# ---------- settings ----------
def get_setting(key):
    v = _load()["settings"].get(key)
    return str(v) if v is not None else None


def update_setting(key, value):
    with db_lock:
        d = _load()
        d["settings"][key] = value
        _save(d)


def get_free_window_enabled():
    return bool(_load()["settings"].get("free_window_enabled", True))


def get_free_limit():
    return int(_load()["settings"].get("free_limit", DEFAULT_FREE_LIMIT))


def get_maintenance_mode():
    return bool(_load()["settings"].get("maintenance_mode", False))


# ---------- users ----------
# Returns tuple with these indices (kept compatible with rest of code):
# 0=user_id, 1=credits, 2=total_downloads, 3=last_download_date,
# 4=daily_downloads, 5=joined_date, 6=plan_type, 7=is_premium
def get_user(user_id):
    u = _load()["users"].get(str(user_id))
    if not u:
        return None
    return (
        int(user_id),
        u.get("credits", 0),
        u.get("total_downloads", 0),
        u.get("last_download_date", ""),
        u.get("daily_downloads", 0),
        u.get("joined_date"),
        u.get("plan_type"),
        u.get("is_premium", 0),
    )


def create_user(user_id):
    with db_lock:
        d = _load()
        if str(user_id) not in d["users"]:
            d["users"][str(user_id)] = _empty_user()
            _save(d)


def update_credits(user_id, amount):
    with db_lock:
        d = _load()
        u = d["users"].setdefault(str(user_id), _empty_user())
        u["credits"] = max(0, u.get("credits", 0) + amount)
        _save(d)


def get_credits(user_id):
    u = _load()["users"].get(str(user_id))
    return u.get("credits", 0) if u else 0


def increment_download(user_id):
    with db_lock:
        d = _load()
        today = datetime.now().date().isoformat()
        u = d["users"].setdefault(str(user_id), _empty_user())
        if u.get("last_download_date") != today:
            u["daily_downloads"] = 0
        u["daily_downloads"] = u.get("daily_downloads", 0) + 1
        u["total_downloads"] = u.get("total_downloads", 0) + 1
        u["last_download_date"] = today
        _save(d)
        return u["daily_downloads"]


def can_download(user_id):
    if get_maintenance_mode():
        return False, "Bot is under maintenance. Please try again later."

    user = get_user(user_id)
    if not user:
        create_user(user_id)
        if get_free_window_enabled():
            return True, "Free window enabled"
        return False, "Free window disabled. Purchase credits to continue."

    credits = user[1]
    if credits > 0:
        return True, f"Premium credits available: {credits}"

    if not get_free_window_enabled():
        return False, "Free window disabled. Purchase credits to continue."

    free_limit = get_free_limit()
    today = datetime.now().date().isoformat()
    daily = user[4] if user[4] else 0
    last_date = user[3] if user[3] else ""

    if last_date != today:
        return True, "New day, free downloads reset"
    if daily < free_limit:
        return True, f"Free downloads remaining: {free_limit - daily}"
    return False, f"Daily free limit reached ({free_limit}/{free_limit}). Purchase credits to continue."


# ============================================================
# SESSION / HELPERS
# ============================================================
class UserSession:
    def __init__(self):
        self.name = None
        self.mobile = None
        self.eid_number = None
        self.eid_captcha_txn_id = None
        self.eid_otp_txn_id = None
        self.eid_captcha_text = None
        self.download_captcha_txn_id = None
        self.download_otp_txn_id = None
        self.download_captcha_text = None
        self.request_id = None
        self.session = requests.Session()
        self.created_at = time.time()
        self.otp_attempts = 0
        self.max_otp_attempts = 3
        self.full_name = None


def generate_captcha(session):
    try:
        payload = {"captchaLength": "6", "captchaType": "2", "audioCaptchaRequired": True}
        r = session.post(CAPTCHA_ENDPOINT, headers=BASE_HEADERS, json=payload, timeout=15)
        result = r.json()
        if result.get("imageBase64") and result.get("transactionId"):
            return base64.b64decode(result["imageBase64"]), result["transactionId"], None
        return None, None, {"error": "No image in response"}
    except Exception as e:
        return None, None, {"error": str(e)}


def api_call(session, url, payload, label, custom_headers=None):
    try:
        headers = custom_headers if custom_headers else BASE_HEADERS
        logger.info(f"{label}: {json.dumps(payload)}")
        r = session.post(url, headers=headers, json=payload, timeout=15)
        return r.json(), None
    except Exception as e:
        return None, {"error": str(e)}


def crack_pdf(pdf_bytes, name):
    if name:
        name_clean = " ".join(name.split()).upper()
        name_prefix = name_clean[:4] if len(name_clean) >= 4 else name_clean.ljust(4, "X")
    else:
        name_prefix = "MR"

    try:
        try:
            pdf = pikepdf.open(io.BytesIO(pdf_bytes))
            out = io.BytesIO()
            pdf.save(out)
            pdf.close()
            return out.getvalue(), None
        except Exception:
            pass

        for year in range(1950, 2016):
            password = f"{name_prefix}{year}"
            try:
                pdf = pikepdf.open(
                    io.BytesIO(pdf_bytes), password=password, allow_overwriting_input=True
                )
                out = io.BytesIO()
                pdf.save(out)
                pdf.close()
                return out.getvalue(), password
            except pikepdf.PasswordError:
                continue
            except Exception:
                continue
        return None, None
    except Exception:
        return None, None


# ============================================================
# MENU
# ============================================================
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("📥 Download Aadhaar", callback_data="start_download")],
        [InlineKeyboardButton("💰 View Plans", callback_data="view_plans")],
        [InlineKeyboardButton("👤 My Credits", callback_data="check_credits")],
        [InlineKeyboardButton("❓ Help", callback_data="help_menu")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "🏠 Main Menu\n\n"
        "Welcome to Aadhaar Download Bot!\n"
        "Select an option below:\n\n"
        f"🤖 Bot by {BOT_OWNER}"
    )

    try:
        if edit and hasattr(update, "callback_query") and update.callback_query:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Error showing menu: {e}")


# ============================================================
# /start
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    if get_maintenance_mode():
        await update.message.reply_text(
            "⛔ Bot Under Maintenance\n\n"
            "The bot is currently being updated.\n"
            "Please try again later.\n\n"
            f"Contact: {BOT_OWNER}"
        )
        return ConversationHandler.END

    can_dl, msg = can_download(user_id)
    if not can_dl:
        credits = get_credits(user_id)
        keyboard = [
            [InlineKeyboardButton("💰 View Premium Plans", callback_data="view_plans")],
            [InlineKeyboardButton("👤 Check Credits", callback_data="check_credits")],
        ]
        await update.message.reply_text(
            f"❌ Access Restricted\n\n{msg}\nYour Credits: {credits}\n\n"
            f"Purchase premium plans to continue!\nContact: {BOT_OWNER}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END

    if user_id in user_sessions:
        del user_sessions[user_id]
    user_sessions[user_id] = UserSession()
    create_user(user_id)

    await show_main_menu(update, context)
    return ConversationHandler.END


# ============================================================
# DOWNLOAD FLOW
# ============================================================
async def start_download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    can_dl, msg = can_download(user_id)
    if not can_dl:
        credits = get_credits(user_id)
        keyboard = [
            [InlineKeyboardButton("💰 View Premium Plans", callback_data="view_plans")],
            [InlineKeyboardButton("👤 Check Credits", callback_data="check_credits")],
        ]
        await query.edit_message_text(
            f"❌ Access Restricted\n\n{msg}\nYour Credits: {credits}\n\n"
            f"Purchase premium plans to continue!\nContact: {BOT_OWNER}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return ConversationHandler.END

    if user_id in user_sessions:
        del user_sessions[user_id]
    user_sessions[user_id] = UserSession()
    create_user(user_id)

    await query.edit_message_text(
        "🔐 Aadhaar Download Bot\n\n"
        "📝 Enter your Full Name as in Aadhaar\n"
        "_Type Mr to skip_"
    )
    return NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us:
        await update.message.reply_text("⏰ Session expired. /start again.")
        return ConversationHandler.END
    name = update.message.text.strip()
    us.name = name if name else "Mr"
    await update.message.reply_text(
        f"✅ Name: {us.name}\n\n📱 Enter your 10-digit Mobile Number"
    )
    return MOBILE


async def get_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us:
        await update.message.reply_text("⏰ Session expired. /start again.")
        return ConversationHandler.END
    mobile = update.message.text.strip()
    if not mobile.isdigit() or len(mobile) != 10:
        await update.message.reply_text("❌ Enter exactly 10 digits.")
        return MOBILE
    us.mobile = mobile

    progress = await update.message.reply_text("🔄 Generating captcha...")
    image_bytes, txn_id, _ = generate_captcha(us.session)
    if not image_bytes:
        await progress.delete()
        await update.message.reply_text("❌ Failed to generate captcha. /start to retry")
        return ConversationHandler.END
    us.eid_captcha_txn_id = txn_id
    img = io.BytesIO(image_bytes)
    await progress.delete()
    await update.message.reply_photo(photo=img, caption="📸 Enter the captcha text")
    return EID_CAPTCHA


async def get_eid_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us:
        await update.message.reply_text("⏰ Session expired. /start again.")
        return ConversationHandler.END
    captcha_text = update.message.text.strip()
    us.eid_captcha_text = captcha_text

    progress = await update.message.reply_text("📤 Requesting OTP...")
    otp_payload = {
        "mobileNumber": us.mobile, "dob": None, "email": None, "name": us.name,
        "option": "EID", "otp": None, "otpTxnId": None,
        "captchaTxnId": us.eid_captcha_txn_id, "captcha": captcha_text,
        "resendOtp": False,
    }
    otp_result, _ = api_call(us.session, EID_ENDPOINT, otp_payload, "EID OTP REQUEST")
    await progress.delete()

    if not otp_result or otp_result.get("status") != "Success" or not otp_result.get("responseData", {}).get("otpSent"):
        err = otp_result.get("responseData", {}).get("message", "Failed to send OTP") if otp_result else "Unknown error"
        await update.message.reply_text(f"❌ OTP Failed: {err}\n/start to retry")
        return ConversationHandler.END

    us.eid_otp_txn_id = otp_result["responseData"]["otpTxnId"]
    masked = f"{us.mobile[:2]}****{us.mobile[-4:]}"
    await update.message.reply_text(
        f"✅ OTP Sent to {masked}\n\n📝 Enter the 6-digit OTP"
    )
    return EID_OTP


async def get_eid_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us:
        await update.message.reply_text("⏰ Session expired. /start again.")
        return ConversationHandler.END
    otp = update.message.text.strip()
    if not otp.isdigit() or len(otp) != 6:
        await update.message.reply_text("❌ OTP must be exactly 6 digits.")
        return EID_OTP

    us.otp_attempts += 1
    if us.otp_attempts > us.max_otp_attempts:
        await update.message.reply_text("❌ Too many attempts. /start to retry.")
        return ConversationHandler.END

    progress = await update.message.reply_text("🔍 Verifying OTP...")
    verify_payload = {
        "mobileNumber": us.mobile, "dob": None, "email": None, "name": us.name,
        "option": "EID", "otp": otp, "otpTxnId": us.eid_otp_txn_id,
        "captchaTxnId": us.eid_captcha_txn_id, "captcha": us.eid_captcha_text,
        "resendOtp": False,
    }
    verify_result, _ = api_call(us.session, EID_ENDPOINT, verify_payload, "EID OTP VERIFY")
    await progress.delete()

    if not verify_result or verify_result.get("status") != "Success":
        err = verify_result.get("responseData", {}).get("message", "Invalid OTP") if verify_result else "Unknown error"
        remaining = us.max_otp_attempts - us.otp_attempts
        await update.message.reply_text(f"❌ OTP Failed: {err}\nAttempts left: {remaining}")
        return EID_OTP if remaining > 0 else ConversationHandler.END

    response_data = verify_result.get("responseData", {})
    if not response_data.get("eidNumber"):
        await update.message.reply_text("❌ EID not found. /start to retry")
        return ConversationHandler.END

    us.eid_number = response_data["eidNumber"]
    us.full_name = response_data.get("name", us.name)
    us.request_id = str(uuid.uuid4())

    await update.message.reply_text(
        f"✅ EID Retrieved!\n👤 {us.full_name}\n🆔 {us.eid_number}\n\n"
        f"🔄 Generating download captcha..."
    )

    dl_image_bytes, dl_txn_id, _ = generate_captcha(us.session)
    if not dl_image_bytes:
        await update.message.reply_text("❌ Failed to generate captcha. /start to retry")
        return ConversationHandler.END
    us.download_captcha_txn_id = dl_txn_id
    await update.message.reply_photo(
        photo=io.BytesIO(dl_image_bytes), caption="📸 Enter the download captcha"
    )
    return DOWNLOAD_CAPTCHA


async def get_download_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us:
        await update.message.reply_text("⏰ Session expired. /start again.")
        return ConversationHandler.END
    captcha_text = update.message.text.strip()
    us.download_captcha_text = captcha_text

    progress = await update.message.reply_text("📤 Requesting download OTP...")
    payload = {
        "eidNumber": us.eid_number, "idType": "eid",
        "captchaTxnId": us.download_captcha_txn_id, "captchaValue": captcha_text,
        "transactionId": us.request_id, "resendOTP": False,
    }
    result, _ = api_call(us.session, DOWNLOAD_OTP_ENDPOINT, payload, "DOWNLOAD OTP REQUEST")
    await progress.delete()

    if not result or result.get("status") != "Success":
        err = result.get("message", "Failed") if result else "Unknown error"
        await update.message.reply_text(f"❌ Failed: {err}\n/start to retry")
        return ConversationHandler.END

    txn_id = result.get("txnId") or result.get("responseData", {}).get("otpTxnId")
    us.download_otp_txn_id = txn_id
    us.otp_attempts = 0
    await update.message.reply_text(
        "✅ Download OTP Sent!\n\n📝 Enter the Download OTP"
    )
    return DOWNLOAD_OTP


async def get_download_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    us = user_sessions.get(user_id)
    if not us:
        await update.message.reply_text("⏰ Session expired. /start again.")
        return ConversationHandler.END
    otp = update.message.text.strip()
    if not otp.isdigit() or len(otp) != 6:
        await update.message.reply_text("❌ OTP must be exactly 6 digits.")
        return DOWNLOAD_OTP

    progress = await update.message.reply_text("📥 Downloading PDF...")
    pdf_payload = {
        "eid": us.eid_number, "mask": False, "otp": otp,
        "otpTxnId": us.download_otp_txn_id,
    }
    txid = str(uuid.uuid4())
    headers = BASE_HEADERS.copy()
    headers["transactionid"] = txid
    headers["x-request-id"] = txid

    try:
        r = us.session.post(DOWNLOAD_PDF_ENDPOINT, headers=headers, json=pdf_payload, timeout=30)
        await progress.delete()
        rj = r.json()

        if rj.get("status") == "Success" and rj.get("statusCode") == 200:
            pdf_data = rj.get("data", {}).get("aadhaarPdf") or rj.get("aadhaarPdf")
            if not pdf_data:
                await update.message.reply_text("❌ No PDF data in response")
                return ConversationHandler.END
            pdf_bytes = base64.b64decode(pdf_data)
            if pdf_bytes[:4] != b"%PDF":
                await update.message.reply_text("❌ Invalid PDF data received")
                return ConversationHandler.END

            await update.message.reply_text("🔓 Attempting to crack PDF...")
            with concurrent.futures.ThreadPoolExecutor() as ex:
                fut = ex.submit(crack_pdf, pdf_bytes, us.full_name)
                try:
                    unlocked_pdf, password = fut.result(timeout=60)
                except concurrent.futures.TimeoutError:
                    await update.message.reply_text("⏰ PDF cracking timed out. Sending locked PDF.")
                    unlocked_pdf, password = None, None

            credits = get_credits(user_id)
            if credits > 0:
                update_credits(user_id, -1)
                credit_msg = f"Credits Remaining: {credits - 1}"
            else:
                increment_download(user_id)
                free_limit = get_free_limit()
                u = get_user(user_id)
                daily = u[4] if u else 0
                credit_msg = f"Free Downloads: {daily}/{free_limit}"

            if unlocked_pdf:
                pdf_to_send = unlocked_pdf
                caption_text = f"🔓 Unlocked\n🔑 Password: {password}" if password else "🔓 Unlocked"
            else:
                pdf_to_send = pdf_bytes
                hint = us.full_name[:4].upper() if us.full_name else "MR"
                caption_text = f"🔒 Password Protected\nHint: {hint}YYYY"

            doc = io.BytesIO(pdf_to_send)
            doc.name = f"aadhaar_{us.eid_number[:8]}.pdf"
            await update.message.reply_document(
                document=doc,
                caption=(
                    f"📄 Aadhaar PDF\n👤 {us.full_name}\n🆔 {us.eid_number}\n"
                    f"{caption_text}\n{credit_msg}\n\n🤖 Bot by {BOT_OWNER}"
                ),
            )
            await update.message.reply_text(
                f"✅ Download Complete!\n\n{credit_msg}\n\n🤖 Bot by {BOT_OWNER}"
            )
            await show_main_menu(update, context)
        else:
            await update.message.reply_text(
                f"❌ Download Failed: {rj.get('statusMessage', 'Unknown error')}"
            )
    except Exception as e:
        await progress.delete()
        await update.message.reply_text(f"❌ Error: {str(e)}")

    user_sessions.pop(user_id, None)
    return ConversationHandler.END


# ============================================================
# CANCEL
# ============================================================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Cancelled. /start to begin again.")
    return ConversationHandler.END


# ============================================================
# CALLBACKS
# ============================================================
async def view_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text = "💰 Premium Plans\n\n"
    for k, p in PLANS.items():
        text += f"{k.title()} Plan\n📊 {p['aadhaars']} Aadhaars\n💵 ₹{p['price_inr']} / {p['price_usdt']} USDT\n\n"
    text += f"\n📩 Contact {BOT_OWNER} to purchase\n💳 Check /credits to see your balance"
    kb = [
        [InlineKeyboardButton("👤 Check Credits", callback_data="check_credits")],
        [InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def check_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        user_id = q.from_user.id
        is_cb = True
    else:
        user_id = update.effective_user.id
        is_cb = False

    credits = get_credits(user_id)
    user = get_user(user_id)
    daily = user[4] if user else 0
    last_date = user[3] if user else ""
    today = datetime.now().date().isoformat()
    if last_date != today:
        daily = 0

    free_limit = get_free_limit()
    remaining = max(0, free_limit - daily)
    free_enabled = get_free_window_enabled()

    text = (
        f"👤 Your Credits\n\n"
        f"💳 Premium Credits: {credits}\n"
        f"📊 Free Downloads Today: {daily}/{free_limit}\n"
        f"⏳ Remaining Today: {remaining}\n"
        f"🔓 Free Window: {'✅ Enabled' if free_enabled else '❌ Disabled'}\n\n"
        f"💰 Buy more credits: /plans"
    )
    kb = [
        [InlineKeyboardButton("💰 View Plans", callback_data="view_plans")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ]
    rm = InlineKeyboardMarkup(kb)
    if is_cb:
        await q.edit_message_text(text, reply_markup=rm)
    else:
        await update.message.reply_text(text, reply_markup=rm)


async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await show_main_menu(update, context, edit=True)


async def help_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    text = (
        "❓ Help & Support\n\n"
        "How to use:\n"
        "1️⃣ /start to begin\n"
        "2️⃣ Enter name, mobile, captcha\n"
        "3️⃣ Enter OTPs when received\n"
        "4️⃣ Get your Aadhaar PDF\n\n"
        "Plans:\n"
    )
    for k, p in PLANS.items():
        text += f"• {k.title()}: {p['aadhaars']} Aadhaars - ₹{p['price_inr']}\n"
    text += f"\n📩 Contact: {BOT_OWNER}"
    kb = [[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))


# ============================================================
# ADMIN PANEL
# ============================================================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Unauthorized access!")
        return
    fe, fl, mm = get_free_window_enabled(), get_free_limit(), get_maintenance_mode()
    text = (
        "⚙️ Admin Panel\n\n"
        f"📊 Free Window: {'✅ Enabled' if fe else '❌ Disabled'}\n"
        f"📊 Free Limit: {fl} per day\n"
        f"🔧 Maintenance: {'⛔ Enabled' if mm else '✅ Disabled'}\n\n"
        "Select an option below:"
    )
    kb = [
        [InlineKeyboardButton(f"{'🔴' if fe else '🟢'} Toggle Free Window", callback_data="admin_toggle_free")],
        [InlineKeyboardButton("📊 Set Free Limit", callback_data="admin_set_limit")],
        [InlineKeyboardButton(f"{'🟢' if mm else '🔴'} Toggle Maintenance", callback_data="admin_toggle_maintenance")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📊 Bot Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 List Users", callback_data="admin_list_users")],
        [InlineKeyboardButton("🏠 Back to Menu", callback_data="main_menu")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def admin_toggle_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Unauthorized!")
        return
    new = not get_free_window_enabled()
    update_setting("free_window_enabled", new)
    await q.edit_message_text(
        f"✅ Free window has been {'Enabled' if new else 'Disabled'}!\n\n"
        f"Free Limit: {get_free_limit()} per day\n\nClick /admin to return to admin panel"
    )


async def admin_set_limit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Unauthorized!")
        return
    await q.edit_message_text(
        "📊 Set Free Limit\n\n"
        "Enter the new daily free limit:\nExample: 10\n\n_Send /cancel to cancel_"
    )
    return ADMIN_SET_LIMIT


async def admin_set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized!")
        return ConversationHandler.END
    try:
        n = int(update.message.text.strip())
        if n < 1:
            await update.message.reply_text("❌ Limit must be at least 1")
            return ADMIN_SET_LIMIT
        update_setting("free_limit", n)
        await update.message.reply_text(
            f"✅ Free limit updated to {n} per day!\n\nType /admin to return to admin panel"
        )
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Please enter a valid number")
        return ADMIN_SET_LIMIT


async def admin_toggle_maintenance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Unauthorized!")
        return
    new = not get_maintenance_mode()
    update_setting("maintenance_mode", new)
    await q.edit_message_text(
        f"✅ Maintenance mode has been {'Enabled' if new else 'Disabled'}!\n\n"
        f"Users will {'not be able' if new else 'be able'} to use the bot.\n\n"
        f"Click /admin to return to admin panel"
    )


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Unauthorized!")
        return
    await q.edit_message_text(
        "📢 Broadcast Message\n\nSend the message to broadcast to all users:\n\n_Send /cancel to cancel_"
    )
    return ADMIN_BROADCAST


async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized!")
        return ConversationHandler.END
    message = update.message.text.strip()
    status = await update.message.reply_text("📤 Broadcasting...")
    users = list(_load()["users"].keys())
    ok = fail = 0
    for uid in users:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"📢 Announcement\n\n{message}\n\n🤖 Bot by {BOT_OWNER}",
            )
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await status.edit_text(
        f"✅ Broadcast Complete\n\n✓ Sent: {ok}\n✗ Failed: {fail}\n\nType /admin to return to admin panel"
    )
    return ConversationHandler.END


async def admin_stats_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Unauthorized!")
        return
    d = _load()
    tu = len(d["users"])
    tc = sum(u.get("credits", 0) for u in d["users"].values())
    td = sum(u.get("total_downloads", 0) for u in d["users"].values())
    pu = sum(1 for u in d["users"].values() if u.get("credits", 0) > 0)
    text = (
        f"📊 Bot Statistics\n\n"
        f"👥 Total Users: {tu}\n⭐ Premium Users: {pu}\n"
        f"💳 Total Credits: {tc}\n📊 Total Downloads: {td}\n"
        f"🔓 Free Window: {'✅' if get_free_window_enabled() else '❌'}\n"
        f"📊 Free Limit: {get_free_limit()}/day\n"
        f"🔧 Maintenance: {'⛔' if get_maintenance_mode() else '✅'}\n\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    kb = [
        [InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats")],
        [InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def admin_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Unauthorized!")
        return
    d = _load()
    users = sorted(
        ((int(uid), u.get("credits", 0), u.get("total_downloads", 0))
         for uid, u in d["users"].items()),
        key=lambda x: x[2], reverse=True
    )[:50]
    if not users:
        await q.edit_message_text("No users found.")
        return
    text = "👥 Top Users\n\n"
    for i, (uid, cr, dl) in enumerate(users, 1):
        text += f"{i}. User: {uid}\n   💳 Credits: {cr}, 📊 Downloads: {dl}\n\n"
    kb = [[InlineKeyboardButton("🏠 Admin Panel", callback_data="admin_panel")]]
    await q.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup(kb))


# ============================================================
# ADMIN COMMANDS
# ============================================================
async def admin_add_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Usage: /add <amount> <user_id>"); return
        amt, uid = int(args[0]), int(args[1])
        update_credits(uid, amt)
        await update.message.reply_text(
            f"✅ Added {amt} credits to {uid}\nNew balance: {get_credits(uid)}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def admin_deduct_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Usage: /deduct <amount> <user_id>"); return
        amt, uid = int(args[0]), int(args[1])
        cur = get_credits(uid)
        if cur < amt:
            await update.message.reply_text(f"❌ User only has {cur} credits"); return
        update_credits(uid, -amt)
        await update.message.reply_text(
            f"✅ Deducted {amt} from {uid}\nNew balance: {get_credits(uid)}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def admin_check_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    try:
        args = context.args
        if len(args) < 1:
            await update.message.reply_text("Usage: /check <user_id>"); return
        uid = int(args[0])
        u = get_user(uid)
        if not u:
            await update.message.reply_text("User not found"); return
        fl = get_free_limit()
        text = (
            f"👤 User Report\n\nUser ID: {uid}\nCredits: {u[1]}\n"
            f"Total Downloads: {u[2]}\nDaily: {u[4] or 0}/{fl}\n"
            f"Joined: {u[5] or 'N/A'}\nPlan: {u[6] or 'Free'}"
        )
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    fe, fl, mm = get_free_window_enabled(), get_free_limit(), get_maintenance_mode()
    text = (
        "⚙️ Admin Panel\n\n"
        f"📊 Free Window: {'✅' if fe else '❌'}\n"
        f"📊 Free Limit: {fl}/day\n"
        f"🔧 Maintenance: {'⛔' if mm else '✅'}\n\n"
        "Commands:\n"
        "/admin /toggle_free /set_limit <n> /toggle_maintenance\n"
        "/broadcast <msg> /add <amt> <uid> /deduct <amt> <uid>\n"
        "/check <uid> /stats"
    )
    kb = [[InlineKeyboardButton("📊 Admin Panel", callback_data="admin_panel")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def admin_toggle_free_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    new = not get_free_window_enabled()
    update_setting("free_window_enabled", new)
    await update.message.reply_text(
        f"✅ Free window {'Enabled' if new else 'Disabled'}!\nLimit: {get_free_limit()}/day"
    )


async def admin_set_limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    try:
        args = context.args
        if len(args) < 1:
            await update.message.reply_text("Usage: /set_limit <number>"); return
        n = int(args[0])
        if n < 1:
            await update.message.reply_text("❌ Limit must be at least 1"); return
        update_setting("free_limit", n)
        await update.message.reply_text(f"✅ Free limit updated to {n}/day")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def admin_toggle_maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    new = not get_maintenance_mode()
    update_setting("maintenance_mode", new)
    await update.message.reply_text(
        f"✅ Maintenance {'Enabled' if new else 'Disabled'}!"
    )


async def admin_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Usage: /broadcast <message>"); return
    message = " ".join(args)
    status = await update.message.reply_text("📤 Broadcasting...")
    users = list(_load()["users"].keys())
    ok = fail = 0
    for uid in users:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"📢 Announcement\n\n{message}\n\n🤖 Bot by {BOT_OWNER}",
            )
            ok += 1
            await asyncio.sleep(0.05)
        except Exception:
            fail += 1
    await status.edit_text(f"✅ Broadcast Complete\n✓ {ok}  ✗ {fail}")


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Unauthorized"); return
    d = _load()
    tu = len(d["users"])
    tc = sum(u.get("credits", 0) for u in d["users"].values())
    td = sum(u.get("total_downloads", 0) for u in d["users"].values())
    pu = sum(1 for u in d["users"].values() if u.get("credits", 0) > 0)
    text = (
        f"📊 Bot Statistics\n\n👥 Users: {tu}\n⭐ Premium: {pu}\n"
        f"💳 Credits: {tc}\n📊 Downloads: {td}\n"
        f"🔓 Free Window: {'✅' if get_free_window_enabled() else '❌'}\n"
        f"📊 Free Limit: {get_free_limit()}/day\n"
        f"🔧 Maintenance: {'⛔' if get_maintenance_mode() else '✅'}\n\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    await update.message.reply_text(text)


# ============================================================
# ROUTER
# ============================================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    route = {
        "view_plans": view_plans,
        "check_credits": check_credits,
        "main_menu": main_menu_callback,
        "start_download": start_download_callback,
        "help_menu": help_menu,
        "admin_panel": admin_panel,
        "admin_toggle_free": admin_toggle_free,
        "admin_set_limit": admin_set_limit_start,
        "admin_toggle_maintenance": admin_toggle_maintenance,
        "admin_broadcast": admin_broadcast_start,
        "admin_stats": admin_stats_panel,
        "admin_list_users": admin_list_users,
    }
    fn = route.get(data)
    if fn:
        await fn(update, context)


# ============================================================
# /plans COMMAND
# ============================================================
async def view_plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "💰 Premium Plans\n\n"
    for k, p in PLANS.items():
        text += f"{k.title()} Plan\n📊 {p['aadhaars']} Aadhaars\n💵 ₹{p['price_inr']} / {p['price_usdt']} USDT\n\n"
    text += f"\n📩 Contact {BOT_OWNER} to purchase\n💳 Check /credits to see your balance"
    kb = [
        [InlineKeyboardButton("👤 Check Credits", callback_data="check_credits")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


# ============================================================
# APP
# ============================================================
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Start the bot and show menu"),
        BotCommand("credits", "Check your credits and usage"),
        BotCommand("plans", "View premium plans"),
        BotCommand("admin", "Admin panel (admins only)"),
        BotCommand("cancel", "Cancel current operation"),
    ])
    logger.info("✅ Bot commands set!")


def create_application() -> Application:
    token = os.getenv("BOT_TOKEN") or BOT_TOKEN
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ BOT_TOKEN not set. Put it at top of file or set BOT_TOKEN env var.")
        sys.exit(1)

    proxy = os.getenv("TELEGRAM_PROXY")
    if proxy:
        request = HTTPXRequest(proxy_url=proxy, connect_timeout=30.0, read_timeout=60.0, write_timeout=30.0)
    else:
        request = HTTPXRequest(connect_timeout=30.0, read_timeout=60.0, write_timeout=30.0)

    app = (
        Application.builder()
        .token(token)
        .request(request)
        .job_queue(JobQueue())
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(start_download_callback, pattern="^start_download$"),
        ],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_name)],
            MOBILE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_mobile)],
            EID_CAPTCHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_eid_captcha)],
            EID_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_eid_otp)],
            DOWNLOAD_CAPTCHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_download_captcha)],
            DOWNLOAD_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_download_otp)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,
        conversation_timeout=600,
    )

    admin_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast$"),
            CommandHandler("broadcast", admin_broadcast_command),
        ],
        states={ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast)]},
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("admin", admin_command)],
        allow_reentry=True,
    )

    admin_limit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_set_limit_start, pattern="^admin_set_limit$")],
        states={ADMIN_SET_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_limit)]},
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("admin", admin_command)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(admin_conv)
    app.add_handler(admin_limit_conv)

    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("toggle_free", admin_toggle_free_command))
    app.add_handler(CommandHandler("set_limit", admin_set_limit_command))
    app.add_handler(CommandHandler("toggle_maintenance", admin_toggle_maintenance_command))
    app.add_handler(CommandHandler("broadcast", admin_broadcast_command))
    app.add_handler(CommandHandler("add", admin_add_credits))
    app.add_handler(CommandHandler("deduct", admin_deduct_credits))
    app.add_handler(CommandHandler("check", admin_check_user))
    app.add_handler(CommandHandler("stats", admin_stats_command))
    app.add_handler(CommandHandler("credits", check_credits))
    app.add_handler(CommandHandler("plans", view_plans_command))
    app.add_handler(CallbackQueryHandler(button_handler))

    return app


def main() -> None:
    init_db()
    logger.info("🤖 Starting Aadhaar EID Bot...")
    try:
        app = create_application()
        logger.info("✅ Bot running!")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            close_loop=False,
        )
    except KeyboardInterrupt:
        logger.info("👋 Stopped")
    except Exception as e:
        logger.error(f"❌ Fatal: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
