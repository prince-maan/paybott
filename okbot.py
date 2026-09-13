import io
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, Response
from functools import wraps
from PIL import Image, ImageDraw
import pymongo
import qrcode
import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
)

# ==========================================
# 🛑 ENVIRONMENT VARIABLES (रेंडर सेटिंग्स)
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB_NAME = os.environ.get("MONGO_DB_NAME", "telegram_store_bot")

UPI_ID = os.environ.get("UPI_ID")
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Store")
SMS_HOOK_SECRET = os.environ.get("SMS_HOOK_SECRET")

CHAT_LINK = os.environ.get("CHAT_LINK")
INTERNATIONAL_LINK = os.environ.get("INTERNATIONAL_LINK")

PROTECT_CONTENT = os.environ.get("PROTECT_CONTENT", "False").strip().lower() == "true"
QR_EXPIRY_SECONDS = int(os.environ.get("QR_EXPIRY_MINUTES", "10")) * 60
INACTIVITY_CLEANUP_SECONDS = 86400

# 7 घंटे बाद एक्सपायर्ड SMS अपने आप डिलीट हो जाएगा
SMS_POOL_TTL_HOURS = 7  
ORDER_TTL_HOURS = 48

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme123")
BOT_USERNAME = "your_bot"

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID"))
    DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID"))
except:
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_time():
    return datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p")

# ==========================================
# 🍃 MONGODB SETUP (7 Hours TTL)
# ==========================================
try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    db = mongo_client.get_database(MONGO_DB_NAME)
    users_col = db["users"]
    courses_col = db["courses"]
    purchases_col = db["purchases"]
    settings_col = db["settings"]
    orders_col = db["orders"]
    sms_pool_col = db["sms_pool"]
    offers_col = db["offers"]
    channel_logs_col = db["channel_logs"]

    # 7 घंटे का ऑटो-डिलीट टाइमर
    try:
        sms_pool_col.create_index("created_at_dt", expireAfterSeconds=SMS_POOL_TTL_HOURS * 3600)
        orders_col.create_index("created_at_dt", expireAfterSeconds=ORDER_TTL_HOURS * 3600)
        orders_col.create_index("txn_id")
    except: pass
except Exception as e:
    sys.exit(1)

# ==========================================
# 📝 STATE & UNIQUE AMOUNT GENERATOR
# ==========================================
pending_orders, all_orders_cache = {}, {}
user_qr_messages, user_states, user_cooldowns = {}, {}, {}
pending_lock = threading.Lock()
rolling_counter = 1  # 1 से 99 पैसे का चक्र

def generate_unique_amount(base_amount):
    global rolling_counter
    base_clean = int(round(float(base_amount)))
    with pending_lock:
        for _ in range(99):
            paise = rolling_counter
            rolling_counter = 1 if rolling_counter >= 99 else rolling_counter + 1
            candidate = f"{base_clean + (paise / 100):.2f}"
            if candidate not in pending_orders: return candidate
        return f"{base_clean + (random.randint(1, 99) / 100):.2f}"

def extract_txn_id(text):
    m = re.search(r"txn\s+([A-Za-z0-9]+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None

def generate_upi_qr(amount, order_id):
    clean_amt = re.sub(r"[^\d.]", "", str(amount))
    upi_url = f"upi://pay?pa={UPI_ID}&pn={MERCHANT_NAME}&am={clean_amt}&cu=INR&tn=Order_{order_id}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=2)
    qr.add_data(upi_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    bio = io.BytesIO()
    qr_img.save(bio, "PNG")
    bio.seek(0)
    return bio, clean_amt

def update_channel_order_status(order, status_type, extra_text=""):
    channel_msg_id = order.get("channel_msg_id")
    if not channel_msg_id: return
    u_men = order.get("user_mention", f"User")
    if status_type == "EXPIRED":
        new_text = f"🔴 <b>[QR EXPIRED / UNPAID]</b>\n👤 {u_men}\n🔖 Order: <code>{order['order_id']}</code>\n💰 ₹{order['amount']}"
    elif status_type == "AUTO_VERIFIED":
        new_text = f"🟢 <b>[AUTO-DELIVERED]</b>\n👤 {u_men}\n🔖 Order: <code>{order['order_id']}</code>\n💰 ₹{order['amount']}\n📩 <code>{extra_text[:100]}</code>"
    elif status_type == "MANUAL_APPROVED":
        new_text = f"✅ <b>[MANUAL-APPROVED]</b>\n👤 {u_men}\n🔖 Order: <code>{order['order_id']}</code>\n💰 ₹{order['amount']}"
    else: return
    try: bot.edit_message_text(new_text, chat_id=DB_CHANNEL_ID, message_id=channel_msg_id, parse_mode="HTML")
    except: pass

def expire_qr(chat_id, message_id, amount_key, order_id):
    order = all_orders_cache.get(order_id) or orders_col.find_one({"order_id": order_id})
    if not order or order.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"): return

    orders_col.update_one({"order_id": order_id}, {"$set": {"status": "EXPIRED"}})
    order["status"] = "EXPIRED"
    update_channel_order_status(order, "EXPIRED")

    with pending_lock: pending_orders.pop(amount_key, None)
    
    try: bot.delete_message(chat_id, message_id)
    except: pass

    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Verify Payment", callback_data=f"paydone_{order_id}"))
    try: bot.send_message(chat_id, "⏳ <b>क्यूआर कोड का समय समाप्त!</b>\n\nअगर आपने पेमेंट कर दिया है, तो नीचे <b>'✅ Verify Payment'</b> दबाएं।", reply_markup=markup, parse_mode="HTML")
    except: pass

def deliver_course_to_buyer(order, sms_text=None, is_manual=False, txn_id=None):
    order_id, chat_id, user_id = order["order_id"], order["chat_id"], order["user_id"]
    course = courses_col.find_one({"course_id": order["course_id"]})
    new_status = "COMPLETED_MANUAL" if is_manual else "COMPLETED_AUTO"
    
    updates = {"status": new_status, "delivered_at": get_ist_time()}
    if txn_id: updates["txn_id"] = txn_id
    res = orders_col.update_one({"order_id": order_id, "status": {"$in": ["PENDING", "EXPIRED"]}}, {"$set": updates})
    if res.modified_count == 0: return

    with pending_lock: pending_orders.pop(order.get("amount"), None)
    
    qr_msg_id = order.get("qr_msg_id")
    if qr_msg_id:
        try: bot.delete_message(chat_id, qr_msg_id)
        except: pass

    try: bot.send_message(chat_id, f"🎉 <b>Payment Verified!</b>\n\n{course['secret_text'] if course else 'Contact Admin'}", parse_mode="HTML")
    except: pass

    purchases_col.insert_one({"user_id": user_id, "item_info": f"{order['course_id']} | Rate: ₹{order['amount']}", "date": get_ist_time()})
    update_channel_order_status(order, "MANUAL_APPROVED" if is_manual else "AUTO_VERIFIED", extra_text=sms_text or "")

# ==========================================
# 🛑 BOT COMMANDS
# ==========================================
@bot.message_handler(commands=["start"])
def start_command(message):
    uid = message.chat.id
    if uid == ADMIN_ID: bot.send_message(uid, "Admin Active.")
    else: bot.send_message(uid, "👋 Welcome! Buy via links.")

@bot.callback_query_handler(func=lambda call: True)
def handle_buttons(call):
    data, chat_id = call.data, call.message.chat.id

    if data.startswith("pay_upi_"):
        bot.answer_callback_query(call.id)
        course_id = data.replace("pay_upi_", "")
        course = courses_col.find_one({"course_id": course_id})
        if not course: return

        # 1. 100% नया यूनिक अमाउंट जनरेट होगा (नो लुकबैक - कोई पुराना SMS नहीं देखा जाएगा)
        amt_key = generate_unique_amount(course["amount"])
        order_id = str(uuid.uuid4())[:8]
        u_men = f"<a href='tg://user?id={call.from_user.id}'>{call.from_user.first_name}</a>"

        o_data = {
            "order_id": order_id, "course_id": course_id, "user_id": call.from_user.id, "chat_id": chat_id,
            "user_mention": u_men, "amount": amt_key, "status": "PENDING", 
            "created_at_dt": datetime.now(timezone.utc), "created_at": time.time(), "channel_msg_id": None
        }

        try:
            ch_msg = bot.send_message(DB_CHANNEL_ID, f"🟡 <b>[ORDER PENDING]</b>\n👤 {u_men}\n🔖 Order: <code>{order_id}</code>\n💰 ₹{amt_key}", parse_mode="HTML")
            o_data["channel_msg_id"] = ch_msg.message_id
        except: pass

        orders_col.insert_one(o_data.copy())
        with pending_lock:
            pending_orders[amt_key] = o_data
            all_orders_cache[order_id] = o_data

        qr_img_bio, clean_amt = generate_upi_qr(amt_key, order_id)
        inv = f"👤 {call.from_user.first_name}\n🆔 <code>{order_id}</code>\n💰 <b>₹{clean_amt}</b> भेजें।\n⏳ <i>QR {int(QR_EXPIRY_SECONDS/60)} मिनट में एक्सपायर होगा।</i>"
        sent = bot.send_photo(chat_id, qr_img_bio, caption=inv, parse_mode="HTML")
        orders_col.update_one({"order_id": order_id}, {"$set": {"qr_msg_id": sent.message_id}})
        
        threading.Timer(QR_EXPIRY_SECONDS, expire_qr, args=(chat_id, sent.message_id, amt_key, order_id)).start()
        return

    if data.startswith("paydone_"):
        oid = data.replace("paydone_", "")
        order = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
        if not order: return bot.answer_callback_query(call.id, "❌ Not found.", show_alert=True)
        if order.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"): 
            return bot.answer_callback_query(call.id, "✅ Already delivered.", show_alert=True)
        
        bot.answer_callback_query(call.id)
        # अगर वेरीफाई दबाने पर भी डिलीवर नहीं हुआ है, तो सीधा स्क्रीनशॉट मांगो
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("📸 Send Screenshot", callback_data=f"send_ss_{oid}"))
        bot.send_message(chat_id, "⚠️ <b>पेमेंट प्राप्त नहीं हुई!</b>\nअगर आपने पैसे काट दिए हैं, तो नीचे दिए बटन पर क्लिक करके स्क्रीनशॉट भेजें।", reply_markup=m, parse_mode="HTML")
        return

    if data.startswith("send_ss_"):
        bot.answer_callback_query(call.id)
        oid = data.replace("send_ss_", "")
        users_col.update_one({"user_id": chat_id}, {"$set": {"bot_state": "WAITING_PAYMENT_SS", "bot_state_order": oid}}, upsert=True)
        bot.send_message(chat_id, "📸 <b>कृपया अपना पेमेंट का स्क्रीनशॉट यहाँ भेजें...</b>", parse_mode="HTML")
        return

    if data.startswith("man_appr_"):
        oid = data.replace("man_appr_", "")
        o = orders_col.find_one({"order_id": oid})
        if o and o.get("status") in ["PENDING", "EXPIRED"]:
            deliver_course_to_buyer(o, is_manual=True)
            bot.answer_callback_query(call.id, "✅ Approved!", show_alert=True)
            try: bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            except: pass
        return

@bot.message_handler(content_types=["photo", "document"])
def handle_screenshot(message):
    uid = message.chat.id
    u = users_col.find_one({"user_id": uid})
    if u and u.get("bot_state") == "WAITING_PAYMENT_SS":
        oid = u.get("bot_state_order")
        users_col.update_one({"user_id": uid}, {"$unset": {"bot_state": "", "bot_state_order": ""}})
        fid = message.photo[-1].file_id if message.photo else message.document.file_id
        bot.send_message(uid, "⏳ <b>स्क्रीनशॉट एडमिन को भेज दिया गया है। कृपया प्रतीक्षा करें।</b>", parse_mode="HTML")
        
        m_admin = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Approve", callback_data=f"man_appr_{oid}"))
        bot.send_photo(DB_CHANNEL_ID, fid, caption=f"📩 <b>MANUAL APPROVAL</b>\n🆔 <code>{uid}</code>\n🔖 Order: <code>{oid}</code>", reply_markup=m_admin, parse_mode="HTML")

# ==========================================
# 🌐 FLASK WEBHOOK (Strict Validation)
# ==========================================
app = Flask(__name__)
AMOUNT_RE = re.compile(r"(?:Rs\.?|₹|INR)\s?([\d,]+\.\d{2})", re.IGNORECASE)

@app.route("/sms-webhook/<secret>", methods=["POST"])
def sms_webhook(secret):
    if secret != SMS_HOOK_SECRET: return "forbidden", 403
    sms_text = request.json.get("text", "").strip()
    
    # 1. सिर्फ 'Received' मैसेज पास होगा
    if "sent" in sms_text.lower() or "debited" in sms_text.lower(): return "ignored", 200

    m = AMOUNT_RE.search(sms_text)
    if not m: return "no_amount", 200
    amt_str = m.group(1).replace(",", "")
    
    txn_id = extract_txn_id(sms_text)

    # 2. Txn ID सुरक्षा ताला (दोबारा इस्तेमाल नामुमकिन)
    if txn_id:
        if orders_col.find_one({"txn_id": txn_id, "status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}}):
            return "already_used_txn", 200

    order = None
    with pending_lock:
        if amt_str in pending_orders: order = pending_orders.pop(amt_str)

    # 3. अगर नेट बंद था, तो पिछले 24 घंटे के एक्सपायर्ड में ढूँढो
    if not order:
        order = orders_col.find_one({"amount": amt_str, "status": {"$in": ["PENDING", "EXPIRED"]}, "created_at": {"$gte": time.time() - 86400}})

    if order:
        deliver_course_to_buyer(order, sms_text=sms_text, is_manual=False, txn_id=txn_id)
        return "matched", 200

    # 4. अगर कोई ऑर्डर नहीं मिला (यूज़र ने गलत अमाउंट भेजा), तो 7 घंटे के लिए पूल में सेव कर लो
    sms_pool_col.insert_one({"amount": amt_str, "raw_text": sms_text, "created_at_dt": datetime.now(timezone.utc)})
    return "saved_to_pool", 200

@app.route("/dashboard")
def dash(): return "Dashboard Active"

if __name__ == "__main__":
    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
