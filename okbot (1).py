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
# 🛑 ENVIRONMENT VARIABLES (Render से लेगा)
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
# 🛡️ RATE LIMITER & STATE MANAGEMENT (Crash Proof)
# ==========================================
admin_data, user_states, user_qr_messages, pending_orders, all_orders_cache = {}, {}, {}, {}, {}
user_cooldowns = {}
pending_lock = threading.Lock()

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
    base_clean = round(float(base_amount))
    flat_key = f"{base_clean:.2f}"
    with pending_lock:
        if flat_key not in pending_orders: return flat_key
        for _ in range(300):
            paise = random.randint(1, 98)
            candidate = f"{base_clean + (paise / 100):.2f}"
            if candidate not in pending_orders: return candidate
        return f"{base_clean + (random.randint(1, 99) / 100):.2f}"

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
        new_text = f"🔴 <b>[UNPAID / QR EXPIRED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Initiated at:</b> {order.get('created_at_str', '')}\n⏳ <b>Status:</b> ⚠️ 10 मिनट में पेमेंट नहीं आई (स्क्रीनशॉट पेंडिंग)"
    elif status_type == "AUTO_VERIFIED":
        new_text = f"🟢 <b>[PAYMENT COMPLETED & AUTO-DELIVERED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount Paid:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Delivered at:</b> {get_ist_time()}\n⚡ <b>Status:</b> ✅ ऑटो-वेरिफाइड (SMS द्वारा)\n\n📩 <code>{extra_text[:180]}</code>"
    elif status_type == "MANUAL_APPROVED":
        new_text = f"✅ <b>[MANUAL-APPROVED & DELIVERED]</b>\n\n👤 <b>User:</b> {user_mention}\n🔖 <b>Order ID:</b> <code>{order['order_id']}</code>\n📚 <b>Pack:</b> <code>{order['course_id']}</code>{ch_name}\n💰 <b>Amount:</b> ₹{order['amount']}{discount_info}\n⏰ <b>Approved at:</b> {get_ist_time()}\n⚡ <b>Status:</b> ✅ एडमिन द्वारा स्क्रीनशॉट देखकर अप्रूव किया गया"
    else: return

    try: bot.edit_message_text(new_text, chat_id=DB_CHANNEL_ID, message_id=channel_msg_id, parse_mode="HTML")
    except Exception: pass

def screenshot_timeout(chat_id, order_id, prompt_msg_id):
    state = get_user_state(chat_id)
    if state and state.get("step") == "WAITING_PAYMENT_SS" and state.get("order_id") == order_id:
        clear_user_state(chat_id) 
        try:
            bot.edit_message_text("⏳ <b>समय समाप्त!</b>\nआपने 10 मिनट के अंदर स्क्रीनशॉट नहीं भेजा।\nयह वेरिफिकेशन रद्द कर दिया गया है, कृपया कोर्स पर दोबारा क्लिक करें।", chat_id=chat_id, message_id=prompt_msg_id, parse_mode="HTML")
        except Exception:
            pass

def expire_qr(chat_id, message_id, course_id, amount_key, order_id):
    order = all_orders_cache.get(order_id) or orders_col.find_one({"order_id": order_id})
    if not order: return
    if order.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"): return

    sms_rec = sms_pool_col.find_one({"amount": amount_key, "status": "UNUSED"})
    if sms_rec:
        updated = sms_pool_col.update_one({"_id": sms_rec["_id"], "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
        if updated.modified_count > 0:
            deliver_course_to_buyer(order, sms_text=sms_rec.get("raw_text"), is_manual=False)
            return

    # Atomic Update for Expiry to prevent Race Conditions
    res = orders_col.update_one({"order_id": order_id, "status": "PENDING"}, {"$set": {"status": "EXPIRED"}})
    if res.modified_count == 0:
        return 

    order["status"] = "EXPIRED"
    update_channel_order_status(order, "EXPIRED")

    with pending_lock: pending_orders.pop(amount_key, None)
    if chat_id in user_qr_messages and user_qr_messages[chat_id] == message_id: del user_qr_messages[chat_id]
    
    try: bot.delete_message(chat_id, message_id)
    except Exception: pass
    if order.get("qr_msg_id") and order.get("qr_msg_id") != message_id:
        try: bot.delete_message(chat_id, order.get("qr_msg_id"))
        except Exception: pass

    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Verify Payment", callback_data=f"paydone_{order_id}"))
    try: bot.send_message(chat_id, "⏳ <b>क्यूआर कोड का समय समाप्त!</b>\n\nअगर आपने पेमेंट कर दिया है, तो नीचे <b>'✅ Verify Payment'</b> दबाएं।", reply_markup=markup, parse_mode="HTML")
    except Exception: pass

def deliver_course_to_buyer(order, sms_text=None, is_manual=False):
    order_id, chat_id, user_id, course_id = order["order_id"], order["chat_id"], order["user_id"], order["course_id"]
    course = courses_col.find_one({"course_id": course_id})
    new_status = "COMPLETED_MANUAL" if is_manual else "COMPLETED_AUTO"
    
    # Atomic Lock
    res = orders_col.update_one({"order_id": order_id, "status": {"$in": ["PENDING", "EXPIRED"]}}, {"$set": {"status": new_status, "delivered_at": get_ist_time(), "delivered_at_ts": time.time()}})
    if res.modified_count == 0: return # Prevents double delivery

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

    date_now = get_ist_time()
    verify_type = "MANUAL-APPROVED" if is_manual else "AUTO-VERIFIED"
    purchases_col.insert_one({"user_id": user_id, "username": order.get("user_mention", f"User ({user_id})"), "item_info": f"{course_id} | Rate: ₹{order['amount']} | {verify_type} (order {order_id})", "date": date_now})
    if order.get("offer_id"):
        offers_col.update_one({"offer_code": order["offer_id"]}, {"$inc": {"used_count": 1}})
        fresh_offer = offers_col.find_one({"offer_code": order["offer_id"]})
        per_user_limit = (fresh_offer or {}).get("per_user_limit", 1)
        if per_user_limit != -1 and get_offer_usage_count(user_id, order["offer_id"]) >= per_user_limit:
            clear_user_offer(user_id, order["offer_id"])
    update_channel_order_status(order, "MANUAL_APPROVED" if is_manual else "AUTO_VERIFIED", extra_text=sms_text or "")

    manual_msg_id = order.get("manual_msg_id")
    if manual_msg_id and not is_manual:
        try:
            bot.edit_message_reply_markup(DB_CHANNEL_ID, manual_msg_id, reply_markup=None)
            orig_send_message(DB_CHANNEL_ID, f"✅ <b>ORDER {order_id} ऑटोमैटिक रूप से वेरिफाई और डिलीवर हो गया</b> (delayed SMS मिल गया)।", parse_mode="HTML")
        except Exception: pass

# ==========================================
# 🛑 GATEKEEPER: CHANNEL JOIN REQUEST HANDLER
# ==========================================
@bot.chat_join_request_handler()
def handle_join_request(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    used_link = message.invite_link.invite_link if message.invite_link else ""
    
    course = None
    for c in courses_col.find({"channel_id": chat_id}):
        if c.get("secret_text", "") and used_link in c.get("secret_text", ""):
            course = c
            break
            
    if not course: 
        return
        
    course_id = course["course_id"]
    channel_name = course.get("channel_name") or message.chat.title or f"Private Channel/Group"
    purchase = purchases_col.find_one({"user_id": user_id, "item_info": {"$regex": course_id}})
    now_str = get_ist_time()
    
    u_first_name = message.from_user.first_name
    u_username = message.from_user.username or "None"
    u_men = f"<a href='tg://user?id={user_id}'>{u_first_name}</a>"

    if purchase:
        try: 
            bot.approve_chat_join_request(chat_id, user_id)
            channel_logs_col.insert_one({
                "user_id": user_id, 
                "first_name": u_first_name,
                "username": u_username,
                "course_id": course_id, 
                "channel_name": channel_name, 
                "status": "APPROVED", 
                "date": now_str
            })
            orig_send_message(user_id, f"✅ <b>Request Approved!</b>\nWelcome to <b>{channel_name}</b>.", parse_mode="HTML")
        except Exception: pass
    else:
        try:
            bot.decline_chat_join_request(chat_id, user_id)
            channel_logs_col.insert_one({
                "user_id": user_id, 
                "first_name": u_first_name,
                "username": u_username,
                "course_id": course_id, 
                "channel_name": channel_name, 
                "status": "DENIED", 
                "date": now_str
            })
            orig_send_message(user_id, f"❌ <b>Access Denied!</b>\nYou haven't purchased this pack yet. Please buy it from the bot first.", parse_mode="HTML")
            
            log_msg = f"🚫 <b>[JOIN DENIED - NO PAYMENT]</b>\n\n👤 <b>User:</b> {u_men} (<code>{user_id}</code>)\n📺 <b>Channel:</b> {channel_name}\n⏰ <b>Time:</b> {now_str}"
            orig_send_message(DB_CHANNEL_ID, log_msg, parse_mode="HTML")
        except Exception: pass

# ==========================================
# 🛑 मेन्यू और सेंडिंग
# ==========================================
def send_course_to_user(chat_id, course):
    raw_promo = course.get("promo_media", [])
    if isinstance(raw_promo, str):
        try: promo_items = json.loads(raw_promo)
        except Exception: promo_items = []
    elif isinstance(raw_promo, list): promo_items = raw_promo
    else: promo_items = []

    markup = InlineKeyboardMarkup().row(InlineKeyboardButton(f"🇮🇳 UPI (Pay ₹{course['amount']})", callback_data=f"pay_upi_{course['course_id']}"))
    btn_row = []
    if INTERNATIONAL_LINK: btn_row.append(InlineKeyboardButton("🌍 International", url=INTERNATIONAL_LINK))
    if CHAT_LINK: btn_row.append(InlineKeyboardButton("💬 Chat with Me", url=CHAT_LINK))
    if btn_row: markup.row(*btn_row)

    media_items = [it for it in promo_items if isinstance(it, dict) and it.get("type") in ["photo", "video"]]
    text_items = [it for it in promo_items if isinstance(it, dict) and it.get("type") == "text"]

    first_photo_caption = media_items[0].get("caption", "").strip() if media_items else ""
    custom_caption = course.get("custom_caption", "").strip()
    final_cap = f"{first_photo_caption}\n\n{custom_caption}" if first_photo_caption and custom_caption else (first_photo_caption or custom_caption)

    if not media_items:
        full_text = "".join(t.get("caption", "") + "\n\n" for t in text_items) + custom_caption
        if not full_text.strip(): full_text = f"📚 <b>Pack: {course['course_id']}</b>\nPrice: ₹{course['amount']}"
        bot.send_message(chat_id, full_text.strip(), reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
    elif len(media_items) == 1:
        it = media_items[0]
        try:
            if it["type"] == "photo": bot.send_photo(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
            elif it["type"] == "video": bot.send_video(chat_id, it["file_id"], caption=final_cap, reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
        except Exception: pass
    else:
        media_group_html = []
        for i, item in enumerate(media_items):
            cap = final_cap if i == 0 else ""
            if item["type"] == "photo": media_group_html.append(InputMediaPhoto(item["file_id"], caption=cap, parse_mode="HTML"))
            elif item["type"] == "video": media_group_html.append(InputMediaVideo(item["file_id"], caption=cap, parse_mode="HTML"))
        try:
            sent_grp = orig_send_media_group(chat_id, media_group_html, protect_content=PROTECT_CONTENT)
            for m in sent_grp: register_activity(chat_id, m.message_id)
        except Exception: pass
        try: bot.send_message(chat_id, f"👆 <b>Choose an option to buy (₹{course['amount']}):</b>\n", reply_markup=markup, parse_mode="HTML")
        except Exception: pass

def send_batch_to_user(chat_id, batch):
    bot.send_message(chat_id, f"📦 <b>{batch['title']}</b>\nAll packs are listed below:", parse_mode="HTML")
    raw_cids = batch.get("course_ids", [])
    course_ids = json.loads(raw_cids) if isinstance(raw_cids, str) else raw_cids if isinstance(raw_cids, list) else []
    for cid in course_ids:
        c_data = courses_col.find_one({"course_id": cid})
        if c_data: send_course_to_user(chat_id, c_data)

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
    markup.row(InlineKeyboardButton("📦 Pack Batch (Multi-Pack)", callback_data="admin_create_batch"))
    markup.row(InlineKeyboardButton("🎟 Create Promo Offer", callback_data="admin_create_offer"))
    markup.row(InlineKeyboardButton("📋 Manage Store Plans", callback_data="admin_manage_plans"))
    markup.row(InlineKeyboardButton("🎨 Customize Start Menu", callback_data="admin_custom_menu"))
    markup.row(InlineKeyboardButton("🔗 Advanced File to Link", callback_data="admin_file_link"))
    markup.row(InlineKeyboardButton("📢 Advanced Broadcast", callback_data="admin_broadcast"))
    markup.row(InlineKeyboardButton("👥 User Info", callback_data="admin_user_info"))
    bot.send_message(chat_id, "🛠 <b>Admin Panel</b>\nPlease select an option:\n", reply_markup=markup, parse_mode="HTML")

# ==========================================
# COMMANDS & HANDLERS
# ==========================================
@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.chat.id
    if not check_rate_limit(user_id, 1): return
    register_activity(user_id, message.message_id)
    users_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id, "updated_at": get_ist_time()}}, upsert=True)
    param = message.text.split()[1].strip() if len(message.text.split()) > 1 else ""

    if param.startswith("off_"):
        offer = offers_col.find_one({"offer_code": param})
        if not offer:
            bot.send_message(user_id, "❌ <b>This offer is invalid or has expired.</b>", parse_mode="HTML")
            return send_custom_start_menu(user_id)
        now_ts = time.time()
        if offer.get("expires_at_ts") and now_ts > offer["expires_at_ts"]:
            bot.send_message(user_id, "⏳ <b>This offer has expired!</b>", parse_mode="HTML")
            return send_custom_start_menu(user_id)
        if offer.get("max_users", -1) != -1 and offer.get("used_count", 0) >= offer["max_users"]:
            bot.send_message(user_id, "⚠️ <b>This offer has reached its maximum claim limit!</b>", parse_mode="HTML")
            return send_custom_start_menu(user_id)
        per_user_limit = offer.get("per_user_limit", 1)
        if per_user_limit != -1 and get_offer_usage_count(user_id, offer["offer_code"]) >= per_user_limit:
            bot.send_message(user_id, "⚠️ <b>Aapne yeh offer pehle claim karke istemal kar liya hai.</b>\nYeh offer dobara claim nahi ho sakta.", parse_mode="HTML")
            return send_custom_start_menu(user_id)
        users_col.update_one({"user_id": user_id}, {"$set": {"active_offer_code": offer["offer_code"]}, "$unset": {"active_offer": ""}}, upsert=True)
        bot.send_message(user_id, f"🎉 <b>Congrats! {offer['discount_percent']}% discount activated!</b>", parse_mode="HTML")
        if offer["target_type"] == "single":
            c = courses_col.find_one({"course_id": offer["target_course_id"]})
            if c: send_course_to_user(user_id, c)
            else: send_custom_start_menu(user_id)
        else: send_custom_start_menu(user_id)
    elif param.startswith("b_"):
        batch = batches_col.find_one({"batch_id": param})
        if batch: send_batch_to_user(user_id, batch)
        else: bot.send_message(user_id, "❌ <b>This link has expired.</b>", parse_mode="HTML")
    elif param.startswith("c_"):
        course = courses_col.find_one({"course_id": param})
        if course: send_course_to_user(user_id, course)
        else: bot.send_message(user_id, "❌ <b>This link is not available.</b>", parse_mode="HTML")
    elif param.startswith("f_"):
        file_data = file_links_col.find_one({"file_code": param})
        if file_data:
            m_items, btns = file_data.get("media_data", []), file_data.get("button_data", [])
            markup = InlineKeyboardMarkup()
            for b in btns: markup.row(InlineKeyboardButton(b["text"], url=b["url"]))
            if len(m_items) == 1:
                it = m_items[0]
                try:
                    if it["type"] == "text": bot.send_message(user_id, it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
                    elif it["type"] == "photo": bot.send_photo(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
                    elif it["type"] == "video": bot.send_video(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
                    elif it["type"] == "document": bot.send_document(user_id, it["file_id"], caption=it["caption"], reply_markup=markup, parse_mode="HTML", protect_content=PROTECT_CONTENT)
                except Exception as e: bot.send_message(user_id, f"❌ Error: {e}")
            elif len(m_items) > 1:
                m_group = []
                for it in m_items:
                    if it["type"] == "photo": m_group.append(InputMediaPhoto(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                    elif it["type"] == "video": m_group.append(InputMediaVideo(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                    elif it["type"] == "document": m_group.append(InputMediaDocument(it["file_id"], caption=it["caption"], parse_mode="HTML"))
                try:
                    sent = orig_send_media_group(user_id, m_group, protect_content=PROTECT_CONTENT)
                    for m in sent: register_activity(user_id, m.message_id)
                    if btns or any(i["type"] == "text" for i in m_items): bot.send_message(user_id, "👇", reply_markup=markup, parse_mode="HTML")
                except Exception as e: bot.send_message(user_id, f"❌ Error: {e}")
        else: bot.send_message(user_id, "❌ <b>File not found or expired.</b>", parse_mode="HTML")
    else:
        if user_id == ADMIN_ID: send_admin_panel(user_id)
        else: send_custom_start_menu(user_id)

@bot.message_handler(content_types=["photo", "video", "document", "text"])
def handle_all_messages(message):
    user_id = message.chat.id
    if not check_rate_limit(user_id, 1): return
    register_activity(user_id, message.message_id)

    state = get_user_state(user_id)
    if state and state.get("step") == "WAITING_PAYMENT_SS":
        order_id = state.get("order_id")
        order = all_orders_cache.get(order_id) or orders_col.find_one({"order_id": order_id})
        if not message.photo and not message.document:
            bot.send_message(user_id, "❌ <b>Please send your screenshot as a photo or document.</b>", parse_mode="HTML")
            return
        fid = message.photo[-1].file_id if message.photo else message.document.file_id
        bot.send_message(user_id, "⏳ <b>Verification pending...</b>\nYour screenshot has been sent to admin.", parse_mode="HTML")
        clear_user_state(user_id)
        u_str = f"@{message.from_user.username}" if message.from_user.username else "No Username"
        u_men = f"<a href='tg://user?id={user_id}'>{message.from_user.first_name}</a> ({u_str})"
        
        course_id = order['course_id'] if order else 'N/A'
        course_obj = courses_col.find_one({"course_id": course_id}) if order else None
        ch_name_display = f"\n📺 <b>Channel:</b> {course_obj.get('channel_name')}" if course_obj and course_obj.get("channel_name") else ""

        cap = f"📩 <b>[MANUAL APPROVAL - PAYMENT SCREENSHOT]</b>\n\n👤 <b>User:</b> {u_men}\n🆔 <b>ID:</b> <code>{user_id}</code>\n🔖 <b>Order:</b> <code>{order_id}</code>\n📚 <b>Pack:</b> <code>{course_id}</code>{ch_name_display}\n💰 <b>Amount:</b> ₹{order['amount'] if order else 'N/A'}\n⏰ <b>Time:</b> {get_ist_time()}"
        
        c_url = f"https://t.me/{message.from_user.username}" if message.from_user.username else f"tg://user?id={user_id}"
        m_admin = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Approve", callback_data=f"man_appr_{order_id}"), InlineKeyboardButton("❌ Deny", callback_data=f"man_deny_{order_id}")).row(InlineKeyboardButton("💬 Chat with User", url=c_url))
        try:
            if message.photo: sent_admin_msg = orig_send_photo(DB_CHANNEL_ID, fid, caption=cap, reply_markup=m_admin, parse_mode="HTML")
            else: sent_admin_msg = orig_send_document(DB_CHANNEL_ID, fid, caption=cap, reply_markup=m_admin, parse_mode="HTML")
            orders_col.update_one({"order_id": order_id}, {"$set": {"manual_msg_id": sent_admin_msg.message_id}})
            if order: order["manual_msg_id"] = sent_admin_msg.message_id
        except Exception as e: orig_send_message(ADMIN_ID, f"❌ Channel error: {e}")
        return

    if user_id == ADMIN_ID and user_id in admin_data:
        step = admin_data[ADMIN_ID].get("step")
        if step == "DELETE_COURSE":
            cid = message.text.strip()
            if courses_col.delete_one({"course_id": cid}).deleted_count:
                settings_col.update_one({"_id": "store_plans"}, {"$pull": {"course_ids": cid}})
                bot.send_message(ADMIN_ID, f"✅ <b>Course <code>{cid}</code> Deleted!</b>", parse_mode="HTML")
            else: bot.send_message(ADMIN_ID, f"❌ <b>Not found.</b>", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            return send_admin_panel(ADMIN_ID)
        elif step == "ADD_PLAN_ID":
            cid = message.text.strip()
            if courses_col.find_one({"course_id": cid}):
                settings_col.update_one({"_id": "store_plans"}, {"$addToSet": {"course_ids": cid}}, upsert=True)
                bot.send_message(ADMIN_ID, f"✅ <b>Course <code>{cid}</code> added to Store Plans!</b>", parse_mode="HTML")
            else: bot.send_message(ADMIN_ID, f"❌ <b>Invalid ID.</b>", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            return send_admin_panel(ADMIN_ID)
        elif step == "OFFER_DISCOUNT":
            try:
                disc = int(re.sub(r"[^\d]", "", message.text.strip()))
                if not (1 <= disc <= 100): raise ValueError()
                admin_data[ADMIN_ID]["discount"], admin_data[ADMIN_ID]["step"] = disc, "OFFER_TARGET"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("🌐 All Courses", callback_data="offtarget_all")).row(InlineKeyboardButton("🎯 Single Course", callback_data="offtarget_single"))
                bot.send_message(ADMIN_ID, f"✅ Discount <b>{disc}%</b> set!\nApply to:", reply_markup=m, parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ 1 to 100 only.")
            return
        elif step == "OFFER_SINGLE_CID":
            cid = message.text.strip()
            if not courses_col.find_one({"course_id": cid}): return bot.send_message(ADMIN_ID, "❌ <b>Course ID not found. Send correct ID:</b>", parse_mode="HTML")
            admin_data[ADMIN_ID]["target_course_id"], admin_data[ADMIN_ID]["step"] = cid, "OFFER_LIMIT"
            bot.send_message(ADMIN_ID, "👥 <b>Max users?</b> (0 for unlimited):", parse_mode="HTML")
            return
        elif step == "OFFER_LIMIT":
            try:
                lim = int(re.sub(r"[^\d]", "", message.text.strip()))
                admin_data[ADMIN_ID]["max_users"], admin_data[ADMIN_ID]["step"] = -1 if lim == 0 else lim, "OFFER_PERUSER"
                bot.send_message(ADMIN_ID, "🔁 <b>Ek user kitni baar yeh offer claim/use kar sakta hai?</b>\n(<b>1</b> = sirf ek baar hi — recommended, taaki koi doosre course par offer reuse na kar sake. <b>0</b> = unlimited baar):", parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid number.")
            return
        elif step == "OFFER_PERUSER":
            try:
                pl = int(re.sub(r"[^\d]", "", message.text.strip()))
                admin_data[ADMIN_ID]["per_user_limit"], admin_data[ADMIN_ID]["step"] = -1 if pl == 0 else pl, "OFFER_HOURS"
                bot.send_message(ADMIN_ID, "⏳ <b>Active for how many hours?</b> (e.g. 24 or 48):", parse_mode="HTML")
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid number.")
            return
        elif step == "OFFER_HOURS":
            try:
                hrs = float(re.sub(r"[^\d.]", "", message.text.strip()))
                off_code, now_ts = "off_" + str(uuid.uuid4())[:6], time.time()
                doc = {
                    "offer_code": off_code, "discount_percent": admin_data[ADMIN_ID]["discount"],
                    "target_type": admin_data[ADMIN_ID]["target_type"], "target_course_id": admin_data[ADMIN_ID].get("target_course_id"),
                    "max_users": admin_data[ADMIN_ID]["max_users"], "per_user_limit": admin_data[ADMIN_ID].get("per_user_limit", 1), "used_count": 0, "created_at_ts": now_ts,
                    "expires_at_ts": now_ts + (hrs * 3600), "expires_str": (datetime.now(IST) + timedelta(hours=hrs)).strftime("%d-%m-%Y %I:%M %p")
                }
                offers_col.insert_one(doc)
                bot.send_message(ADMIN_ID, f"🎉 <b>Discount Offer Link Created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={off_code}</code>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            except Exception: bot.send_message(ADMIN_ID, "❌ Invalid hours.")
            return
        elif step == "MENU_CUSTOM_CONTENT":
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            admin_data[ADMIN_ID]["menu_content"] = {"media_type": mt, "file_id": fid, "text": get_formatted_text(message)}
            admin_data[ADMIN_ID]["buttons"], admin_data[ADMIN_ID]["step"] = [], "MENU_ADD_BUTTONS"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save", callback_data="menu_finish_save"))
            bot.send_message(ADMIN_ID, "✅ <b>Content Saved!</b>\nAdd buttons: <code>Button Name - Link</code> or Finish.", reply_markup=m, parse_mode="HTML")
            return
        elif step == "MENU_ADD_BUTTONS":
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID]["buttons"].append({"text": t.strip(), "url": u.strip()})
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Save Menu", callback_data="menu_finish_save"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! ({len(admin_data[ADMIN_ID]['buttons'])})</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format error. <code>Name - Link</code>", parse_mode="HTML")
            return
        elif step == "PROMO":
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            admin_data[ADMIN_ID]["promo"].append({"type": mt, "file_id": fid, "caption": get_formatted_text(message)})
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step (Price)", callback_data="next_price"))
            bot.send_message(ADMIN_ID, f"✅ <b>{mt.capitalize()} saved!</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "AMOUNT":
            amt = re.sub(r"[^\d.]", "", message.text.strip())
            if not amt: return bot.send_message(ADMIN_ID, "❌ <b>Numbers only.</b>", parse_mode="HTML")
            admin_data[ADMIN_ID]["amount"], admin_data[ADMIN_ID]["step"] = amt, "CAPTION"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip (No Caption)", callback_data="skip_caption"))
            bot.send_message(ADMIN_ID, f"✅ <b>Price ₹{amt} saved!</b>\n📝 Type optional extra caption, or skip:", reply_markup=m, parse_mode="HTML")
            return
        elif step == "CAPTION":
            admin_data[ADMIN_ID]["caption"], admin_data[ADMIN_ID]["step"] = get_formatted_text(message), "COURSE_TYPE"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("📝 Text / Secret Link", callback_data="ctype_text"), InlineKeyboardButton("📢 Private Channel/Group", callback_data="ctype_channel"))
            bot.send_message(ADMIN_ID, "✅ <b>Caption saved!</b>\nWhat will the user get after payment?", reply_markup=m, parse_mode="HTML")
            return
        elif step == "SECRET":
            cid = "c_" + str(uuid.uuid4())[:6]
            courses_col.update_one({"course_id": cid}, {"$set": {"course_id": cid, "promo_media": admin_data[ADMIN_ID]["promo"], "amount": admin_data[ADMIN_ID]["amount"], "custom_caption": admin_data[ADMIN_ID].get("caption",""), "secret_text": get_formatted_text(message)}}, upsert=True)
            if admin_data[ADMIN_ID].get("mode") == "single":
                bot.send_message(ADMIN_ID, f"🎉 <b>Pack created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={cid}</code>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            elif admin_data[ADMIN_ID].get("mode") == "batch":
                admin_data[ADMIN_ID]["course_ids"].append(cid)
                admin_data[ADMIN_ID]["step"] = "NEXT_ACTION"
                m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Another", callback_data="batch_add_next")).row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                bot.send_message(ADMIN_ID, f"✅ <b>Pack saved!</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step == "CHANNEL_ID":
            channel_id = None
            if message.forward_from_chat:
                channel_id = message.forward_from_chat.id
            elif message.text:
                text = message.text.strip()
                match = re.search(r"t\.me/c/(\d+)", text)
                if match:
                    channel_id = int(f"-100{match.group(1)}")
                elif text.startswith("-100") and text.replace("-", "").isdigit():
                    channel_id = int(text)
            
            if not channel_id:
                return bot.send_message(ADMIN_ID, "❌ कृपया किसी Private Channel/Group की लिंक भेजें (जैसे https://t.me/c/123456.../1) या मैसेज फॉरवर्ड करें।")
            
            try:
                chat_info = bot.get_chat(channel_id)
                channel_name = chat_info.title if chat_info.title else "Private Channel"

                link = bot.create_chat_invite_link(channel_id, creates_join_request=True)
                secret_text = f"👉 <b>Click here to join the Group/Channel:</b>\n{link.invite_link}"
                cid = "c_" + str(uuid.uuid4())[:6]
                
                courses_col.update_one({"course_id": cid}, {"$set": {
                    "course_id": cid, "promo_media": admin_data[ADMIN_ID]["promo"], "amount": admin_data[ADMIN_ID]["amount"],
                    "custom_caption": admin_data[ADMIN_ID].get("caption", ""), "secret_text": secret_text, "channel_id": channel_id,
                    "channel_name": channel_name
                }}, upsert=True)
                
                if admin_data[ADMIN_ID].get("mode") == "single":
                    bot.send_message(ADMIN_ID, f"🎉 <b>Channel Pack created! ({channel_name})</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={cid}</code>", parse_mode="HTML")
                    del admin_data[ADMIN_ID]
                    send_admin_panel(ADMIN_ID)
                elif admin_data[ADMIN_ID].get("mode") == "batch":
                    admin_data[ADMIN_ID]["course_ids"].append(cid)
                    admin_data[ADMIN_ID]["step"] = "NEXT_ACTION"
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Another", callback_data="batch_add_next")).row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Pack saved!</b>", reply_markup=m, parse_mode="HTML")
            except Exception as e:
                bot.send_message(ADMIN_ID, f"❌ Error: Make sure the bot is an Admin in the Channel/Group first! ({e})")
            return
        elif step == "TITLE":
            admin_data[ADMIN_ID]["title"], admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["promo"] = message.text.strip(), "PROMO", []
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price"))
            bot.send_message(ADMIN_ID, f"✅ Title saved. <b>Send promo media/text:</b>", reply_markup=m, parse_mode="HTML")
            return
        elif step in ["BC_MEDIA", "FTL_MEDIA"]:
            mt, fid = "text", None
            if message.photo: mt, fid = "photo", message.photo[-1].file_id
            elif message.video: mt, fid = "video", message.video.file_id
            elif message.document: mt, fid = "document", message.document.file_id
            admin_data[ADMIN_ID].setdefault("media", []).append({"type": mt, "file_id": fid, "caption": get_formatted_text(message)})
            cb = "bc_done" if step == "BC_MEDIA" else "ftl_done"
            m = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done Adding", callback_data=cb))
            bot.send_message(ADMIN_ID, "✅ <b>Saved!</b> Send another or click Done.", reply_markup=m, parse_mode="HTML")
            return
        elif step in ["BC_BUTTONS", "FTL_BUTTONS"]:
            txt = message.text.strip()
            if " - " in txt:
                try:
                    t, u = txt.split(" - ", 1)
                    admin_data[ADMIN_ID].setdefault("buttons", []).append({"text": t.strip(), "url": u.strip()})
                    cb = "bc_finish" if step == "BC_BUTTONS" else "ftl_finish"
                    m = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data=cb))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added!</b>", reply_markup=m, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format Error.", parse_mode="HTML")
            return

@bot.callback_query_handler(func=lambda call: True)
def handle_buttons(call):
    data, chat_id, msg_id = call.data, call.message.chat.id, call.message.message_id
    if not check_rate_limit(chat_id, 1.5): 
        return bot.answer_callback_query(call.id, "⚠️ थोड़ा धीमे! (Slow down)", show_alert=False)
    register_activity(chat_id)

    if data == "user_view_plans":
        bot.answer_callback_query(call.id)
        plans = settings_col.find_one({"_id": "store_plans"})
        c_ids = plans.get("course_ids", []) if plans else [c["course_id"] for c in courses_col.find().limit(10)]
        if not c_ids: return bot.send_message(chat_id, "ℹ️ No plans available.", parse_mode="HTML")
        bot.send_message(chat_id, "📚 <b>Available Plans:</b>", parse_mode="HTML")
        for cid in c_ids:
            c = courses_col.find_one({"course_id": cid})
            if c: send_course_to_user(chat_id, c)
        return
    if data == "ctype_text":
        admin_data[ADMIN_ID]["step"] = "SECRET"
        bot.edit_message_text("✅ <b>Send final secret link or text content:</b>", chat_id, msg_id, parse_mode="HTML")
        return
    if data == "ctype_channel":
        admin_data[ADMIN_ID]["step"] = "CHANNEL_ID"
        bot.edit_message_text("📢 <b>Forward a message OR send a Private Post Link (e.g. https://t.me/c/123...):</b>\n<i>(Make sure I am an Admin in that Channel/Group first!)</i>", chat_id, msg_id, parse_mode="HTML")
        return
    if data == "skip_caption" and ADMIN_ID in admin_data:
        admin_data[ADMIN_ID]["caption"], admin_data[ADMIN_ID]["step"] = "", "COURSE_TYPE"
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("📝 Text / Secret Link", callback_data="ctype_text"), InlineKeyboardButton("📢 Private Channel/Group", callback_data="ctype_channel"))
        bot.edit_message_text("✅ <b>What will the user get after payment?</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
        return
    if data == "admin_create_offer":
        bot.answer_callback_query(call.id)
        admin_data[ADMIN_ID] = {"step": "OFFER_DISCOUNT"}
        bot.edit_message_text("🎟 <b>Create Discount Offer:</b>\nEnter % (e.g. 50):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return
    elif data == "offtarget_all":
        bot.answer_callback_query(call.id)
        admin_data[ADMIN_ID]["target_type"], admin_data[ADMIN_ID]["step"] = "all", "OFFER_LIMIT"
        bot.edit_message_text("👥 <b>Max claims?</b> (0 = unlimited):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return
    elif data == "offtarget_single":
        bot.answer_callback_query(call.id)
        admin_data[ADMIN_ID]["target_type"], admin_data[ADMIN_ID]["step"] = "single", "OFFER_SINGLE_CID"
        bot.edit_message_text("🎯 <b>Send Course ID:</b> (e.g. <code>c_abc123</code>)", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return
    if data == "admin_delete_course":
        bot.answer_callback_query(call.id)
        admin_data[ADMIN_ID] = {"step": "DELETE_COURSE"}
        bot.edit_message_text("🗑 <b>Delete Course:</b>\nSend Course ID:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return
    if data == "admin_manage_plans":
        bot.answer_callback_query(call.id)
        c_ids = (settings_col.find_one({"_id": "store_plans"}) or {}).get("course_ids", [])
        text = f"📋 <b>Manage Store Plans ({len(c_ids)}):</b>\n" + "".join(f"• <code>{cid}</code>\n" for cid in c_ids)
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("➕ Add Course", callback_data="plan_add_id")).row(InlineKeyboardButton("🗑 Clear All", callback_data="plan_clear_all")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
        return
    elif data == "plan_add_id":
        bot.answer_callback_query(call.id)
        admin_data[ADMIN_ID] = {"step": "ADD_PLAN_ID"}
        bot.edit_message_text("➕ Send <b>Course ID</b> to add:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return
    elif data == "plan_clear_all":
        settings_col.delete_one({"_id": "store_plans"})
        bot.answer_callback_query(call.id, "All plans cleared!", show_alert=True)
        return send_admin_panel(chat_id)
    if data == "admin_custom_menu":
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("✏️ Set New", callback_data="menu_set_new")).row(InlineKeyboardButton("🗑 Reset Default", callback_data="menu_reset_default")).row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("🎨 <b>Customize Start Menu</b>", chat_id=chat_id, message_id=msg_id, reply_markup=m, parse_mode="HTML")
        return
    elif data == "menu_set_new":
        admin_data[ADMIN_ID] = {"step": "MENU_CUSTOM_CONTENT"}
        bot.edit_message_text("📝 <b>Start Menu Content</b>\nSend Photo, Video or Text:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return
    elif data == "menu_finish_save":
        c = admin_data.get(ADMIN_ID, {}).get("menu_content", {})
        settings_col.update_one({"_id": "start_menu"}, {"$set": {"media_type": c.get("media_type", "text"), "file_id": c.get("file_id"), "text": c.get("text", ""), "buttons": admin_data.get(ADMIN_ID, {}).get("buttons", []), "updated_at": get_ist_time()}}, upsert=True)
        del admin_data[ADMIN_ID]
        bot.edit_message_text("🎉 <b>Saved!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return send_admin_panel(chat_id)
    elif data == "menu_reset_default":
        settings_col.delete_one({"_id": "start_menu"})
        bot.edit_message_text("✅ <b>Reset!</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        return send_admin_panel(chat_id)
        
    if data.startswith("paydone_"):
        oid = data.replace("paydone_", "")
        order = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
        if not order: return bot.answer_callback_query(call.id, "❌ Order not found.", show_alert=True)
        if order.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"): return bot.answer_callback_query(call.id, "✅ Already delivered.", show_alert=True)
        
        bot.answer_callback_query(call.id, "⏳ Checking...", show_alert=False)
        amt_key = order.get("amount")
        sms_rec = sms_pool_col.find_one({"amount": amt_key, "status": "UNUSED"})
        if sms_rec:
            updated = sms_pool_col.update_one({"_id": sms_rec["_id"], "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
            if updated.modified_count > 0:
                deliver_course_to_buyer(order, sms_text=sms_rec.get("raw_text"), is_manual=False)
                return
            
        try: bot.delete_message(chat_id, msg_id)
        except Exception: pass
        
        set_user_state(chat_id, "WAITING_PAYMENT_SS", oid)
        try: 
            prompt_msg = bot.send_message(chat_id, "📸 <b>अपना पेमेंट स्क्रीनशॉट यहाँ भेजें।</b>\n⏳ <i>(कृपया 10 मिनट के अंदर भेजें, अन्यथा यह फेल हो जाएगा)</i>", parse_mode="HTML")
            threading.Timer(600, screenshot_timeout, args=(chat_id, oid, prompt_msg.message_id)).start()
        except Exception: pass
        return
        
    if data.startswith("send_ss_"):
        bot.answer_callback_query(call.id)
        set_user_state(chat_id, "WAITING_PAYMENT_SS", data.replace("send_ss_", ""))
        bot.send_message(chat_id, "📸 <b>Please send your payment screenshot.</b>", parse_mode="HTML")
        return
    if data.startswith("man_appr_"):
        oid = data.replace("man_appr_", "")
        o = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
        if not o: return bot.answer_callback_query(call.id, "❌ Order not found.", show_alert=True)
        if o.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"):
            bot.answer_callback_query(call.id, "ℹ️ Already delivered via SMS.", show_alert=True)
            try: bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            except Exception: pass
            return
        deliver_course_to_buyer(o, sms_text="Manual Approval", is_manual=True)
        bot.answer_callback_query(call.id, "✅ Approved!", show_alert=True)
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            orig_send_message(chat_id, f"✅ <b>ORDER {oid} APPROVED</b>\n⏰ {get_ist_time()}", reply_to_message_id=msg_id, parse_mode="HTML")
        except Exception: pass
        return
    if data.startswith("man_deny_"):
        oid = data.replace("man_deny_", "")
        o = all_orders_cache.get(oid) or orders_col.find_one({"order_id": oid})
        if not o: return bot.answer_callback_query(call.id, "❌ Not found.", show_alert=True)
        if o.get("status") in ("COMPLETED_AUTO", "COMPLETED_MANUAL"):
            bot.answer_callback_query(call.id, "ℹ️ Already delivered.", show_alert=True)
            try: bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            except Exception: pass
            return
        m = InlineKeyboardMarkup().row(InlineKeyboardButton("💬 Contact Admin", url=CHAT_LINK)) if CHAT_LINK else None
        try: bot.send_message(o["chat_id"], f"❌ <b>Payment Failed!</b>\nOrder <code>{oid}</code> rejected.", reply_markup=m, parse_mode="HTML")
        except Exception: pass
        bot.answer_callback_query(call.id, "❌ Rejected.", show_alert=True)
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            orig_send_message(chat_id, f"❌ <b>ORDER {oid} REJECTED</b>", reply_to_message_id=msg_id, parse_mode="HTML")
        except Exception: pass
        return
    if data.startswith("pay_upi_"):
        bot.answer_callback_query(call.id, "⏳ Generating Fresh QR...", show_alert=False)
        course_id = data.replace("pay_upi_", "")
        course = courses_col.find_one({"course_id": course_id})
        if course:
            state = get_user_state(chat_id)
            if state and state.get("step") == "PENDING_UPI":
                with pending_lock: pending_orders.pop(state.get("amount_key", ""), None)
                if chat_id in user_qr_messages:
                    try: bot.delete_message(chat_id, user_qr_messages.pop(chat_id))
                    except Exception: pass
            
            base_price = float(course["amount"])
            u_rec = users_col.find_one({"user_id": call.from_user.id}) or {}
            active_off_code = u_rec.get("active_offer_code") or (u_rec.get("active_offer") or {}).get("offer_code")
            disc_pct, off_code, final_base = None, None, base_price
            if active_off_code:
                live_offer = offers_col.find_one({"offer_code": active_off_code})
                is_valid, _reason = check_offer_validity(live_offer, call.from_user.id, course_id)
                if is_valid:
                    disc_pct, off_code = live_offer["discount_percent"], live_offer["offer_code"]
                    final_base = round(base_price * (1.0 - (disc_pct / 100.0)), 2)
                else:
                    clear_user_offer(call.from_user.id, active_off_code)
                    
            order_id, amt_key = str(uuid.uuid4())[:8], generate_unique_amount(final_base)
            u_men = f"<a href='tg://user?id={call.from_user.id}'>{call.from_user.first_name}</a> (@{call.from_user.username or ''})"
            o_data = {
                "order_id": order_id, "course_id": course_id, "user_id": call.from_user.id, "chat_id": chat_id,
                "user_mention": u_men, "amount": amt_key, "original_amount": str(base_price), "discount_percent": disc_pct,
                "offer_id": off_code, "status": "PENDING", "created_at_str": get_ist_time(), "created_at": time.time(), "created_at_dt": datetime.now(timezone.utc), "channel_msg_id": None
            }
            d_log = f"\n🎟 <b>Offer Applied:</b> {disc_pct}% OFF" if disc_pct else ""
            
            ch_name_display = f"\n📺 <b>Channel:</b> {course.get('channel_name')}" if course.get("channel_name") else f"\n📚 <b>Pack:</b> <code>{course_id}</code>"
            ch_txt = f"🟡 <b>[ORDER INITIATED - QR]</b>\n\n👤 <b>User:</b> {u_men}\n🔖 <b>Order:</b> <code>{order_id}</code>{ch_name_display}\n💰 <b>Amount:</b> ₹{amt_key}{d_log}\n⏳ <b>Status:</b> ⏳ पेंडिंग"
            
            try:
                ch_msg = bot.send_message(DB_CHANNEL_ID, ch_txt, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("💬 Chat", url=f"tg://user?id={call.from_user.id}")), parse_mode="HTML")
                o_data["channel_msg_id"] = ch_msg.message_id
            except Exception: pass
            orders_col.insert_one(o_data.copy())
            with pending_lock:
                pending_orders[amt_key] = o_data
                all_orders_cache[order_id] = o_data
                
            set_user_state(chat_id, "PENDING_UPI", order_id, amt_key)

            sms_rec = sms_pool_col.find_one({"amount": amt_key, "status": "UNUSED"})
            if sms_rec:
                updated = sms_pool_col.update_one({"_id": sms_rec["_id"], "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
                if updated.modified_count > 0:
                    return deliver_course_to_buyer(o_data, sms_text=sms_rec.get("raw_text"), is_manual=False)

            qr_img_bio, clean_amt = generate_upi_qr(amt_key, order_id)
            inv = f"👤 <b>User:</b> {call.from_user.first_name}\n🆔 <b>Order:</b> <code>{order_id}</code>\n💰 <b>Amount:</b> ₹{clean_amt}\n⚠️ <b>Exact Amount Pay Karein.</b>\n⏳ <i>QR {QR_EXPIRY_SECONDS // 60} min mein expire hoga.</i>"
            m = InlineKeyboardMarkup()
            if CHAT_LINK: m.row(InlineKeyboardButton("💬 Chat with Admin", url=CHAT_LINK))
            sent_msg = bot.send_photo(chat_id, photo=qr_img_bio, caption=inv, reply_markup=m, parse_mode="HTML")
            user_qr_messages[chat_id] = sent_msg.message_id
            orders_col.update_one({"order_id": order_id}, {"$set": {"qr_msg_id": sent_msg.message_id}})
            threading.Timer(QR_EXPIRY_SECONDS, expire_qr, args=(chat_id, sent_msg.message_id, course_id, amt_key, order_id)).start()
        return

    bot.answer_callback_query(call.id)
    if data == "admin_add_course":
        admin_data[ADMIN_ID] = {"mode": "single", "step": "PROMO", "promo": [], "amount": None, "caption": ""}
        bot.edit_message_text("📝 <b>Step 1/4: Promo Media OR Text</b>", chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next", callback_data="next_price")), parse_mode="HTML")
    elif data == "admin_create_batch":
        admin_data[ADMIN_ID] = {"mode": "batch", "step": "TITLE", "course_ids": []}
        bot.edit_message_text("📦 <b>Create Pack Batch</b>\nSend Title:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data in ["admin_file_link", "admin_broadcast"]:
        admin_data[ADMIN_ID] = {"step": "FTL_MEDIA" if data == "admin_file_link" else "BC_MEDIA", "media": []}
        bot.edit_message_text(f"{'📎 File to Link' if data == 'admin_file_link' else '📢 Broadcast'}\nSend Media/Text.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "next_price" and ADMIN_ID in admin_data:
        admin_data[ADMIN_ID]["step"] = "AMOUNT"
        bot.edit_message_text("💰 <b>Step 2/4: Price (INR)</b>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "batch_add_next":
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["promo"], admin_data[ADMIN_ID]["caption"] = "PROMO", [], ""
        bot.edit_message_text("📝 <b>Send promo for next pack:</b>", chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next", callback_data="next_price")), parse_mode="HTML")
    elif data == "batch_finish":
        d = admin_data.get(ADMIN_ID)
        if d and d.get("course_ids"):
            bid = "b_" + str(uuid.uuid4())[:6]
            batches_col.update_one({"batch_id": bid}, {"$set": {"batch_id": bid, "title": d["title"], "course_ids": d["course_ids"]}}, upsert=True)
            bot.edit_message_text(f"🎉 <b>Batch Created!</b>\n👉 <code>https://t.me/{BOT_USERNAME}?start={bid}</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
            del admin_data[ADMIN_ID]
            send_admin_panel(ADMIN_ID)
    elif data == "admin_user_info":
        recs = list(purchases_col.find().sort("_id", -1).limit(15))
        txt = "👥 <b>Recent Purchases:</b>\n\n" + "".join(f"👤 {r.get('username')} | 📅 {r.get('date', '')[:10]} | 📚 <code>{r.get('item_info')}</code>\n" for r in recs) if recs else "No purchases yet."
        bot.edit_message_text(txt, chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin")), parse_mode="HTML")
    elif data == "back_to_admin":
        try: bot.delete_message(chat_id, msg_id)
        except Exception: pass
        send_admin_panel(chat_id)
    elif data.startswith("mainmenu_"):
        t = data.replace("mainmenu_", "")
        if t.startswith("c_"):
            c = courses_col.find_one({"course_id": t})
            if c: send_course_to_user(chat_id, c)
        elif t.startswith("b_"):
            b = batches_col.find_one({"batch_id": t})
            if b: send_batch_to_user(chat_id, b)
    elif data == "bc_done":
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["buttons"] = "BC_BUTTONS", []
        bot.send_message(ADMIN_ID, "✅ <b>Media Saved!</b> Add button or Finish.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data="bc_finish")), parse_mode="HTML")
    elif data == "bc_finish":
        m_items, btns = admin_data[ADMIN_ID].get("media", []), admin_data[ADMIN_ID].get("buttons", [])
        m = InlineKeyboardMarkup()
        for b in btns: m.row(InlineKeyboardButton(b["text"], url=b["url"]))
        bot.send_message(ADMIN_ID, "⏳ Broadcasting started...")
        success = 0
        for u in users_col.find():
            uid = u["user_id"]
            try:
                if not m_items: continue
                if len(m_items) == 1:
                    it = m_items[0]
                    if it["type"] == "text": bot.send_message(uid, it["caption"], reply_markup=m, parse_mode="HTML")
                    elif it["type"] == "photo": bot.send_photo(uid, it["file_id"], caption=it["caption"], reply_markup=m, parse_mode="HTML")
                    elif it["type"] == "video": bot.send_video(uid, it["file_id"], caption=it["caption"], reply_markup=m, parse_mode="HTML")
                    elif it["type"] == "document": bot.send_document(uid, it["file_id"], caption=it["caption"], reply_markup=m, parse_mode="HTML")
                else:
                    m_group = [InputMediaPhoto(it["file_id"], caption=it["caption"], parse_mode="HTML") if it["type"] == "photo" else InputMediaVideo(it["file_id"], caption=it["caption"], parse_mode="HTML") if it["type"] == "video" else InputMediaDocument(it["file_id"], caption=it["caption"], parse_mode="HTML") for it in m_items]
                    sent = orig_send_media_group(uid, m_group)
                    if btns or any(i["type"] == "text" for i in m_items): bot.send_message(uid, "👇", reply_markup=m, parse_mode="HTML")
                success += 1
                time.sleep(0.05)
            except Exception: pass
        bot.send_message(ADMIN_ID, f"✅ <b>Broadcast Complete!</b> ({success} users).", parse_mode="HTML")
        del admin_data[ADMIN_ID]
        send_admin_panel(ADMIN_ID)
    elif data == "ftl_done":
        admin_data[ADMIN_ID]["step"], admin_data[ADMIN_ID]["buttons"] = "FTL_BUTTONS", []
        bot.send_message(ADMIN_ID, "✅ <b>Media Saved!</b> Add button or Finish.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish", callback_data="ftl_finish")), parse_mode="HTML")
    elif data == "ftl_finish":
        fid = "f_" + str(uuid.uuid4())[:6]
        file_links_col.update_one({"file_code": fid}, {"$set": {"file_code": fid, "media_data": admin_data[ADMIN_ID].get("media", []), "button_data": admin_data[ADMIN_ID].get("buttons", [])}}, upsert=True)
        bot.send_message(ADMIN_ID, f"🎉 <b>Link Created!</b>\n👉 <code>https://t.me/{BOT_USERNAME}?start={fid}</code>", parse_mode="HTML")
        del admin_data[ADMIN_ID]
        send_admin_panel(ADMIN_ID)

# ==========================================
# FLASK WEB SERVER & API
# ==========================================
app = Flask(__name__)
AMOUNT_RE_DECIMAL = re.compile(r"(?:Rs\.?|₹|INR)\s?([\d,]+\.\d{2})", re.IGNORECASE)
AMOUNT_RE_INT = re.compile(r"(?:Rs\.?|₹|INR)\s?([\d,]+)(?!\.\d)", re.IGNORECASE)
BOT_USERNAME = "your_bot" 

@app.route("/")
def home(): return "Telegram Bot API Running."

@app.route("/sms-webhook/<secret>", methods=["GET", "POST"])
def sms_webhook(secret):
    if secret != SMS_HOOK_SECRET: return "forbidden", 403
    sms_text = (request.get_json(silent=True) or request.form).get("text", "").strip() if request.method == "POST" else request.args.get("text", "").strip()
    if not sms_text: return "no 'text' param", 400
    m = AMOUNT_RE_DECIMAL.search(sms_text)
    has_dec = bool(m)
    if not m: m = AMOUNT_RE_INT.search(sms_text)
    if not m: return "no amount", 200

    amt_str = m.group(1).replace(",", "")
    f_round = f"{float(amt_str):.2f}" if not has_dec else amt_str
    sms_pool_col.insert_one({"amount": f_round, "raw_text": sms_text, "status": "UNUSED", "created_at_dt": datetime.now(timezone.utc), "created_at_str": get_ist_time()})

    order, amb = None, False
    with pending_lock:
        cands = [amt_str] if has_dec and amt_str in pending_orders else [f_round] if not has_dec and f_round in pending_orders else [k for k in pending_orders if not has_dec and k.startswith(amt_str + ".")]
        if len(cands) == 1: order = pending_orders.pop(cands[0])
        else: amb = len(cands) > 1

    if not order:
        stale_cutoff = time.time() - STALE_ORDER_SECONDS
        order = orders_col.find_one({"amount": f_round, "status": {"$in": ["PENDING", "EXPIRED"]}, "created_at": {"$gte": stale_cutoff}})
    
    if order:
        updated = sms_pool_col.update_one({"amount": f_round, "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
        if updated.modified_count > 0:
            deliver_course_to_buyer(order, sms_text=sms_text, is_manual=False)
        return "matched", 200
    if amb:
        try: orig_send_message(DB_CHANNEL_ID, f"⚠️ <b>Ambiguous</b> ₹{amt_str}\n📩 <code>{sms_text[:300]}</code>", parse_mode="HTML")
        except Exception: pass
        return "ambiguous", 200
    return "saved_to_pool", 200

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USERNAME or auth.password != DASHBOARD_PASSWORD:
            return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Dashboard"'})
        return f(*args, **kwargs)
    return wrapper

def _strip_html(s): return re.sub(r"<[^>]*>", "", s or "").strip()
def _order_ts(o):
    if o.get("delivered_at_ts"): return o["delivered_at_ts"]
    ds = o.get("delivered_at")
    if ds:
        try: return datetime.strptime(ds, "%d-%m-%Y %I:%M:%S %p").replace(tzinfo=IST).timestamp()
        except Exception: pass
    return o.get("created_at", 0)

@app.route("/dashboard/api/overview")
@require_auth
def api_overview():
    completed_q = {"status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}}
    today_start = (datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)).timestamp()
    today_amt = sum(float(o.get("amount", 0) or 0) for o in orders_col.find(completed_q) if _order_ts(o) >= today_start)
    
    return jsonify({
        "total_users": users_col.count_documents({}), "total_qr_generated": orders_col.count_documents({}),
        "active_pending": orders_col.count_documents({"status": "PENDING"}), "completed_total": orders_col.count_documents(completed_q),
        "expired_total": orders_col.count_documents({"status": "EXPIRED"}), "pending_sms": sms_pool_col.count_documents({"status": "UNUSED"}),
        "today_amount": round(today_amt, 2)
    })

@app.route("/dashboard/api/orders")
@require_auth
def api_orders():
    st = request.args.get("status", "all")
    q = {"status": "PENDING"} if st == "pending" else {"status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}} if st == "completed" else {"status": "EXPIRED"} if st == "expired" else {}
    now = time.time()
    out = []
    for o in orders_col.find(q).sort("created_at", -1).limit(300):
        remaining = max(0, int(QR_EXPIRY_SECONDS - (now - o.get("created_at", now)))) if o.get("status") == "PENDING" else None
        out.append({
            "order_id": o.get("order_id"), "user": _strip_html(o.get("user_mention", "")), "course_id": o.get("course_id"), 
            "amount": o.get("amount"), "status": o.get("status"), "created_at": o.get("created_at_str"),
            "remaining_seconds": remaining, "method": {"COMPLETED_AUTO": "Auto SMS", "COMPLETED_MANUAL": "Manual"}.get(o.get("status")),
            "has_screenshot": bool(o.get("manual_msg_id"))
        })
    return jsonify(out)

@app.route("/dashboard/api/orders/<order_id>/approve", methods=["POST"])
@require_auth
def api_approve_order(order_id):
    order = orders_col.find_one({"order_id": order_id})
    if order and order.get("status") in ["PENDING", "EXPIRED"]:
        deliver_course_to_buyer(order, sms_text="Dashboard Approved", is_manual=True)
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route("/dashboard/api/courses", methods=["GET"])
@require_auth
def api_courses():
    out = []
    for c in courses_col.find().sort("_id", -1):
        out.append({"course_id": c.get("course_id"), "amount": c.get("amount"), "caption": c.get("custom_caption", "")[:40], "is_channel": bool(c.get("channel_id"))})
    return jsonify(out)

@app.route("/dashboard/api/courses/<course_id>", methods=["DELETE"])
@require_auth
def api_delete_course(course_id):
    courses_col.delete_one({"course_id": course_id})
    settings_col.update_one({"_id": "store_plans"}, {"$pull": {"course_ids": course_id}})
    return jsonify({"status": "success"})

@app.route("/dashboard/api/courses/<course_id>/buyers")
@require_auth
def api_course_buyers(course_id):
    course = courses_col.find_one({"course_id": course_id})
    is_channel = bool(course and course.get("channel_id"))
    completed_q = {"course_id": course_id, "status": {"$in": ["COMPLETED_AUTO", "COMPLETED_MANUAL"]}}
    out = []
    for o in orders_col.find(completed_q).sort("delivered_at_ts", -1):
        uid = o.get("user_id")
        joined = None
        if is_channel:
            joined = channel_logs_col.count_documents({"user_id": uid, "course_id": course_id, "status": "APPROVED"}) > 0
        out.append({
            "user_id": uid, "user": _strip_html(o.get("user_mention", f"User ({uid})")),
            "amount": o.get("amount"), "purchased_at": o.get("delivered_at") or o.get("created_at_str"),
            "method": {"COMPLETED_AUTO": "Auto SMS", "COMPLETED_MANUAL": "Manual"}.get(o.get("status")),
            "channel_joined": joined
        })
    return jsonify({"course_id": course_id, "is_channel": is_channel, "channel_name": (course or {}).get("channel_name"), "buyers": out})

@app.route("/dashboard/api/offers", methods=["GET", "POST"])
@require_auth
def api_offers():
    if request.method == "POST":
        d = request.json
        off_code = "off_" + str(uuid.uuid4())[:6]
        now_ts = time.time()
        hrs = float(d.get("hours", 24))
        doc = {
            "offer_code": off_code, "discount_percent": int(d.get("discount", 10)),
            "target_type": d.get("target_type", "all"), "target_course_id": d.get("course_id", ""),
            "max_users": int(d.get("max_users", -1)), "per_user_limit": int(d.get("per_user_limit", 1)), "used_count": 0,
            "created_at_ts": now_ts, "expires_at_ts": now_ts + (hrs * 3600),
            "expires_str": (datetime.now(IST) + timedelta(hours=hrs)).strftime("%d-%m-%Y %I:%M %p")
        }
        offers_col.insert_one(doc)
        return jsonify({"status": "success"})
    
    out = []
    now = time.time()
    for o in offers_col.find().sort("created_at_ts", -1):
        status = "Active" if o["expires_at_ts"] > now and (o["max_users"] == -1 or o["used_count"] < o["max_users"]) else "Expired"
        link = f"https://t.me/{BOT_USERNAME}?start={o['offer_code']}"
        out.append({"offer_code": o["offer_code"], "discount": o["discount_percent"], "target": o["target_type"], "course": o["target_course_id"], "used": o["used_count"], "max": o["max_users"], "per_user": o.get("per_user_limit", 1), "expires": o["expires_str"], "status": status, "link": link})
    return jsonify(out)

@app.route("/dashboard/api/offers/<offer_code>", methods=["DELETE"])
@require_auth
def api_delete_offer(offer_code):
    offers_col.delete_one({"offer_code": offer_code})
    return jsonify({"status": "success"})

@app.route("/dashboard/api/broadcast", methods=["POST"])
@require_auth
def api_broadcast():
    msg = request.json.get("message")
    btns = request.json.get("buttons", [])
    if not msg: return jsonify({"error": "Empty message"}), 400
    
    markup = telebot.types.InlineKeyboardMarkup()
    for b in btns:
        if b.get("text") and b.get("url"):
            markup.add(telebot.types.InlineKeyboardButton(b["text"], url=b["url"]))
    if not markup.keyboard: markup = None

    def run_bc():
        for u in users_col.find():
            try: 
                bot.send_message(u["user_id"], msg, reply_markup=markup, parse_mode="HTML")
                time.sleep(0.05)
            except Exception: pass
    threading.Thread(target=run_bc).start()
    return jsonify({"status": "success"})

@app.route("/dashboard/api/channel-logs")
@require_auth
def api_channel_logs():
    return jsonify([{
        "user_id": l["user_id"], 
        "first_name": l.get("first_name", "Unknown"),
        "username": l.get("username", "None"),
        "course": l["course_id"], 
        "channel_name": l.get("channel_name", "Unknown Channel"), 
        "status": l["status"], 
        "date": l["date"]
    } for l in channel_logs_col.find().sort("_id", -1).limit(100)])

@app.route("/dashboard/api/sms-pool")
@require_auth
def api_sms_pool():
    return jsonify([{"amount": s.get("amount"), "created_at": s.get("created_at_str"), "preview": (s.get("raw_text") or "")[:100]} for s in sms_pool_col.find({"status": "UNUSED"}).sort("created_at_dt", -1).limit(100)])

@app.route("/dashboard/api/users")
@require_auth
def api_users():
    return jsonify([{"user_id": u.get("user_id"), "updated_at": u.get("updated_at")} for u in users_col.find().sort("updated_at", -1).limit(200)])

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Store Dashboard</title>
<style>
  :root{--bg:#0F1512; --surface:#141C18; --line:#26332C; --text:#EDF2EF; --muted:#8FA398; --ok:#3ED9A0; --pending:#E8A94A; --danger:#E8695F;}
  *{box-sizing:border-box;} body{margin:0; background:var(--bg); color:var(--text); font-family:sans-serif; font-size:14px;}
  .mono{font-family:monospace;}
  header{padding:15px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between;}
  .ledger{display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:10px; padding:15px;}
  .ledger .row{background:var(--surface); padding:15px; border-radius:6px; border:1px solid var(--line);}
  .tabs{display:flex; gap:15px; padding:10px 15px; border-bottom:1px solid var(--line); overflow-x:auto;}
  .tab{color:var(--muted); cursor:pointer; padding-bottom:5px; border-bottom:2px solid transparent; white-space:nowrap;}
  .tab.active{color:var(--text); border-bottom-color:var(--ok);}
  .subtabs{display:flex; gap:10px; padding:10px 15px;}
  .subtab{color:var(--muted); padding:4px 10px; border:1px solid var(--line); border-radius:15px; cursor:pointer;}
  .subtab.active{background:var(--ok); color:var(--bg); border-color:var(--ok);}
  .list{padding:15px; padding-bottom:50px;}
  .item{display:flex; justify-content:space-between; align-items:center; background:var(--surface); padding:10px; margin-bottom:8px; border-left:3px solid var(--line); border-radius:4px;}
  .item.ok{border-color:var(--ok);} .item.pending{border-color:var(--pending);} .item.expired{border-color:var(--danger);}
  .item .main{flex:1;} .item .name{font-weight:bold;} .item .sub{color:var(--muted); font-size:12px; margin-top:3px;}
  .item .amt{font-size:15px; font-weight:bold;} 
  .action-btn{background:var(--surface); color:var(--text); border:1px solid var(--line); padding:6px 10px; border-radius:4px; cursor:pointer; font-size:12px; margin-top:6px; display:inline-block;}
  .ok-btn{border-color:var(--ok); color:var(--ok);} .danger-btn{border-color:var(--danger); color:var(--danger);}
  .form-box{background:var(--surface); padding:15px; border-radius:6px; border:1px solid var(--line); margin-bottom:15px;}
  .form-box input, .form-box select, .form-box textarea{width:100%; padding:8px; margin:5px 0 10px; background:var(--bg); border:1px solid var(--line); color:var(--text); border-radius:4px;}
  .form-box button{background:var(--ok); color:var(--bg); border:none; padding:10px 15px; border-radius:4px; font-weight:bold; cursor:pointer;}
</style></head><body>
<header><h2>Store Dashboard</h2><div id="clock" class="mono"></div></header>
<div class="ledger" id="overview"></div>
<div class="tabs">
  <div class="tab active" data-tab="orders">Orders</div>
  <div class="tab" data-tab="courses">Courses</div>
  <div class="tab" data-tab="offers">Offers</div>
  <div class="tab" data-tab="logs">Channel Logs</div>
  <div class="tab" data-tab="broadcast">Broadcast</div>
  <div class="tab" data-tab="sms">SMS Pool</div>
  <div class="tab" data-tab="users">Users</div>
</div>
<div class="subtabs" id="subtabs">
  <div class="subtab active" data-status="all">All</div>
  <div class="subtab" data-status="pending">Pending</div>
  <div class="subtab" data-status="completed">Completed</div>
  <div class="subtab" data-status="expired">Expired</div>
</div>
<div class="list" id="list">Loading...</div>
<script>
let curTab="orders", curSt="all";
function fmtSecs(s){ if(s<=0)return "0s"; let m=Math.floor(s/60), sec=s%60; return m+"m "+sec+"s"; }
async function load(){
  let r=await fetch("/dashboard/api/overview"), d=await r.json();
  document.getElementById("overview").innerHTML=`
    <div class="row">Today: <br><b class="mono" style="font-size:18px">₹${d.today_amount}</b></div>
    <div class="row">Active QR: <b>${d.active_pending}</b><br>Pending SMS: <b>${d.pending_sms}</b></div>
    <div class="row">Total Sales: <b>${d.completed_total}</b><br>Total Users: <b>${d.total_users}</b></div>
  `;
  if(curTab==="orders"){
    r=await fetch("/dashboard/api/orders?status="+curSt); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>{
      let c=x.status==="PENDING"?"pending":x.status==="EXPIRED"?"expired":"ok";
      let rgt = x.status==="PENDING"?`<div class="timer mono" data-remain="${x.remaining_seconds}">${fmtSecs(x.remaining_seconds)} bacha</div>`:`<div class="sub">${x.method||x.status}</div>`;
      if((x.status==="PENDING"||x.status==="EXPIRED") && x.has_screenshot) rgt+=`<br><button class="action-btn ok-btn" onclick="appr('${x.order_id}')">📸 Approve (SS)</button>`;
      return `<div class="item ${c}"><div class="main"><div class="name">${x.user} · <span class="mono">${x.course_id}</span></div><div class="sub">${x.order_id} · ${x.created_at}</div></div><div style="text-align:right"><div class="amt mono">₹${x.amount}</div>${rgt}</div></div>`;
    }).join("")||"No orders.";
  } else if(curTab==="courses"){
    r=await fetch("/dashboard/api/courses"); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>`<div class="item ok" style="cursor:pointer" onclick="viewBuyers('${x.course_id}')"><div class="main"><div class="name"><span class="mono">${x.course_id}</span> ${x.is_channel?'📢 Channel':'📝 Text'}</div><div class="sub">₹${x.amount} · ${x.caption}</div></div><div><button class="action-btn danger-btn" onclick="event.stopPropagation(); delC('${x.course_id}')">🗑 Delete</button></div></div>`).join("")||"No courses.";
  } else if(curTab==="offers"){
    r=await fetch("/dashboard/api/offers"); let o=await r.json();
    let formHTML = `<div class="form-box"><h3>Create New Offer</h3>
      <label>Discount %:</label><input type="number" id="off_disc" value="50">
      <label>Target:</label><select id="off_tgt" onchange="document.getElementById('off_cid').style.display=this.value=='single'?'block':'none'"><option value="all">All Courses</option><option value="single">Single Course</option></select>
      <input type="text" id="off_cid" placeholder="Course ID (e.g. c_12345)" style="display:none">
      <label>Max Users (0 for unlimited):</label><input type="number" id="off_max" value="0">
      <label>Per User Limit (1 = one-time only per user, 0 = unlimited):</label><input type="number" id="off_peruser" value="1">
      <label>Active Hours:</label><input type="number" id="off_hrs" value="24">
      <button onclick="createOffer()">Create Offer</button></div>`;
    let listHTML = o.map(x=>`<div class="item ${x.status==='Active'?'ok':'expired'}"><div class="main"><div class="name">${x.discount}% OFF - <span class="mono">${x.offer_code}</span></div><div class="sub">🔗 Link: <a href="${x.link}" target="_blank" style="color:#3ED9A0">${x.link}</a></div><div class="sub">Target: ${x.target} ${x.course?'('+x.course+')':''} | Used: ${x.used}/${x.max==-1?'∞':x.max} | Per User: ${x.per_user==-1?'∞':x.per_user+'x'} | Exp: ${x.expires}</div></div><div><button class="action-btn danger-btn" onclick="delOffer('${x.offer_code}')">🗑</button></div></div>`).join("");
    document.getElementById("list").innerHTML = formHTML + (listHTML||"No offers.");
  } else if(curTab==="broadcast"){
    document.getElementById("list").innerHTML = `<div class="form-box"><h3>Broadcast Message</h3>
      <p style="font-size:12px; color:var(--muted)">Use HTML tags: &lt;b&gt;<b>Bold</b>&lt;/b&gt;, &lt;i&gt;<i>Italic</i>&lt;/i&gt;</p>
      <textarea id="bc_msg" rows="5" placeholder="Type your message here..."></textarea>
      <p style="font-size:12px; color:var(--muted); margin-top:10px;">Buttons (Optional) - Format: <b>Name - Link</b> (One per line)</p>
      <textarea id="bc_btns" rows="3" placeholder="My Youtube - https://youtube.com\\nChat with me - https://t.me/yourid"></textarea>
      <button onclick="sendBc()">🚀 Send to All Users</button></div>`;
  } else if(curTab==="logs"){
    r=await fetch("/dashboard/api/channel-logs"); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>`<div class="item ${x.status==='APPROVED'?'ok':'expired'}"><div class="main"><div class="name">${x.first_name} (@${x.username}) - <span class="mono">${x.user_id}</span></div><div class="sub">📺 Channel: <b style="color:var(--text)">${x.channel_name}</b></div><div class="sub">Pack: ${x.course} · ${x.date}</div></div><div style="font-weight:bold; color:var(--${x.status==='APPROVED'?'ok':'danger'})">${x.status}</div></div>`).join("")||"No logs yet.";
  } else if(curTab==="sms"){
    r=await fetch("/dashboard/api/sms-pool"); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>`<div class="item pending"><div class="main"><div class="name">₹${x.amount}</div><div class="sub mono">${x.preview}</div></div><div><div class="sub">${x.created_at}</div></div></div>`).join("")||"No SMS.";
  } else {
    r=await fetch("/dashboard/api/users"); let o=await r.json();
    document.getElementById("list").innerHTML = o.map(x=>`<div class="item ok"><div class="main"><div class="name">ID: <span class="mono">${x.user_id}</span></div><div class="sub">Active: ${x.updated_at}</div></div></div>`).join("")||"No users.";
  }
}
async function appr(id){ if(confirm("Approve order manually?")){ await fetch("/dashboard/api/orders/"+id+"/approve",{method:"POST"}); load(); } }
async function delC(id){ if(confirm("Delete course?")){ await fetch("/dashboard/api/courses/"+id,{method:"DELETE"}); load(); } }
async function viewBuyers(courseId){
  document.getElementById("list").innerHTML = "Loading buyers...";
  let r = await fetch("/dashboard/api/courses/"+courseId+"/buyers"); let d = await r.json();
  let rows = d.buyers.map(b=>`<div class="item ok"><div class="main"><div class="name">${b.user} <span class="mono">(${b.user_id})</span></div><div class="sub">🕒 ${b.purchased_at} · 💰 ₹${b.amount} · ${b.method}</div></div>${d.is_channel?`<div style="font-weight:bold; color:var(--${b.channel_joined?'ok':'danger'})">${b.channel_joined?'📺 Joined':'⏳ Not Joined'}</div>`:''}</div>`).join("")||"Is pack ko abhi tak kisi ne nahi khareeda.";
  document.getElementById("list").innerHTML = `<div class="form-box"><button class="action-btn" onclick="curTab='courses'; document.getElementById('subtabs').style.display='none'; load();">⬅ Back to Courses</button><h3 style="margin-top:10px; margin-bottom:0">${courseId}${d.channel_name?' · 📺 '+d.channel_name:''}</h3><div class="sub" style="color:var(--muted)">${d.buyers.length} Buyer(s)</div></div>` + rows;
}
async function delOffer(id){ if(confirm("Delete this offer?")){ await fetch("/dashboard/api/offers/"+id,{method:"DELETE"}); load(); } }
async function createOffer(){
  let d = { discount: document.getElementById('off_disc').value, target_type: document.getElementById('off_tgt').value, course_id: document.getElementById('off_cid').value, max_users: document.getElementById('off_max').value==-1?-1:(document.getElementById('off_max').value==0?-1:document.getElementById('off_max').value), per_user_limit: document.getElementById('off_peruser').value==0?-1:document.getElementById('off_peruser').value, hours: document.getElementById('off_hrs').value };
  await fetch("/dashboard/api/offers", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(d)}); load();
}
async function sendBc(){
  let msg = document.getElementById('bc_msg').value;
  let btnRaw = document.getElementById('bc_btns').value;
  if(!msg) return alert("Message is empty!");
  let btns = [];
  if(btnRaw){
     for(let l of btnRaw.split("\\n")){
        if(l.includes("-")){
           let pts = l.split("-");
           btns.push({text: pts[0].trim(), url: pts.slice(1).join("-").trim()});
        }
     }
  }
  if(confirm("Send this broadcast to ALL users?")){
    await fetch("/dashboard/api/broadcast", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({message: msg, buttons: btns})});
    alert("Broadcast started in background!"); document.getElementById('bc_msg').value=""; document.getElementById('bc_btns').value="";
  }
}

document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{ document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active")); t.classList.add("active"); curTab=t.dataset.tab; document.getElementById("subtabs").style.display=curTab==="orders"?"flex":"none"; load(); }));
document.querySelectorAll(".subtab").forEach(t=>t.addEventListener("click",()=>{ document.querySelectorAll(".subtab").forEach(x=>x.classList.remove("active")); t.classList.add("active"); curSt=t.dataset.status; load(); }));
setInterval(()=>{ document.querySelectorAll("[data-remain]").forEach(el=>{ let s=Math.max(0,el.dataset.remain-1); el.dataset.remain=s; el.textContent=fmtSecs(s)+" bacha"; }); document.getElementById("clock").textContent=new Date().toLocaleTimeString("en-IN"); }, 1000);
load(); setInterval(load, 15000);
</script></body></html>"""

@app.route("/dashboard")
@require_auth
def dashboard_page(): return DASHBOARD_HTML

# ==========================================
# 🔄 GLOBAL THREADS (CPU Saver & Crash Restore)
# ==========================================
def global_sms_checker():
    while True:
        try:
            time.sleep(10) # 10 सेकंड में एक बार डेटाबेस से चेक करेगा
            pending_orders_list = list(orders_col.find({"status": "PENDING"}))
            for order in pending_orders_list:
                amt_key = order.get("amount")
                sms_rec = sms_pool_col.find_one({"amount": amt_key, "status": "UNUSED"})
                if sms_rec:
                    updated = sms_pool_col.update_one({"_id": sms_rec["_id"], "status": "UNUSED"}, {"$set": {"status": "PROCESSED"}})
                    if updated.modified_count > 0:
                        deliver_course_to_buyer(order, sms_text=sms_rec.get("raw_text"), is_manual=False)
        except Exception as e:
            pass

def global_memory_cleanup():
    while True:
        try:
            time.sleep(3600) # हर 1 घंटे में मेमोरी क्लीन करेगा
            now = time.time()
            # Clear old orders cache
            keys_to_del = [oid for oid, o in all_orders_cache.items() if now - o.get("created_at", 0) > 86400]
            for k in keys_to_del: all_orders_cache.pop(k, None)
            # Clear old cooldown limits
            cool_keys = [uid for uid, t in user_cooldowns.items() if now - t > 3600]
            for k in cool_keys: user_cooldowns.pop(k, None)
        except Exception: pass

def restore_pending_orders():
    for order in orders_col.find({"status": "PENDING"}):
        order_id, amt_key, chat_id, created_ts = order["order_id"], order.get("amount"), order.get("chat_id"), order.get("created_at", 0)
        remaining = QR_EXPIRY_SECONDS - (time.time() - created_ts if created_ts else QR_EXPIRY_SECONDS)
        with pending_lock: pending_orders[amt_key], all_orders_cache[order_id] = order, order
        if remaining > 0:
            threading.Timer(remaining, expire_qr, args=(chat_id, order.get("qr_msg_id"), order["course_id"], amt_key, order_id)).start()
        else:
            threading.Thread(target=expire_qr, args=(chat_id, order.get("qr_msg_id"), order["course_id"], amt_key, order_id), daemon=True).start()

if __name__ == "__main__":
    try: BOT_USERNAME = bot.get_me().username
    except Exception: pass
    
    # Start Services
    restore_pending_orders()
    threading.Thread(target=global_sms_checker, daemon=True).start()
    threading.Thread(target=global_memory_cleanup, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), daemon=True).start()
    
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
