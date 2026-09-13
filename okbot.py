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
# 🛑 ENVIRONMENT VARIABLES
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
STALE_ORDER_SECONDS = int(os.environ.get("STALE_ORDER_HOURS", "24")) * 3600
SMS_POOL_TTL_HOURS = int(os.environ.get("SMS_POOL_TTL_HOURS", "5"))
ORDER_TTL_HOURS = int(os.environ.get("ORDER_TTL_HOURS", "48"))

DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme123")

BOT_USERNAME = "your_bot"

try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID"))
    DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID"))
except (TypeError, ValueError):
    print("❌ ERROR: 'ADMIN_ID' या 'DB_CHANNEL_ID' Environment Variable सही से सेट नहीं है।")
    sys.exit(1)

if not BOT_TOKEN or not MONGO_URI or not UPI_ID or not SMS_HOOK_SECRET:
    print("❌ ERROR: कोई महत्वपूर्ण Environment Variable मिसिंग है।")
    sys.exit(1)

bot = telebot.TeleBot(BOT_TOKEN)
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_time():
    return datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p")

# ==========================================
# 🍃 MONGODB SETUP
# ==========================================
try:
    mongo_client = pymongo.MongoClient(MONGO_URI)
    db = mongo_client.get_database(MONGO_DB_NAME)
    users_col = db["users"]
    courses_col = db["courses"]
    batches_col = db["batches"]
    purchases_col = db["purchases"]
    file_links_col = db["file_links"]
    settings_col = db["settings"]
    orders_col = db["orders"]
    sms_pool_col = db["sms_pool"]
    offers_col = db["offers"]
    channel_logs_col = db["channel_logs"]

    try:
        sms_pool_col.create_index("created_at_dt", expireAfterSeconds=SMS_POOL_TTL_HOURS * 3600)
        orders_col.create_index("created_at_dt", expireAfterSeconds=ORDER_TTL_HOURS * 3600)
        orders_col.create_index([("user_id", 1), ("offer_id", 1), ("status", 1)])
        orders_col.create_index([("course_id", 1), ("status", 1)])
        orders_col.create_index("txn_id")
    except Exception:
        pass
    print(f"✅ MongoDB Connected! (Database: {MONGO_DB_NAME})")
except Exception as e:
    print(f"❌ MongoDB Error: {e}")
    sys.exit(1)

# ==========================================
# 📝 TEXT FORMATTING & ACTIVITY TRACKER
# ==========================================
def get_formatted_text(message):
    if hasattr(message, "html_text") and message.html_text: return message.html_text
    if hasattr(message, "html_caption") and message.html_caption: return message.html_caption
    return message.caption or message.text or ""

user_chat_messages, user_inactivity_timers, tracker_lock = {}, {}, threading.Lock()

def clear_inactive_chat(chat_id):
    with tracker_lock:
        msg_ids = user_chat_messages.pop(chat_id, [])
        user_inactivity_timers.pop(chat_id, None)
    for mid in msg_ids:
        try: bot.delete_message(chat_id, mid)
        except Exception: pass

def register_activity(chat_id, message_id=None):
    if not isinstance(chat_id, int) or chat_id <= 0 or chat_id == ADMIN_ID: return
    with tracker_lock:
        if chat_id not in user_chat_messages: user_chat_messages[chat_id] = []
        if message_id and message_id not in user_chat_messages[chat_id]: user_chat_messages[chat_id].append(message_id)
        if chat_id in user_inactivity_timers: user_inactivity_timers[chat_id].cancel()
        new_timer = threading.Timer(INACTIVITY_CLEANUP_SECONDS, clear_inactive_chat, args=(chat_id,))
        user_inactivity_timers[chat_id] = new_timer
        new_timer.start()

orig_send_message = bot.send_message
orig_send_photo = bot.send_photo
orig_send_video = bot.send_video
orig_send_document = bot.send_document
orig_send_media_group = bot.send_media_group

def tracked_send_message(chat_id, *args, **kwargs):
    msg = orig_send_message(chat_id, *args, **kwargs)
    register_activity(chat_id, msg.message_id)
    return msg
bot.send_message = tracked_send_message

def tracked_send_photo(chat_id, *args, **kwargs):
    msg = orig_send_photo(chat_id, *args, **kwargs)
    register_activity(chat_id, msg.message_id)
    return msg
bot.send_photo = tracked_send_photo

def tracked_send_video(chat_id, *args, **kwargs):
    msg = orig_send_video(chat_id, *args, **kwargs)
    register_activity(chat_id, msg.message_id)
    return msg
bot.send_video = tracked_send_video

def tracked_send_document(chat_id, *args, **kwargs):
    msg = orig_send_document(chat_id, *args, **kwargs)
    register_activity(chat_id, msg.message_id)
    return msg
bot.send_document = tracked_send_document

bot.send_media_group = orig_send_media_group

def generate_upi_qr(amount, order_id):
    clean_amt = re.sub(r"[^\d.]", "", str(amount))
    upi_url = f"upi://pay?pa={UPI_ID}&pn={MERCHANT_NAME}&am={clean_amt}&cu=INR&tn=Order_{order_id}"
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=2)
    qr.add_data(upi_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    w, h = qr_img.size
    box_w, box_h = int(w * 0.28), int(int(w * 0.28) * 0.40)
    box_x, box_y = (w - box_w) // 2, (h - box_h) // 2
    draw = ImageDraw.Draw(qr_img)
    draw.rounded_rectangle([box_x, box_y, box_x + box_w, box_y + box_h], radius=6, fill="#ffffff", outline="#0b1329", width=2)
    lw = max(2, int(box_h * 0.12))
    let_w, let_h, gap = box_w * 0.18, box_h * 0.45, box_w * 0.08
    start_x = box_x + (box_w - ((let_w * 2) + gap * 2 + lw)) // 2
    start_y = box_y + (box_h - let_h) // 2
    u_x = start_x
    draw.line([(u_x, start_y), (u_x, start_y + let_h)], fill="#097939", width=lw)
    draw.line([(u_x, start_y + let_h), (u_x + let_w, start_y + let_h)], fill="#097939", width=lw)
    draw.line([(u_x + let_w, start_y + let_h), (u_x + let_w, start_y)], fill="#097939", width=lw)
    p_x = u_x + let_w + gap
    draw.line([(p_x, start_y), (p_x, start_y + let_h)], fill="#F37021", width=lw)
    draw.line([(p_x, start_y), (p_x + let_w, start_y)], fill="#F37021", width=lw)
    draw.line([(p_x + let_w, start_y), (p_x + let_w, start_y + let_h // 2)], fill="#F37021", width=lw)
    draw.line([(p_x + let_w, start_y + let_h // 2), (p_x, start_y + let_h // 2)], fill="#F37021", width=lw)
    i_x = p_x + let_w + gap + lw // 2
    draw.line([(i_x, start_y), (i_x, start_y + let_h)], fill="#1a73e8", width=lw)
    bio = io.BytesIO()
    qr_img.save(bio, "PNG")
    bio.seek(0)
    return bio, clean_amt

# ==========================================
# 🛡️ STATE MANAGEMENT & ROLLING PAISE
# ==========================================
admin_data, user_states, user_qr_messages, pending_orders, all_orders_cache = {}, {}, {}, {}, {}
user_cooldowns = {}
pending_lock = threading.Lock()
rolling_counter = 1

def check_rate_limit(user_id, cooldown=2):
    now = time.time()
    if user_id in user_cooldowns and now - user_cooldowns[user_id] < cooldown:
        return False
    user_cooldowns[user_id] = now
    return True

def set_user_state(user_id, step, order_id=None, amount_key=None):
    user_states[user_id] = {"step": step, "order_id": order_id, "amount_key": amount_key}
    users_col.update_one({"user_id": user_id}, {"$set": {"bot_state": step, "bot_state_order": order_id, "bot_state_amt": amount_key}}, upsert=True)

def get_user_state(user_id):
    if user_id in user_states: return user_states[user_id]
    u = users_col.find_one({"user_id": user_id})
    if u and u.get("bot_state"):
        st = {"step": u.get("bot_state"), "order_id": u.get("bot_state_order"), "amount_key": u.get("bot_state_amt")}
        user_states[user_id] = st
        return st
    return {}

def clear_user_state(user_id):
    user_states.pop(user_id, None)
    users_col.update_one({"user_id": user_id}, {"$unset": {"bot_state": "", "bot_state_order": "", "bot_state_amt": ""}})

def generate_unique_amount(base_amount):
    global rolling_counter
    base_clean = int(round(float(base_amount)))
    with pending_lock:
        for _ in range(99):
            paise = rolling_counter
            rolling_counter = 1 if rolling_counter >= 99 else rolling_counter + 1
            candidate = f"{base_clean + (paise / 100):.2f}"
            if candidate not in pending_orders:
                return candidate
        return f"{base_clean + (random.randint(1, 99) / 100):.2f}"

def extract_txn_id(text):
    m = re.search(r"txn\s+([A-Za-z0-9]+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None

# ==========================================
# 🎟 OFFER VALIDATION
# ==========================================
def get_offer_usage_count(user_id, offer_code):
    return orders_col.count_documents({
        "user_id": user_id,
        "offer_id": offer_code,
        "status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}
    })

def check_offer_validity(offer, user_id, course_id=None):
    if not offer: return False, "not_found"
    now_ts = time.time()
    if offer.get("expires_at_ts") and now_ts > offer["expires_at_ts"]: return False, "expired"
    if offer.get("max_users", -1) != -1 and offer.get("used_count", 0) >= offer["max_users"]: return False, "max_reached"
    per_user_limit = offer.get("per_user_limit", 1) 
    if per_user_limit != -1 and get_offer_usage_count(user_id, offer["offer_code"]) >= per_user_limit: return False, "already_used"
    if course_id and offer.get("target_type") == "single" and offer.get("target_course_id") != course_id: return False, "wrong_course"
    return True, "ok"

def clear_user_offer(user_id, offer_code):
    users_col.update_one({"user_id": user_id}, {"$unset": {"active_offer_code": "", "active_offer": ""}})

def update_channel_order_status(order, status_type, extra_text=""):
    channel_msg_id = order.get("channel_msg_id")
    if not channel_msg_id: return
    user_mention = order.get("user_mention", f"User ({order['user_id']})")
    discount_info = f"\n🎟 <b>Offer Applied:</b> {order.get('discount_percent')}% OFF (Original: ₹{order.get('original_amount')})" if order.get("discount_percent") else ""

    course = courses_col.find_one({"course_id": order.get("course_id")})
    ch_name = f"\n📺 <b>Channel:</b> {course.get('channel_name')}" if course and course.get("channel_name") else ""

    if status_type == "EXPIRED":
        new_text = f"🔴 <b>[UNPAID / QR EXPIRED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Initiated at:</b> {order.get('created_at_str', '')}\n⏳ <b>Status:</b> ⚠️ 10 मिनट में पेमेंट नहीं आई"
    elif status_type == "AUTO_VERIFIED":
        new_text = f"🟢 <b>[PAYMENT COMPLETED & AUTO-DELIVERED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount Paid:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Delivered at:</b> {get_ist_time()}\n⚡ <b>Status:</b> ✅ ऑटो-वेरिफाइड\n\n📩 <code>{extra_text[:180]}</code>"
    elif status_type == "MANUAL_APPROVED":
        new_text = f"✅ <b>[MANUAL-APPROVED & DELIVERED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Approved at:</b> {get_ist_time()}\n⚡ <b>Status:</b> ✅ एडमिन द्वारा स्क्रीनशॉट देखकर अप्रूव किया गया"
    else: return

    try: bot.edit_message_text(new_text, chat_id=DB_CHANNEL_ID, message_id=channel_msg_id, parse_mode="HTML")
    except Exception: pass

def expire_qr(chat_id, message_id, course_id, amount_key, order_id):
    order = all_orders_cache.get(order_id) or orders_col.find_one({"order_id": order_id})
    if not order or order.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"): return

    res = orders_col.update_one({"order_id": order_id, "status": "PENDING"}, {"$set": {"status": "EXPIRED"}})
    if res.modified_count == 0: return 

    order["status"] = "EXPIRED"
    update_channel_order_status(order, "EXPIRED")

    with pending_lock: pending_orders.pop(amount_key, None)
    if chat_id in user_qr_messages and user_qr_messages[chat_id] == message_id: del user_qr_messages[chat_id]
    
    try: bot.delete_message(chat_id, message_id)
    except Exception: pass

    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Verify Payment", callback_data=f"paydone_{order_id}"))
    try: bot.send_message(chat_id, "⏳ <b>क्यूआर कोड का समय समाप्त!</b>\n\nअगर आपने पेमेंट कर दिया है, तो नीचे <b>'✅ Verify Payment'</b> दबाएं।", reply_markup=markup, parse_mode="HTML")
    except Exception: pass

def deliver_course_to_buyer(order, sms_text=None, is_manual=False, txn_id=None):
    order_id, chat_id, user_id, course_id = order["order_id"], order["chat_id"], order["user_id"], order["course_id"]
    course = courses_col.find_one({"course_id": course_id})
    new_status = "COMPLETED_MANUAL" if is_manual else "COMPLETED_AUTO"
    
    update_fields = {
        "status": new_status,
        "delivered_at": get_ist_time(),
        "delivered_at_ts": time.time()
    }
    if txn_id:
        update_fields["txn_id"] = txn_id

    res = orders_col.update_one({"order_id": order_id, "status": {"$in": ["PENDING", "EXPIRED"]}}, {"$set": update_fields})
    if res.modified_count == 0: return

    order["status"] = new_status
    with pending_lock: pending_orders.pop(order.get("amount"), None)
    
    state = get_user_state(user_id)
    if state and state.get("order_id") == order_id: clear_user_state(user_id)
    
    qr_msg_id = user_qr_messages.get(chat_id) or order.get("qr_msg_id")
    if qr_msg_id:
        try: bot.delete_message(chat_id, qr_msg_id)
        except Exception: pass
    if chat_id in user_qr_messages: del user_qr_messages[chat_id]

    if not course:
        try: bot.send_message(chat_id, "⚠️ Payment verify ho gayi hai, par pack nahi mila. Admin se sampark karein.")
        except Exception: pass
        return

    try: bot.send_message(chat_id, f"🎉 <b>Payment Verified Successfully!</b>\n\n{course['secret_text']}", parse_mode="HTML", protect_content=PROTECT_CONTENT)
    except Exception: pass

    purchases_col.insert_one({"user_id": user_id, "username": order.get("user_mention", f"User ({user_id})"), "item_info": f"{course_id} | Rate: ₹{order['amount']} | {new_status} (order {order_id})", "date": get_ist_time()})
    if order.get("offer_id"):
        offers_col.update_one({"offer_code": order["offer_id"]}, {"$inc": {"used_count": 1}})
    update_channel_order_status(order, "MANUAL_APPROVED" if is_manual else "AUTO_VERIFIED", extra_text=sms_text or "")

# ==========================================
# 🛑 GATEKEEPER: CHANNEL JOIN
# ==========================================
@bot.chat_join_request_handler()
def handle_join_request(message):
    chat_id, user_id = message.chat.id, message.from_user.id
    used_link = message.invite_link.invite_link if message.invite_link else ""
    
    course = None
    for c in courses_col.find({"channel_id": chat_id}):
        if c.get("secret_text", "") and used_link in c.get("secret_text", ""):
            course = c
            break
    if not course: return
        
    course_id = course["course_id"]
    channel_name = course.get("channel_name") or message.chat.title or "Private Channel"
    purchase = purchases_col.find_one({"user_id": user_id, "item_info": {"$regex": course_id}})
    now_str = get_ist_time()

    if purchase:
        try: 
            bot.approve_chat_join_request(chat_id, user_id)
            channel_logs_col.insert_one({"user_id": user_id, "first_name": message.from_user.first_name, "username": message.from_user.username or "None", "course_id": course_id, "channel_name": channel_name, "status": "APPROVED", "date": now_str})
            orig_send_message(user_id, f"✅ <b>Request Approved!</b>\nWelcome to <b>{channel_name}</b>.", parse_mode="HTML")
        except Exception: pass
    else:
        try:
            bot.decline_chat_join_request(chat_id, user_id)
            channel_logs_col.insert_one({"user_id": user_id, "first_name": message.from_user.first_name, "username": message.from_user.username or "None", "course_id": course_id, "channel_name": channel_name, "status": "DENIED", "date": now_str})
            orig_send_message(user_id, "❌ <b>Access Denied!</b>\nPlease purchase this pack from the bot first.", parse_mode="HTML")
        except Exception: pass

# ==========================================
# 🛑 मेन्यू और सेंडिंग
# ==========================================
def send_course_to_user(chat_id, course):
    raw_promo = course.get("promo_media", [])
    promo_items = json.loads(raw_promo) if isinstance(raw_promo, str) else raw_promo if isinstance(raw_promo, list) else []

    markup = InlineKeyboardMarkup().row(InlineKeyboardButton(f"🇮🇳 UPI (Pay ₹{course['amount']})", callback_data=f"pay_upi_{course['course_id']}"))
    btn_row = []
    if INTERNATIONAL_LINK: btn_row.append(InlineKeyboardButton("🌍 International", url=INTERNATIONAL_LINK))
    if CHAT_LINK: btn_row.append(InlineKeyboardButton("💬 Chat with Me", url=CHAT_LINK))
    if btn_row: markup.row(*btn_row)

    media_items = [it for it in promo_items if isinstance(it, dict) and it.get("type") in ["photo", "video"]]
    first_photo_caption = media_items[0].get("caption", "").strip() if media_items else ""
    custom_caption = course.get("custom_caption", "").strip()
    final_cap = f"{first_photo_caption}\n\n{custom_caption}" if first_photo_caption and custom_caption else (first_photo_caption or custom_caption)

    if not media_items:
        bot.send_message(chat_id, final_cap or f"📚 <b>Pack: {course['course_id']}</b>\nPrice: ₹{course['amount']}", reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
    elif len(media_items) == 1:
        it = media_items[0]
        if it["type"] == "photo": bot.send_photo(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
        elif it["type"] == "video": bot.send_video(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
    else:
        media_group_html = [InputMediaPhoto(it["file_id"], caption=final_cap if i == 0 else "", parse_mode="HTML") if it["type"] == "photo" else InputMediaVideo(it["file_id"], caption=final_cap if i == 0 else "", parse_mode="HTML") for i, it in enumerate(media_items)]
        try:
            sent_grp = orig_send_media_group(chat_id, media_group_html, protect_content=PROTECT_CONTENT)
            for m in sent_grp: register_activity(chat_id, m.message_id)
        except Exception: pass
        bot.send_message(chat_id, f"👆 <b>Choose an option to buy (₹{course['amount']}):</b>\n", reply_markup=markup, parse_mode="HTML")

def send_custom_start_menu(chat_id):
    cfg = settings_col.find_one({"_id": "start_menu"})
    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("📋 View All Plans / Packs", callback_data="user_view_plans"))
    if cfg:
        for b in cfg.get("buttons", []):
            if b["url"].startswith("http"): markup.row(InlineKeyboardButton(b["text"], url=b["url"]))
            else: markup.row(InlineKeyboardButton(b["text"], callback_data=f"mainmenu_{b['url']}"))
        m_type, txt, fid = cfg.get("media_type"), cfg.get("text", ""), cfg.get("file_id")
        if m_type == "photo" and fid: bot.send_photo(chat_id, fid, caption=txt, reply_markup=markup, parse_mode="HTML")
        elif m_type == "video" and fid: bot.send_video(chat_id, fid, caption=txt, reply_markup=markup, parse_mode="HTML")
        else: bot.send_message(chat_id, txt or "👋 Welcome to our Store!", reply_markup=markup, parse_mode="HTML")
    else: bot.send_message(chat_id, "👋 <b>Welcome to our Store!</b>\n\nSelect an option below to get started:", reply_markup=markup, parse_mode="HTML")

def send_admin_panel(chat_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("➕ Add Single Pack", callback_data="admin_add_course"))
    markup.row(InlineKeyboardButton("🗑 Delete Pack", callback_data="admin_delete_course"))
    markup.row(InlineKeyboardButton("🎟 Create Promo Offer", callback_data="admin_create_offer"))
    markup.row(InlineKeyboardButton("📋 Manage Store Plans", callback_data="admin_manage_plans"))
    bot.send_message(chat_id, "🛠 <b>Admin Panel</b>\nPlease select an option:\n", reply_markup=markup, parse_mode="HTML")

@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.chat.id
    if not check_rate_limit(user_id, 1): return
    register_activity(user_id, message.message_id)
    users_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id, "updated_at": get_ist_time()}}, upsert=True)
    param = message.text.split()[1].strip() if len(message.text.split()) > 1 else ""

    if param.startswith("c_"):
        course = courses_col.find_one({"course_id": param})
        if course: send_course_to_user(user_id, course)
        else: bot.send_message(user_id, "❌ <b>This link is not available.</b>", parse_mode="HTML")
    else:
        if user_id == ADMIN_ID: send_admin_panel(user_id)
        else: send_custom_start_menu(user_id)

@bot.callback_query_handler(func=lambda call: True)
def handle_buttons(call):
    global BOT_USERNAME
    data, chat_id = call.data, call.message.chat.id
    if not check_rate_limit(chat_id, 1.5): return bot.answer_callback_query(call.id, "⚠️ थोड़ा धीमे!")
    register_activity(chat_id)

    if data == "user_view_plans":
        bot.answer_callback_query(call.id)
        plans = settings_col.find_one({"_id": "store_plans"})
        c_ids = plans.get("course_ids", []) if plans else [c["course_id"] for c in courses_col.find().limit(10)]
        for cid in c_ids:
            c = courses_col.find_one({"course_id": cid})
            if c: send_course_to_user(chat_id, c)
        return

    # ==========================================
    # ⚡ QR CODE GENERATION (NO LOOKBACK - GHOST BUG FIXED)
    # ==========================================
    if data.startswith("pay_upi_"):
        bot.answer_callback_query(call.id)
        
        loading_msg = None
        try:
            loading_msg = orig_send_message(chat_id, "⏳ <b>Generating unique payment QR...</b>", parse_mode="HTML")
        except Exception: pass

        course_id = data.replace("pay_upi_", "")
        course = courses_col.find_one({"course_id": course_id})
        if not course:
            if loading_msg: bot.delete_message(chat_id, loading_msg.message_id)
            return

        base_price = float(course["amount"])
        order_id, amt_key = str(uuid.uuid4())[:8], generate_unique_amount(base_price)
        u_men = f"<a href='tg://user?id={call.from_user.id}'>{call.from_user.first_name}</a>"

        o_data = {
            "order_id": order_id, "course_id": course_id, "user_id": call.from_user.id, "chat_id": chat_id,
            "user_mention": u_men, "amount": amt_key, "original_amount": str(base_price),
            "status": "PENDING", "created_at_str": get_ist_time(), "created_at": time.time(),
            "created_at_dt": datetime.now(timezone.utc), "channel_msg_id": None
        }

        try:
            ch_msg = bot.send_message(DB_CHANNEL_ID, f"🟡 <b>[ORDER INITIATED]</b>\n\n👤 {u_men}\n🔖 Order: <code>{order_id}</code>\n💰 Amount: ₹{amt_key}", parse_mode="HTML")
            o_data["channel_msg_id"] = ch_msg.message_id
        except Exception: pass

        orders_col.insert_one(o_data.copy())
        with pending_lock:
            pending_orders[amt_key] = o_data
            all_orders_cache[order_id] = o_data

        set_user_state(chat_id, "PENDING_UPI", order_id, amt_key)

        qr_img_bio, clean_amt = generate_upi_qr(amt_key, order_id)
        inv = f"👤 <b>User:</b> {call.from_user.first_name}\n🆔 <b>Order:</b> <code>{order_id}</code>\n💰 <b>Amount:</b> ₹{clean_amt}\n⚠️ <b>कृपया ठीक यही अमाउंट भेजें।</b>\n⏳ <i>QR 10 मिनट में एक्सपायर होगा।</i>"
        sent_msg = bot.send_photo(chat_id, photo=qr_img_bio, caption=inv, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("💬 Chat with Admin", url=CHAT_LINK)) if CHAT_LINK else None, parse_mode="HTML")
        user_qr_messages[chat_id] = sent_msg.message_id
        orders_col.update_one({"order_id": order_id}, {"$set": {"qr_msg_id": sent_msg.message_id}})

        if loading_msg:
            try: bot.delete_message(chat_id, loading_msg.message_id)
            except Exception: pass

        threading.Timer(QR_EXPIRY_SECONDS, expire_qr, args=(chat_id, sent_msg.message_id, course_id, amt_key, order_id)).start()
        return

    if data.startswith("paydone_"):
        oid = data.replace("paydone_", "")
        order = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
        if not order: return bot.answer_callback_query(call.id, "❌ Order not found.", show_alert=True)
        if order.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"): return bot.answer_callback_query(call.id, "✅ Already delivered.", show_alert=True)
        
        bot.answer_callback_query(call.id, "⏳ Checking SMS status...", show_alert=False)
        bot.send_message(chat_id, "📸 <b>अगर पेमेंट कट गई है और डिलीवरी नहीं हुई, तो यहाँ स्क्रीनशॉट भेजें:</b>", parse_mode="HTML")
        return

# ==========================================
# 🌐 FLASK WEBHOOK (Strict Verification)
# ==========================================
app = Flask(__name__)
AMOUNT_RE_DECIMAL = re.compile(r"(?:Rs\.?|₹|INR)\s?([\d,]+\.\d{2})", re.IGNORECASE)
AMOUNT_RE_INT = re.compile(r"(?:Rs\.?|₹|INR)\s?([\d,]+)(?!\.\d)", re.IGNORECASE)

@app.route("/")
def home(): return "Telegram Bot API Running."

@app.route("/sms-webhook/<secret>", methods=["GET", "POST"])
def sms_webhook(secret):
    if secret != SMS_HOOK_SECRET: return "forbidden", 403
    sms_text = (request.get_json(silent=True) or request.form).get("text", "").strip() if request.method == "POST" else request.args.get("text", "").strip()
    if not sms_text: return "no text", 400

    lower = sms_text.lower()
    if "sent" in lower or "debited" in lower or "slice" in lower: return "ignored_debit", 200

    m = AMOUNT_RE_DECIMAL.search(sms_text)
    has_dec = bool(m)
    if not m: m = AMOUNT_RE_INT.search(sms_text)
    if not m: return "no amount found", 200

    amt_str = m.group(1).replace(",", "")
    f_round = f"{float(amt_str):.2f}" if not has_dec else amt_str
    txn_id = extract_txn_id(sms_text)

    # 1. ट्रांजेक्शन ID लॉक चेक (डुप्लीकेट री-डिलीवरी रोकें)
    if txn_id:
        existing_order = orders_col.find_one({"txn_id": txn_id, "status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}})
        if existing_order:
            return "already_used_txn", 200

    order = None

    # 2. एक्टिव पेंडिंग ऑर्डर्स में ढूँढें
    with pending_lock:
        if f_round in pending_orders:
            order = pending_orders.pop(f_round)

    # 3. अगर एक्टिव में नहीं है (जैसे फ़ोन रात को ऑफ़लाइन था), तो पिछले 24 घंटे के EXPIRED ऑर्डर्स में ढूँढें
    if not order:
        stale_cutoff = time.time() - STALE_ORDER_SECONDS
        order = orders_col.find_one({
            "amount": f_round,
            "status": {"$in": ["PENDING", "EXPIRED"]},
            "created_at": {"$gte": stale_cutoff}
        })

    if order:
        deliver_course_to_buyer(order, sms_text=sms_text, is_manual=False, txn_id=txn_id)
        return "matched", 200

    return "saved_to_pool", 200

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USERNAME or auth.password != DASHBOARD_PASSWORD:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Dashboard"'})
        return f(*args, **kwargs)
    return wrapper

@app.route("/dashboard")
@require_auth
def dashboard_page(): return "<h2>Store Dashboard Active</h2>"

if __name__ == "__main__":
    try: BOT_USERNAME = bot.get_me().username
    except Exception: pass
    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
