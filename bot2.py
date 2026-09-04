import io
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, request
from PIL import Image, ImageDraw
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
# 🛑 सेटिंग्स और टोकन्स (सीधे यहीं set हैं — testing ke liye) 🛑
# ==========================================
# ⚠️ सिर्फ यह एक लाइन बदलनी है — अपने BotFather वाला असली token यहां paste karo.
# (Tumhare screenshot me token beech se cut ho gaya tha, isliye main use guess nahi kar sakta —
#  BotFather chat me apne bot ko kholo, "API Token" copy karo, aur neeche paste kardo.)
BOT_TOKEN = "8235615756:AAEW6m_YRrDR9dWAox6BoV2NwaAp2ucnfjc"

# 🔑 Yeh secret webhook URL ko lock karta hai — maine ek random value bana ke daal di hai,
# chaho to isko waisa hi rehne do, koi dikkat nahi. Bas kisi ko share mat karna.
SMS_HOOK_SECRET = "84dea856ae8001df1bd2912e0833bc30379dffe1"

if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
    print("❌ ERROR: BOT_TOKEN abhi bhi placeholder hai. Upar wali line me apna asli bot token daalo.")
    sys.exit(1)

# बाकी सेटिंग्स (tumhare pehle wale code se hi liya hai)
ADMIN_ID = 8820964089
DB_CHANNEL_ID = -1003757631353
UPI_ID = "Q520245588@ybl"
MERCHANT_NAME = "Study Wala"

CHAT_LINK = "https://t.me/SaulGoodmanOp"
INTERNATIONAL_LINK = "https://t.me/SaulGoodmanOp"

# Order kitni der tak valid rahega (seconds)
QR_EXPIRY_SECONDS = 600  # 10 minute

bot = telebot.TeleBot(BOT_TOKEN)

# ==========================================
# 🗂 SIMPLE JSON FILE STORAGE (MongoDB ki jagah) 🗂
# ==========================================
# ⚠️ IMPORTANT: Render ke free/standard web service ka disk EPHEMERAL hota hai —
# restart/redeploy hone par yeh files delete ho jaati hain. Testing ke liye theek hai,
# lekin production me courses/purchases permanently rakhne ke liye Render "Persistent Disk"
# add karo (Render dashboard -> Disks) aur DATA_DIR ko us disk ke path par point karo,
# ya baad me ek proper DB (SQLite/Postgres) laga lena.
DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)


class JSONCollection:
    """MongoDB collection jaisa hi thoda-bahut interface, lekin ek JSON file me store karta hai."""

    def __init__(self, name):
        self.path = os.path.join(DATA_DIR, f"{name}.json")
        self.lock = threading.Lock()
        if not os.path.exists(self.path):
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _matches(doc, query):
        return all(doc.get(k) == v for k, v in query.items())

    def find_one(self, query):
        for doc in self._load():
            if self._matches(doc, query):
                return doc
        return None

    def find(self, query=None):
        data = self._load()
        if not query:
            return data
        return [d for d in data if self._matches(d, query)]

    def recent(self, limit=15):
        return list(reversed(self._load()))[:limit]

    def insert_one(self, doc):
        with self.lock:
            data = self._load()
            doc = dict(doc)
            doc.setdefault("_id", str(uuid.uuid4()))
            data.append(doc)
            self._save(data)
        return doc

    def update_one(self, query, update, upsert=False):
        set_data = update.get("$set", {})
        with self.lock:
            data = self._load()
            for doc in data:
                if self._matches(doc, query):
                    doc.update(set_data)
                    self._save(data)
                    return doc
            if upsert:
                new_doc = {**query, **set_data}
                new_doc.setdefault("_id", str(uuid.uuid4()))
                data.append(new_doc)
                self._save(data)
                return new_doc
        return None

    def delete_one(self, query):
        with self.lock:
            data = self._load()
            for i, doc in enumerate(data):
                if self._matches(doc, query):
                    data.pop(i)
                    self._save(data)
                    return True
        return False


purchases_col = JSONCollection("purchases")
courses_col = JSONCollection("courses")
batches_col = JSONCollection("batches")
users_col = JSONCollection("users")
file_links_col = JSONCollection("file_links")
menu_buttons_col = JSONCollection("menu_buttons")


# --- डेटाबेस हेल्पर फंक्शन्स ---
def get_course_data(course_id):
    return courses_col.find_one({"course_id": course_id})

def get_batch_data(batch_id):
    return batches_col.find_one({"batch_id": batch_id})


# --- ऑटोमैटिक UPI QR जनरेटर ---
def generate_upi_qr(amount, order_id):
    clean_amt = re.sub(r"[^\d.]", "", str(amount))
    upi_url = f"upi://pay?pa={UPI_ID}&pn={MERCHANT_NAME}&am={clean_amt}&cu=INR&tn=Order_{order_id}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=2,
    )
    qr.add_data(upi_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    w, h = qr_img.size

    box_w = int(w * 0.28)
    box_h = int(box_w * 0.40)
    box_x = (w - box_w) // 2
    box_y = (h - box_h) // 2
    draw = ImageDraw.Draw(qr_img)
    draw.rounded_rectangle(
        [box_x, box_y, box_x + box_w, box_y + box_h],
        radius=6,
        fill="#ffffff",
        outline="#0b1329",
        width=2,
    )

    lw = max(2, int(box_h * 0.12))
    let_w = box_w * 0.18
    let_h = box_h * 0.45
    gap = box_w * 0.08
    total_w = (let_w * 2) + gap * 2 + lw
    start_x = box_x + (box_w - total_w) // 2
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


# --- स्टेट मैनेजमेंट ---
admin_data = {}
user_states = {}
user_qr_messages = {}

# 🔑 pending_orders: key = exact amount string jaise "99.07" (paise wale digit se
# order unique pehchana jaata hai), value = order info dict.
pending_orders = {}
pending_lock = threading.Lock()


def generate_unique_amount(base_amount):
    """Base price me chhote random paise jodta hai taaki har order ka amount
    ekdum unique ho jaaye — isi se SMS aane par pata chalta hai ki yeh
    kis order ka payment hai (kyunki bank/UPI app ka SMS me sirf amount
    hota hai, koi order id nahi hoti)."""
    base = round(float(base_amount))
    with pending_lock:
        for _ in range(300):
            candidate = round(base + random.randint(1, 97) / 100, 2)
            key = f"{candidate:.2f}"
            if key not in pending_orders:
                return key
    # bahut hi rare fallback
    return f"{base + random.random():.2f}"


# --- टाइमर फंक्शन्स ---
def expire_qr(chat_id, message_id, course_id, amount_key):
    with pending_lock:
        pending_orders.pop(amount_key, None)
    if chat_id in user_states and user_states[chat_id].get("amount_key") == amount_key:
        try:
            bot.delete_message(chat_id, message_id)
            bot.send_message(chat_id, f"❌ <b>Your payment session ({QR_EXPIRY_SECONDS // 60} min) has expired! Please try again.</b>", parse_mode="HTML")
            del user_states[chat_id]
        except Exception:
            pass


def deliver_course_to_buyer(order, sms_text=None):
    """Payment match hone (ya admin ke /approve karne) par pack deliver karta hai."""
    chat_id = order["chat_id"]
    user_id = order["user_id"]
    course_id = order["course_id"]
    course = get_course_data(course_id)

    if user_id in user_states and user_states[user_id].get("order_id") == order["order_id"]:
        del user_states[user_id]

    if chat_id in user_qr_messages:
        try:
            bot.delete_message(chat_id, user_qr_messages[chat_id])
        except Exception:
            pass
        del user_qr_messages[chat_id]

    if not course:
        try:
            bot.send_message(chat_id, "⚠️ Payment mil gayi, lekin pack details nahi mili. Admin se contact karo.")
        except Exception:
            pass
        return

    try:
        bot.send_message(chat_id, f"🎉 <b>Payment Verified!</b>\n\n{course['secret_text']}", parse_mode="HTML")
    except Exception:
        pass

    date_now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    try:
        user_info = bot.get_chat(user_id)
        user_mention = f"@{user_info.username}" if user_info.username else f"<a href='tg://user?id={user_id}'>{user_info.first_name or 'User'}</a>"
    except Exception:
        user_mention = f"<a href='tg://user?id={user_id}'>User</a>"

    purchases_col.insert_one({
        "user_id": user_id,
        "username": user_mention,
        "item_info": f"{course_id} | Rate: ₹{order['amount']} | AUTO-VERIFIED (order {order['order_id']})",
        "date": date_now,
    })

    admin_note = (
        "✅ <b>[AUTO-VERIFIED & DELIVERED]</b>\n\n"
        f"👤 <b>User:</b> {user_mention}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"📚 <b>Pack:</b> <code>{course_id}</code>\n"
        f"💰 <b>Paid:</b> ₹{order['amount']}\n"
        f"🔖 <b>Order:</b> <code>{order['order_id']}</code>\n"
        f"📅 <b>Date:</b> {date_now}"
    )
    if sms_text:
        admin_note += f"\n\n📩 <b>SMS:</b> <code>{sms_text[:300]}</code>"
    try:
        bot.send_message(DB_CHANNEL_ID, admin_note, parse_mode="HTML")
    except Exception:
        pass


# ==========================================
# 🛑 कोर्स व बैच डिलीवरी 🛑
# ==========================================
def send_course_to_user(chat_id, course):
    try: promo_items = json.loads(course.get("promo_media", "[]"))
    except Exception: promo_items = []

    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(f"🇮🇳 UPI (Pay ₹{course['amount']})", callback_data=f"pay_upi_{course['course_id']}"))

    btn_row = []
    if INTERNATIONAL_LINK: btn_row.append(InlineKeyboardButton("🌍 International", url=INTERNATIONAL_LINK))
    if CHAT_LINK: btn_row.append(InlineKeyboardButton("💬 Chat with Me", url=CHAT_LINK))
    if btn_row: markup.row(*btn_row)

    media_items = [it for it in promo_items if it["type"] in ["photo", "video"]]
    first_photo_caption = ""
    for it in media_items:
        if it.get("caption"):
            first_photo_caption = it["caption"].strip()
            break

    custom_caption = course.get("custom_caption", "").strip()
    final_album_caption = ""
    if first_photo_caption and custom_caption: final_album_caption = f"{first_photo_caption}\n\n{custom_caption}"
    elif first_photo_caption: final_album_caption = first_photo_caption
    elif custom_caption: final_album_caption = custom_caption

    if len(final_album_caption) > 1000: final_album_caption = final_album_caption[:1000] + "..."

    if len(media_items) == 1:
        item = media_items[0]
        try:
            if item["type"] == "photo": bot.send_photo(chat_id, item["file_id"], caption=final_album_caption, reply_markup=markup, parse_mode="HTML")
            elif item["type"] == "video": bot.send_video(chat_id, item["file_id"], caption=final_album_caption, reply_markup=markup, parse_mode="HTML")
        except Exception: pass
    elif len(media_items) > 1:
        media_group_html = []
        for i, item in enumerate(media_items):
            cap = final_album_caption if i == 0 else ""
            if item["type"] == "photo": media_group_html.append(InputMediaPhoto(item["file_id"], caption=cap, parse_mode="HTML"))
            elif item["type"] == "video": media_group_html.append(InputMediaVideo(item["file_id"], caption=cap, parse_mode="HTML"))
        try: bot.send_media_group(chat_id, media_group_html)
        except Exception: pass
        try: bot.send_message(chat_id, f"👆 <b>Choose an option to buy this pack (₹{course['amount']}):</b>\n", reply_markup=markup, parse_mode="HTML")
        except Exception: pass

def send_batch_to_user(chat_id, batch):
    bot.send_message(chat_id, f"📦 <b>{batch['title']}</b>\nAll packs are listed below:\n<i>(Niche sabhi packs diye gaye hain:)</i>", parse_mode="HTML")
    try: course_ids = json.loads(batch["course_ids"])
    except Exception: course_ids = []

    for cid in course_ids:
        c_data = get_course_data(cid)
        if c_data: send_course_to_user(chat_id, c_data)


# 🌟 डायनेमिक स्टार्ट मेन्यू 🌟
def send_main_menu(chat_id):
    buttons = menu_buttons_col.find()
    markup = InlineKeyboardMarkup()
    for b in buttons:
        target = b["target_data"]
        if target.startswith("http"): markup.row(InlineKeyboardButton(b["button_text"], url=target))
        else: markup.row(InlineKeyboardButton(b["button_text"], callback_data=f"mainmenu_{target}"))

    msg_text = (
        "👋 <b>Welcome to our Store!</b>\n\n"
        "Please select a course or pack from the menu below to get started:\n"
        "<i>(Niche diye gaye options mein se koi course chunein:)</i>"
    )
    bot.send_message(chat_id, msg_text, reply_markup=markup, parse_mode="HTML")

def send_admin_panel(chat_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("➕ Add Single Pack", callback_data="admin_add_course"))
    markup.row(InlineKeyboardButton("📦 Pack Batch (Multi-Pack)", callback_data="admin_create_batch"))
    markup.row(InlineKeyboardButton("🔗 Advanced File to Link", callback_data="admin_file_link"))
    markup.row(InlineKeyboardButton("📢 Advanced Broadcast", callback_data="admin_broadcast"))
    markup.row(InlineKeyboardButton("📋 Manage Main Menu", callback_data="admin_manage_menu"))
    markup.row(InlineKeyboardButton("👥 User Info", callback_data="admin_user_info"))

    bot.send_message(chat_id, "🛠 <b>Admin Panel</b>\nPlease select an option:\n", reply_markup=markup, parse_mode="HTML")


# ==========================================
# 1. स्टार्ट कमांड
# ==========================================
@bot.message_handler(commands=["start"])
def start_command(message):
    user_id = message.chat.id
    users_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)

    param = message.text.split()[1].strip() if len(message.text.split()) > 1 else ""

    if param.startswith("b_"):
        batch = get_batch_data(param)
        if batch: send_batch_to_user(user_id, batch)
        else: bot.send_message(user_id, "❌ <b>This pack link has expired.</b>", parse_mode="HTML")

    elif param.startswith("c_"):
        course = get_course_data(param)
        if course: send_course_to_user(user_id, course)
        else: bot.send_message(user_id, "❌ <b>This link is not available.</b>", parse_mode="HTML")

    elif param.startswith("f_"):
        file_data = file_links_col.find_one({"file_code": param})
        if file_data:
            try:
                media_items = json.loads(file_data["media_data"])
                buttons = json.loads(file_data["button_data"])
            except Exception:
                media_items, buttons = [], []

            markup = InlineKeyboardMarkup()
            for b in buttons: markup.row(InlineKeyboardButton(b["text"], url=b["url"]))

            if len(media_items) == 1:
                item = media_items[0]
                try:
                    if item["type"] == "text": bot.send_message(user_id, item["caption"], reply_markup=markup, parse_mode="HTML")
                    elif item["type"] == "photo": bot.send_photo(user_id, item["file_id"], caption=item["caption"], reply_markup=markup, parse_mode="HTML")
                    elif item["type"] == "video": bot.send_video(user_id, item["file_id"], caption=item["caption"], reply_markup=markup, parse_mode="HTML")
                    elif item["type"] == "document": bot.send_document(user_id, item["file_id"], caption=item["caption"], reply_markup=markup, parse_mode="HTML")
                except Exception as e:
                    bot.send_message(user_id, f"❌ Error: {e}")

            elif len(media_items) > 1:
                media_group = []
                for item in media_items:
                    if item["type"] == "photo": media_group.append(InputMediaPhoto(item["file_id"], caption=item["caption"], parse_mode="HTML"))
                    elif item["type"] == "video": media_group.append(InputMediaVideo(item["file_id"], caption=item["caption"], parse_mode="HTML"))
                    elif item["type"] == "document": media_group.append(InputMediaDocument(item["file_id"], caption=item["caption"], parse_mode="HTML"))
                try:
                    if media_group: bot.send_media_group(user_id, media_group)
                    if buttons or any(i["type"] == "text" for i in media_items):
                        bot.send_message(user_id, "👇 <b>Check the link(s) below:</b>", reply_markup=markup, parse_mode="HTML")
                except Exception as e:
                    bot.send_message(user_id, f"❌ Error sending album: {e}")
        else:
            bot.send_message(user_id, "❌ <b>File not found or expired.</b>", parse_mode="HTML")

    else:
        if user_id == ADMIN_ID: send_admin_panel(user_id)
        else: send_main_menu(user_id)


# ==========================================
# 1b. एडमिन: मैन्युअल अप्रूवल / पेंडिंग लिस्ट (फॉलबैक सेफ्टी नेट)
# ==========================================
@bot.message_handler(commands=["approve"])
def manual_approve(message):
    if message.chat.id != ADMIN_ID:
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        bot.reply_to(message, "Usage: <code>/approve ORDER_ID</code>", parse_mode="HTML")
        return
    order_id = parts[1].strip()
    with pending_lock:
        match_key = None
        for k, o in pending_orders.items():
            if o["order_id"] == order_id:
                match_key = k
                break
        order = pending_orders.pop(match_key, None) if match_key else None
    if not order:
        bot.reply_to(message, "❌ Order not found (already delivered/expired ho chuka hoga).")
        return
    deliver_course_to_buyer(order)
    bot.reply_to(message, f"✅ Order {order_id} manually approved & delivered.")


@bot.message_handler(commands=["pending"])
def list_pending(message):
    if message.chat.id != ADMIN_ID:
        return
    with pending_lock:
        items = list(pending_orders.values())
    if not items:
        bot.reply_to(message, "✅ Koi pending order nahi hai.")
        return
    lines = ["⏳ <b>Pending Orders:</b>\n"]
    for o in items:
        age_min = int((time.time() - o["created_at"]) / 60)
        lines.append(f"🔖 <code>{o['order_id']}</code> | ₹{o['amount']} | pack <code>{o['course_id']}</code> | {age_min} min pehle")
    bot.reply_to(message, "\n".join(lines), parse_mode="HTML")


# ==========================================
# 2. एडमिन और यूज़र मैसेज हैंडलर
# ==========================================
@bot.message_handler(content_types=["photo", "video", "document", "text"])
def handle_all_messages(message):
    user_id = message.chat.id

    # --- ADMIN FLOWS ---
    if user_id == ADMIN_ID and user_id in admin_data:
        step = admin_data[ADMIN_ID].get("step")

        if step == "BC_MEDIA":
            media_type, file_id = "text", None
            caption = message.caption or message.text or ""
            if message.photo: media_type, file_id = "photo", message.photo[-1].file_id
            elif message.video: media_type, file_id = "video", message.video.file_id
            elif message.document: media_type, file_id = "document", message.document.file_id

            admin_data[ADMIN_ID].setdefault("media", []).append({"type": media_type, "file_id": file_id, "caption": caption})
            markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done Adding Media/Text", callback_data="bc_done"))
            bot.send_message(ADMIN_ID, "✅ <b>Saved!</b>\nSend another Photo/Video/Text, OR click 'Done' if finished.", reply_markup=markup, parse_mode="HTML")
            return

        elif step == "BC_BUTTONS":
            btn_text = message.text.strip()
            if " - " in btn_text:
                try:
                    text, url = btn_text.split(" - ", 1)
                    admin_data[ADMIN_ID].setdefault("buttons", []).append({"text": text.strip(), "url": url.strip()})
                    count = len(admin_data[ADMIN_ID]["buttons"])
                    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Broadcast", callback_data="bc_finish"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! (Total: {count})</b>\n\nWant to add another button? Send it in <code>Name - Link</code> format.\n\n<i>(OR click Finish to start broadcast)</i>", reply_markup=markup, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format Error. Use EXACTLY like this: <code>My Website - https://google.com</code>", parse_mode="HTML")
            else: bot.send_message(ADMIN_ID, "❌ Format Error. Use EXACTLY like this: <code>My Website - https://google.com</code>", parse_mode="HTML")
            return

        elif step == "FTL_MEDIA":
            media_type, file_id = "text", None
            caption = message.caption or message.text or ""
            if message.photo: media_type, file_id = "photo", message.photo[-1].file_id
            elif message.video: media_type, file_id = "video", message.video.file_id
            elif message.document: media_type, file_id = "document", message.document.file_id

            admin_data[ADMIN_ID].setdefault("media", []).append({"type": media_type, "file_id": file_id, "caption": caption})
            markup = InlineKeyboardMarkup().row(InlineKeyboardButton("✅ Done Adding Media/Text", callback_data="ftl_done"))
            bot.send_message(ADMIN_ID, "✅ <b>Saved!</b>\nSend another Photo/Video/Text, OR click 'Done' if finished.", reply_markup=markup, parse_mode="HTML")
            return

        elif step == "FTL_BUTTONS":
            btn_text = message.text.strip()
            if " - " in btn_text:
                try:
                    text, url = btn_text.split(" - ", 1)
                    admin_data[ADMIN_ID].setdefault("buttons", []).append({"text": text.strip(), "url": url.strip()})
                    count = len(admin_data[ADMIN_ID]["buttons"])
                    markup = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Create Link", callback_data="ftl_finish"))
                    bot.send_message(ADMIN_ID, f"✅ <b>Button Added! (Total: {count})</b>\n\nWant to add another button? Send it in <code>Name - Link</code> format.\n\n<i>(OR click Finish to generate link)</i>", reply_markup=markup, parse_mode="HTML")
                except Exception: bot.send_message(ADMIN_ID, "❌ Format Error.", parse_mode="HTML")
            else: bot.send_message(ADMIN_ID, "❌ Format Error.", parse_mode="HTML")
            return

        elif step == "TITLE":
            admin_data[ADMIN_ID]["title"] = message.text.strip()
            admin_data[ADMIN_ID]["step"] = "PROMO"
            admin_data[ADMIN_ID]["promo"] = []
            bot.send_message(ADMIN_ID, f"✅ Batch title saved: <b>{admin_data[ADMIN_ID]['title']}</b>\n\n📝 <b>Send the promo media (photo/video).</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price")))
            return

        elif step == "PROMO":
            media_type, file_id = "text", None
            if message.photo: media_type, file_id = "photo", message.photo[-1].file_id
            elif message.video: media_type, file_id = "video", message.video.file_id
            admin_data[ADMIN_ID]["promo"].append({"type": media_type, "file_id": file_id, "caption": message.caption or message.text or ""})
            return

        elif step == "AMOUNT":
            clean_amt = re.sub(r"[^\d.]", "", message.text.strip())
            if not clean_amt:
                bot.send_message(ADMIN_ID, "❌ <b>Please enter the price in numbers only.</b>", parse_mode="HTML")
                return
            admin_data[ADMIN_ID]["amount"] = clean_amt
            admin_data[ADMIN_ID]["step"] = "CAPTION"
            markup = InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip (No Caption)", callback_data="skip_caption"))
            bot.send_message(ADMIN_ID, f"✅ <b>Price ₹{clean_amt} saved!</b>\n📝 Type any extra caption.", reply_markup=markup, parse_mode="HTML")
            return

        elif step == "CAPTION":
            admin_data[ADMIN_ID]["caption"] = message.text.strip()
            admin_data[ADMIN_ID]["step"] = "SECRET"
            bot.send_message(ADMIN_ID, "✅ <b>Caption saved!</b>\n🔗 Now send the final secret link:", parse_mode="HTML")
            return

        elif step == "SECRET":
            secret = message.text.strip()
            course_id = "c_" + str(uuid.uuid4())[:6]
            promo_json = json.dumps(admin_data[ADMIN_ID]["promo"])

            courses_col.update_one({"course_id": course_id}, {"$set": {"course_id": course_id, "promo_media": promo_json, "amount": admin_data[ADMIN_ID]["amount"], "custom_caption": admin_data[ADMIN_ID]["caption"], "secret_text": secret}}, upsert=True)

            mode = admin_data[ADMIN_ID].get("mode")
            if mode == "single":
                link = f"https://t.me/{bot.get_me().username}?start={course_id}"
                bot.send_message(ADMIN_ID, f"🎉 <b>Pack created!</b>\n💰 Price: ₹{admin_data[ADMIN_ID]['amount']}\n👉 <code>{link}</code>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            elif mode == "batch":
                admin_data[ADMIN_ID]["course_ids"].append(course_id)
                admin_data[ADMIN_ID]["step"] = "NEXT_ACTION"
                markup = InlineKeyboardMarkup()
                markup.row(InlineKeyboardButton("➕ Add Another Pack", callback_data="batch_add_next"))
                markup.row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                bot.send_message(ADMIN_ID, f"✅ <b>Pack saved! (Total: {len(admin_data[ADMIN_ID]['course_ids'])})</b>", reply_markup=markup, parse_mode="HTML")
            return

        elif step == "MENU_TARGET":
            admin_data[ADMIN_ID]["menu_target"] = message.text.strip()
            admin_data[ADMIN_ID]["step"] = "MENU_TEXT"
            bot.send_message(ADMIN_ID, "📝 <b>Send Button Text:</b>\nWhat text should appear on the button?", parse_mode="HTML")
            return

        elif step == "MENU_TEXT":
            btn_text = message.text.strip()
            target = admin_data[ADMIN_ID]["menu_target"]
            menu_buttons_col.insert_one({"button_text": btn_text, "target_data": target})
            bot.send_message(ADMIN_ID, f"✅ <b>Button Added!</b>\n\nText: {btn_text}\nLink: {target}", parse_mode="HTML")
            del admin_data[ADMIN_ID]
            send_admin_panel(ADMIN_ID)
            return

    # Note: purani "USER PAYMENT SUBMISSION" (screenshot/UTR admin ko bhejna) yahan se
    # hata di gayi hai — ab payment /sms-webhook route se apne aap verify hoti hai.
    # Agar user pending payment ke dauraan kuch message bhejta hai to bas usko batado.
    if user_id in user_states:
        bot.send_message(user_id, "⏳ <b>Payment automatically verify ho rahi hai.</b> Pay karne ke turant baad yahin pack mil jayega. Agar 10 minute me na mile to admin se contact karo.", parse_mode="HTML")


# ==========================================
# 3. कॉलबैक बटन्स हैंडलर
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def handle_buttons(call):
    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data == "bc_done":
        admin_data[ADMIN_ID]["step"] = "BC_BUTTONS"
        admin_data[ADMIN_ID]["buttons"] = []
        markup = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Broadcast (No Buttons)", callback_data="bc_finish"))
        bot.send_message(ADMIN_ID, "✅ <b>Media/Text Saved!</b>\n\nDo you want to add buttons below the message?\nSend your FIRST button like this:\n<code>My Website - https://google.com</code>\n\n<i>(Or click Finish to broadcast immediately without buttons)</i>", reply_markup=markup, parse_mode="HTML")
        return

    elif data == "bc_finish":
        media_items = admin_data[ADMIN_ID].get("media", [])
        buttons = admin_data[ADMIN_ID].get("buttons", [])

        markup = InlineKeyboardMarkup()
        for b in buttons: markup.row(InlineKeyboardButton(b["text"], url=b["url"]))

        bot.send_message(ADMIN_ID, "⏳ Broadcasting started... Please wait.")
        users = users_col.find()
        success_count = 0
        for u in users:
            uid = u["user_id"]
            try:
                if not media_items: continue
                if len(media_items) == 1:
                    item = media_items[0]
                    if item["type"] == "text": bot.send_message(uid, item["caption"], reply_markup=markup, parse_mode="HTML")
                    elif item["type"] == "photo": bot.send_photo(uid, item["file_id"], caption=item["caption"], reply_markup=markup, parse_mode="HTML")
                    elif item["type"] == "video": bot.send_video(uid, item["file_id"], caption=item["caption"], reply_markup=markup, parse_mode="HTML")
                    elif item["type"] == "document": bot.send_document(uid, item["file_id"], caption=item["caption"], reply_markup=markup, parse_mode="HTML")
                elif len(media_items) > 1:
                    media_group = []
                    for item in media_items:
                        if item["type"] == "photo": media_group.append(InputMediaPhoto(item["file_id"], caption=item["caption"], parse_mode="HTML"))
                        elif item["type"] == "video": media_group.append(InputMediaVideo(item["file_id"], caption=item["caption"], parse_mode="HTML"))
                        elif item["type"] == "document": media_group.append(InputMediaDocument(item["file_id"], caption=item["caption"], parse_mode="HTML"))
                    if media_group: bot.send_media_group(uid, media_group)
                    if buttons or any(i["type"] == "text" for i in media_items): bot.send_message(uid, "👇 <b>Check the link(s) below:</b>", reply_markup=markup, parse_mode="HTML")
                success_count += 1
            except Exception: pass

        bot.send_message(ADMIN_ID, f"✅ <b>Broadcast Complete!</b>\nMessage successfully sent to {success_count} users.", parse_mode="HTML")
        del admin_data[ADMIN_ID]
        send_admin_panel(ADMIN_ID)
        return

    if data == "ftl_done":
        admin_data[ADMIN_ID]["step"] = "FTL_BUTTONS"
        admin_data[ADMIN_ID]["buttons"] = []
        markup = InlineKeyboardMarkup().row(InlineKeyboardButton("🚀 Finish & Create Link (No Buttons)", callback_data="ftl_finish"))
        bot.send_message(ADMIN_ID, "✅ <b>Media/Text Saved!</b>\n\nDo you want to add buttons below the message?\nSend your FIRST button like this:\n<code>My Website - https://google.com</code>\n\n<i>(Or click Finish to create link immediately without buttons)</i>", reply_markup=markup, parse_mode="HTML")
        return

    elif data == "ftl_finish":
        file_code = "f_" + str(uuid.uuid4())[:6]
        media_json = json.dumps(admin_data[ADMIN_ID].get("media", []))
        btn_json = json.dumps(admin_data[ADMIN_ID].get("buttons", []))

        file_links_col.update_one({"file_code": file_code}, {"$set": {"file_code": file_code, "media_data": media_json, "button_data": btn_json}}, upsert=True)
        link = f"https://t.me/{bot.get_me().username}?start={file_code}"
        bot.send_message(ADMIN_ID, f"🎉 <b>File/Album Link Created Successfully!</b>\n\n🔗 Share this link:\n<code>{link}</code>", parse_mode="HTML")
        del admin_data[ADMIN_ID]
        send_admin_panel(ADMIN_ID)
        return

    if data.startswith("mainmenu_"):
        target = data.replace("mainmenu_", "")
        bot.answer_callback_query(call.id)
        if target.startswith("c_"):
            course = get_course_data(target)
            if course: send_course_to_user(chat_id, course)
        elif target.startswith("b_"):
            batch = get_batch_data(target)
            if batch: send_batch_to_user(chat_id, batch)
        return

    if data.startswith("pay_upi_"):
        bot.answer_callback_query(call.id, "⏳ Generating Payment QR...", show_alert=False)
        course_id = data.replace("pay_upi_", "")
        course = get_course_data(course_id)
        if course:
            order_id = str(uuid.uuid4())[:8]
            amt_key = generate_unique_amount(course["amount"])

            first_name = call.from_user.first_name or "User"
            username_h = f"(@{call.from_user.username})" if call.from_user.username else ""

            with pending_lock:
                pending_orders[amt_key] = {
                    "order_id": order_id,
                    "course_id": course_id,
                    "user_id": call.from_user.id,
                    "chat_id": chat_id,
                    "amount": amt_key,
                    "created_at": time.time(),
                }
            user_states[chat_id] = {"course_id": course_id, "order_id": order_id, "amount_key": amt_key}

            qr_img_bio, clean_amt = generate_upi_qr(amt_key, order_id)

            invoice_text = (
                f"👤 <b>User:</b> {first_name} {username_h}\n"
                f"🆔 <b>Order ID:</b> <code>{order_id}</code>\n"
                f"📅 <b>Date:</b> {datetime.now().strftime('%d-%m-%Y %I:%M %p')}\n"
                f"💰 <b>Amount:</b> ₹{clean_amt}\n"
                f"💳 <b>UPI ID:</b> <code>{UPI_ID}</code>\n"
            )
            if course.get("custom_caption"): invoice_text += f"\n📝 {course['custom_caption']}\n"
            invoice_text += (
                "\n⚠️ <b>Pay the EXACT amount shown above (including the paise) — "
                "this is what confirms it's your payment.</b>\n"
                "🤖 <b>AUTO-VERIFICATION:</b> Just pay and wait ~10-20 seconds. No screenshot needed!\n"
                f"⏳ <i>This QR expires in {QR_EXPIRY_SECONDS // 60} minutes.</i>"
            )

            markup = InlineKeyboardMarkup()
            if CHAT_LINK: markup.row(InlineKeyboardButton("💬 Chat with Admin", url=CHAT_LINK))

            sent_msg = bot.send_photo(chat_id, photo=qr_img_bio, caption=invoice_text, reply_markup=markup, parse_mode="HTML")
            user_qr_messages[chat_id] = sent_msg.message_id
            threading.Timer(QR_EXPIRY_SECONDS, expire_qr, args=(chat_id, sent_msg.message_id, course_id, amt_key)).start()
        return

    bot.answer_callback_query(call.id)

    if data == "admin_add_course":
        admin_data[ADMIN_ID] = {"mode": "single", "step": "PROMO", "promo": [], "amount": None, "caption": ""}
        markup = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price"))
        bot.edit_message_text("📝 <b>Step 1/4: Promo Media</b>\nSend demo photo/video.", chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="HTML")

    elif data == "admin_create_batch":
        admin_data[ADMIN_ID] = {"mode": "batch", "step": "TITLE", "course_ids": []}
        bot.edit_message_text("📦 <b>Create New Pack Batch</b>\nPlease send the Title:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")

    elif data == "admin_file_link":
        admin_data[ADMIN_ID] = {"step": "FTL_MEDIA", "media": []}
        bot.edit_message_text("📎 <b>Advanced File to Link</b>\nSend your Photos, Videos, Documents, or Texts (with captions).\n<i>(Apni file yahan bhejein:)</i>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")

    elif data == "admin_broadcast":
        admin_data[ADMIN_ID] = {"step": "BC_MEDIA", "media": []}
        bot.edit_message_text("📢 <b>Advanced Broadcast</b>\nSend the Text, Photo, Video, or Document that you want to broadcast.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")

    elif data == "admin_manage_menu":
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("➕ Add Menu Button", callback_data="admin_menu_add"))
        markup.row(InlineKeyboardButton("🗑 Delete Menu Button", callback_data="admin_menu_del"))
        markup.row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
        bot.edit_message_text("📋 <b>Manage Main Menu</b>", chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="HTML")

    elif data == "admin_menu_add":
        admin_data[ADMIN_ID] = {"step": "MENU_TARGET"}
        bot.edit_message_text("🔗 <b>Add Menu Button</b>\nSend the Course ID (c_...), Batch ID (b_...), or Website URL:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")

    elif data == "admin_menu_del":
        buttons = menu_buttons_col.find()
        if not buttons:
            bot.edit_message_text("❌ No buttons currently in menu.", chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="admin_manage_menu")))
            return
        markup = InlineKeyboardMarkup()
        for b in buttons: markup.add(InlineKeyboardButton(f"❌ {b['button_text']}", callback_data=f"delmenu_{b['_id']}"))
        markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_manage_menu"))
        bot.edit_message_text("🗑 <b>Click a button to delete it:</b>", chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="HTML")

    elif data.startswith("delmenu_"):
        btn_id = data.replace("delmenu_", "")
        menu_buttons_col.delete_one({"_id": btn_id})
        bot.edit_message_text("✅ <b>Button Deleted!</b>", chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back to Admin", callback_data="back_to_admin")), parse_mode="HTML")

    elif data == "next_price":
        if ADMIN_ID not in admin_data or not admin_data[ADMIN_ID].get("promo"): return
        admin_data[ADMIN_ID]["step"] = "AMOUNT"
        bot.edit_message_text("💰 <b>Step 2/4: Price</b>\nSend the price.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")

    elif data == "skip_caption":
        if ADMIN_ID in admin_data:
            admin_data[ADMIN_ID]["caption"] = ""
            admin_data[ADMIN_ID]["step"] = "SECRET"
            bot.edit_message_text("✅ <b>Caption Skipped!</b>\n🔗 <b>Step 4/4: Final Link</b>\nSend the final secret link.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")

    elif data == "batch_add_next":
        admin_data[ADMIN_ID]["step"] = "PROMO"
        admin_data[ADMIN_ID]["promo"] = []
        admin_data[ADMIN_ID]["caption"] = ""
        markup = InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price"))
        bot.edit_message_text("📝 <b>Send promo media for the next pack</b>", chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="HTML")

    elif data == "batch_finish":
        d = admin_data.get(ADMIN_ID)
        if not d or not d.get("course_ids"): return
        batch_id = "b_" + str(uuid.uuid4())[:6]
        c_ids_json = json.dumps(d["course_ids"])
        batches_col.update_one({"batch_id": batch_id}, {"$set": {"batch_id": batch_id, "title": d["title"], "course_ids": c_ids_json}}, upsert=True)
        link = f"https://t.me/{bot.get_me().username}?start={batch_id}"
        bot.edit_message_text(f"🎉 <b>Pack Batch Created!</b>\n👉 <code>{link}</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        del admin_data[ADMIN_ID]
        send_admin_panel(ADMIN_ID)

    elif data == "admin_user_info":
        records = purchases_col.recent(15)
        if not records: bot.edit_message_text("No one has bought a pack yet.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        else:
            text = "👥 <b>Recent Purchases:</b>\n\n"
            for r in records: text += f"👤 {r.get('username', '')} | 📅 {r.get('date', '')[:10]} | 📚 <code>{r.get('item_info', '')}</code>\n"
            markup = InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin"))
            bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=markup, parse_mode="HTML")

    elif data == "back_to_admin":
        try: bot.delete_message(chat_id, msg_id)
        except Exception: pass
        send_admin_panel(chat_id)


# ==========================================
# 4. Flask Web Server (Render 24/7 Hosting) + SMS Auto-Verify Webhook
# ==========================================
app = Flask(__name__)

AMOUNT_RE_DECIMAL = re.compile(r"Rs\.?\s?([\d,]+\.\d{2})")
AMOUNT_RE_INT = re.compile(r"Rs\.?\s?([\d,]+)(?!\.\d)")


@app.route("/")
def home():
    return "Telegram Pack Bot is running (JSON storage, SMS auto-verify webhook)."


@app.route("/sms-webhook/<secret>")
def sms_webhook(secret):
    """MacroDroid (ya koi bhi SMS-forwarding app) yahan GET request bhejta hai jab
    payment ka SMS aata hai. URL example:
    https://YOUR-APP.onrender.com/sms-webhook/<SMS_HOOK_SECRET>?text={sms_message}
    """
    if secret != SMS_HOOK_SECRET:
        return "forbidden", 403

    sms_text = request.args.get("text", "").strip()
    if not sms_text:
        return "no 'text' param", 400

    m = AMOUNT_RE_DECIMAL.search(sms_text)
    has_decimal = bool(m)
    if not m:
        m = AMOUNT_RE_INT.search(sms_text)
    if not m:
        return "no amount found in sms", 200

    amt_str = m.group(1).replace(",", "")

    with pending_lock:
        if has_decimal:
            candidates = [amt_str] if amt_str in pending_orders else []
        else:
            # SMS me paise nahi the (round amount) — sabhi pending orders dhoondo jinka
            # base amount match karta ho.
            candidates = [k for k in pending_orders if k.startswith(amt_str + ".")]

        if len(candidates) == 1:
            order = pending_orders.pop(candidates[0])
        else:
            order = None
            ambiguous = len(candidates) > 1

    if order:
        deliver_course_to_buyer(order, sms_text=sms_text)
        return "matched", 200

    if ambiguous:
        try:
            bot.send_message(DB_CHANNEL_ID, f"⚠️ <b>Ambiguous payment</b> ₹{amt_str} — multiple pending orders match. Check /pending and use /approve manually.\n\n📩 SMS: <code>{sms_text[:300]}</code>", parse_mode="HTML")
        except Exception:
            pass
        return "ambiguous", 200

    try:
        bot.send_message(DB_CHANNEL_ID, f"ℹ️ SMS received (₹{amt_str}) but no matching pending order.\n\n📩 SMS: <code>{sms_text[:300]}</code>", parse_mode="HTML")
    except Exception:
        pass
    return "no match", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    def run_bot():
        # 🟢 यह 'while True' लूप 409 Conflict एरर आने पर बॉट को क्रैश होने से बचाएगा
        while True:
            try:
                print("🤖 Bot is starting and connecting to Telegram API...")
                bot.infinity_polling(skip_pending=True)
            except Exception as e:
                print(f"⚠️ Bot API Error (Probably 409 Conflict): {e}")
                print("🔄 Retrying in 5 seconds to wait for Render old instance to die...")
                time.sleep(5)

    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=port)
