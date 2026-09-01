import io
import json
import os
import sqlite3
import threading
import uuid
import hmac
import hashlib
from datetime import datetime
from flask import Flask, request
import qrcode
import razorpay
import telebot
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)

# ==========================================
# 🛑 सेटिंग्स (Render Environment Variables से आएँगी) 🛑
# ==========================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0").strip())
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "0").strip())

CHAT_LINK = os.environ.get("CHAT_LINK", "https://t.me/SaulGoodmanOp").strip()
INTERNATIONAL_LINK = os.environ.get("INTERNATIONAL_LINK", "https://t.me/SaulGoodmanOp").strip()

# --- RAZORPAY SETTINGS ---
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "") 

rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
bot = telebot.TeleBot(BOT_TOKEN)

# --- डेटाबेस और बैकअप सेटअप ---
DB_FILE = "shop_master_v5.db"
BACKUP_FILE = "master_backup_v5.json"

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                user_id INTEGER, 
                username TEXT, 
                item_info TEXT, 
                date TEXT
            )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS courses (
                course_id TEXT PRIMARY KEY, 
                promo_media TEXT, 
                amount TEXT, 
                custom_caption TEXT, 
                secret_text TEXT
            )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY, 
                title TEXT, 
                course_ids TEXT
            )""")
        conn.commit()

init_db()

def load_backup():
    if os.path.exists(BACKUP_FILE):
        try:
            with open(BACKUP_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"courses": {}, "batches": {}}

def save_backup(data):
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_course_data(course_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM courses WHERE course_id=?", (course_id,))
        row = cursor.fetchone()
        if row: return dict(row)
    return load_backup().get("courses", {}).get(course_id)

def get_batch_data(batch_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,))
        row = cursor.fetchone()
        if row: return dict(row)
    return load_backup().get("batches", {}).get(batch_id)

# --- स्टेट मैनेजमेंट ---
admin_data = {}
pending_orders = {} 
user_qr_messages = {}

def expire_qr(chat_id, message_id, order_id):
    if order_id in pending_orders:
        try:
            bot.delete_message(chat_id, message_id)
            bot.send_message(
                chat_id,
                "❌ <b>Your payment session (10 minutes) has expired! Please try again.</b>",
                parse_mode="HTML",
            )
            del pending_orders[order_id]
        except:
            pass

# ==========================================
# 🛑 कोर्स डिलीवरी सिस्टम 🛑
# ==========================================
def send_course_to_user(chat_id, course):
    try: promo_items = json.loads(course["promo_media"])
    except: promo_items = []

    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton(f"💳 Pay ₹{course['amount']} (Auto Verify)", callback_data=f"pay_rzp_{course['course_id']}"))
    
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
    final_album_caption = f"{first_photo_caption}\n\n{custom_caption}" if first_photo_caption and custom_caption else (first_photo_caption or custom_caption)
    if len(final_album_caption) > 1000: final_album_caption = final_album_caption[:1000] + "..."

    if len(media_items) == 1:
        item = media_items[0]
        try:
            if item["type"] == "photo": bot.send_photo(chat_id, item["file_id"], caption=final_album_caption, reply_markup=markup, parse_mode="HTML")
            elif item["type"] == "video": bot.send_video(chat_id, item["file_id"], caption=final_album_caption, reply_markup=markup, parse_mode="HTML")
        except: pass
    elif len(media_items) > 1:
        media_group_html = []
        for i, item in enumerate(media_items):
            cap = final_album_caption if i == 0 else "" 
            if item["type"] == "photo": media_group_html.append(InputMediaPhoto(item["file_id"], caption=cap, parse_mode="HTML"))
            elif item["type"] == "video": media_group_html.append(InputMediaVideo(item["file_id"], caption=cap, parse_mode="HTML"))
        try: bot.send_media_group(chat_id, media_group_html)
        except Exception as e: print(f"MediaGroup Error: {e}")
        bot.send_message(chat_id, f"👆 <b>Choose an option to buy this pack (₹{course['amount']}):</b>", reply_markup=markup, parse_mode="HTML")

# ==========================================
# 1. स्टार्ट कमांड
# ==========================================
@bot.message_handler(commands=["start"])
def start_command(message):
    param = message.text.split()[1].strip() if len(message.text.split()) > 1 else ""
    user_id = message.chat.id

    if param.startswith("b_"):
        batch = get_batch_data(param)
        if batch:
            bot.send_message(user_id, f"📦 <b>{batch['title']}</b>\nAll packs are listed below:", parse_mode="HTML")
            for cid in json.loads(batch["course_ids"]):
                c_data = get_course_data(cid)
                if c_data: send_course_to_user(user_id, c_data)
        else:
            bot.send_message(user_id, "❌ <b>This pack link has expired.</b>", parse_mode="HTML")
    elif param.startswith("c_"):
        course = get_course_data(param)
        if course: send_course_to_user(user_id, course)
        else: bot.send_message(user_id, "❌ <b>This link is not available.</b>", parse_mode="HTML")
    else:
        if user_id == ADMIN_ID: send_admin_panel(user_id)
        else: bot.send_message(user_id, "👋 <b>Hello! Please click on the correct pack link to enter.</b>", parse_mode="HTML")

def send_admin_panel(chat_id):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("➕ Add Single Pack", callback_data="admin_add_course"))
    markup.row(InlineKeyboardButton("📦 Pack Batch (Multi-Pack)", callback_data="admin_create_batch"))
    markup.row(InlineKeyboardButton("👥 User Info", callback_data="admin_user_info"))
    bot.send_message(chat_id, "🛠 <b>Admin Panel</b>\nPlease select an option:", reply_markup=markup, parse_mode="HTML")

# ==========================================
# 2. एडमिन मैसेज हैंडलर
# ==========================================
@bot.message_handler(content_types=["photo", "video", "document", "text"])
def handle_all_messages(message):
    user_id = message.chat.id
    if user_id == ADMIN_ID and user_id in admin_data:
        step = admin_data[ADMIN_ID].get("step")

        if step == "TITLE":
            admin_data[ADMIN_ID]["title"] = message.text.strip()
            admin_data[ADMIN_ID]["step"] = "PROMO"
            admin_data[ADMIN_ID]["promo"] = []
            bot.send_message(ADMIN_ID, f"✅ Batch title saved!\n📝 <b>Send promo media (photo/video).</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price")))
            return
        elif step == "PROMO":
            media_type, file_id = "text", None
            if message.photo: media_type, file_id = "photo", message.photo[-1].file_id
            elif message.video: media_type, file_id = "video", message.video.file_id
            admin_data[ADMIN_ID]["promo"].append({"type": media_type, "file_id": file_id, "caption": message.caption or message.text or ""})
            return
        elif step == "AMOUNT":
            clean_amt = "".join(filter(str.isdigit, message.text.strip()))
            if not clean_amt: return bot.send_message(ADMIN_ID, "❌ Please enter numbers only.", parse_mode="HTML")
            admin_data[ADMIN_ID]["amount"] = clean_amt
            admin_data[ADMIN_ID]["step"] = "CAPTION"
            bot.send_message(ADMIN_ID, f"✅ <b>Price ₹{clean_amt} saved!</b>\n\n📝 Type extra caption or skip.", reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("⏭ Skip", callback_data="skip_caption")), parse_mode="HTML")
            return
        elif step == "CAPTION":
            admin_data[ADMIN_ID]["caption"] = message.text.strip()
            admin_data[ADMIN_ID]["step"] = "SECRET"
            bot.send_message(ADMIN_ID, "✅ <b>Caption saved!</b>\n\n🔗 Now send the final secret link:", parse_mode="HTML")
            return
        elif step == "SECRET":
            secret = message.text.strip()
            course_id = "c_" + str(uuid.uuid4())[:6]
            promo_json = json.dumps(admin_data[ADMIN_ID]["promo"])

            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT OR REPLACE INTO courses VALUES (?, ?, ?, ?, ?)", (course_id, promo_json, admin_data[ADMIN_ID]["amount"], admin_data[ADMIN_ID]["caption"], secret))
                conn.commit()

            mode = admin_data[ADMIN_ID].get("mode")
            if mode == "single":
                bot.send_message(ADMIN_ID, f"🎉 <b>Pack created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={course_id}</code>", parse_mode="HTML")
                del admin_data[ADMIN_ID]
                send_admin_panel(ADMIN_ID)
            elif mode == "batch":
                admin_data[ADMIN_ID]["course_ids"].append(course_id)
                markup = InlineKeyboardMarkup()
                markup.row(InlineKeyboardButton("➕ Add Another Pack", callback_data="batch_add_next"))
                markup.row(InlineKeyboardButton("✅ Finish Batch", callback_data="batch_finish"))
                bot.send_message(ADMIN_ID, f"✅ <b>Pack saved! (Total: {len(admin_data[ADMIN_ID]['course_ids'])})</b>", reply_markup=markup, parse_mode="HTML")
            return

# ==========================================
# 3. सभी बटन्स को हैंडल करना
# ==========================================
@bot.callback_query_handler(func=lambda call: True)
def handle_buttons(call):
    data = call.data
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    # --- USER: RAZORPAY PAYMENT ---
    if data.startswith("pay_rzp_"):
        bot.answer_callback_query(call.id, "⏳ Generating Secure Payment QR...", show_alert=False)
        course_id = data.replace("pay_rzp_", "")
        course = get_course_data(course_id)
        
        if course:
            order_id = "ORDER_" + str(uuid.uuid4())[:8].upper()
            amount_in_paise = int(float(course["amount"]) * 100) 

            try:
                pl_data = {
                    "amount": amount_in_paise,
                    "currency": "INR",
                    "accept_partial": False,
                    "reference_id": order_id,
                    "description": f"Purchase Pack: {course_id}",
                    "customer": {"name": call.from_user.first_name or "Telegram User"},
                    "notify": {"sms": False, "email": False},
                    "reminder_enable": False
                }
                payment_link = rzp_client.payment_link.create(pl_data)
                short_url = payment_link['short_url']

                qr = qrcode.QRCode(version=1, box_size=10, border=2)
                qr.add_data(short_url)
                qr.make(fit=True)
                qr_img = qr.make_image(fill_color="black", back_color="white")
                
                bio = io.BytesIO()
                qr_img.save(bio, "PNG")
                bio.seek(0)

                pending_orders[order_id] = {"chat_id": chat_id, "course_id": course_id}

                invoice_text = (
                    f"🆔 <b>Order ID:</b> <code>{order_id}</code>\n"
                    f"💰 <b>Amount:</b> ₹{course['amount']}\n\n"
                    f"✅ <b>How to pay:</b>\n"
                    f"Scan this QR Code using any UPI app (GPay, PhonePe, Paytm).\n\n"
                    f"⏳ <i>Auto-Verification Active! The pack will be sent here automatically upon payment.</i>"
                )

                markup = InlineKeyboardMarkup()
                markup.row(InlineKeyboardButton("🔗 Open Payment Link", url=short_url))

                sent_msg = bot.send_photo(chat_id, photo=bio, caption=invoice_text, reply_markup=markup, parse_mode="HTML")
                threading.Timer(600, expire_qr, args=(chat_id, sent_msg.message_id, order_id)).start()

            except Exception as e:
                bot.send_message(chat_id, f"⚠️ Error generating payment: {str(e)}")
        return

    # --- ADMIN ACTIONS ---
    if data == "admin_add_course":
        admin_data[ADMIN_ID] = {"mode": "single", "step": "PROMO", "promo": [], "amount": None, "caption": ""}
        bot.edit_message_text("📝 <b>Step 1/4: Promo Media</b>\nSend demo photo/video.", chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price")), parse_mode="HTML")
    elif data == "admin_create_batch":
        admin_data[ADMIN_ID] = {"mode": "batch", "step": "TITLE", "course_ids": []}
        bot.edit_message_text("📦 <b>Create New Pack Batch</b>\nType Title:", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "next_price":
        admin_data[ADMIN_ID]["step"] = "AMOUNT"
        bot.edit_message_text("💰 <b>Step 2/4: Price</b>\nSend price (₹):", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "skip_caption":
        admin_data[ADMIN_ID]["caption"] = ""
        admin_data[ADMIN_ID]["step"] = "SECRET"
        bot.edit_message_text("✅ <b>Caption Skipped!</b>\n\n🔗 <b>Step 4/4: Final Link</b>\nSend the secret link.", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
    elif data == "batch_add_next":
        admin_data[ADMIN_ID]["step"] = "PROMO"
        bot.edit_message_text("📝 <b>Send promo media for next pack</b>", chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("➡️ Next Step", callback_data="next_price")), parse_mode="HTML")
    elif data == "batch_finish":
        d = admin_data.get(ADMIN_ID)
        batch_id = "b_" + str(uuid.uuid4())[:6]
        c_ids_json = json.dumps(d["course_ids"])
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO batches VALUES (?, ?, ?)", (batch_id, d["title"], c_ids_json))
            conn.commit()
        bot.edit_message_text(f"🎉 <b>Batch Created!</b>\n👉 <code>https://t.me/{bot.get_me().username}?start={batch_id}</code>", chat_id=chat_id, message_id=msg_id, parse_mode="HTML")
        del admin_data[ADMIN_ID]

    elif data == "admin_user_info":
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT username, date, item_info FROM purchases ORDER BY id DESC LIMIT 15")
            records = cursor.fetchall()
        
        text = "👥 <b>Recent Purchases:</b>\n\n" if records else "No one has bought a pack yet."
        for r in records: text += f"👤 {r['username']} | 📅 {r['date'][:10]} | 📚 <code>{r['item_info']}</code>\n"
        bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=InlineKeyboardMarkup().row(InlineKeyboardButton("🔙 Back", callback_data="back_to_admin")), parse_mode="HTML")

    elif data == "back_to_admin":
        try: bot.delete_message(chat_id, msg_id)
        except: pass
        send_admin_panel(chat_id)

# ==========================================
# 4. Flask Web Server & Webhook Handler
# ==========================================
app = Flask(__name__)

@app.route("/")
def home():
    return "Telegram Bot is Running Smoothly!"

@app.route("/razorpay-webhook", methods=["POST"])
def razorpay_webhook():
    webhook_signature = request.headers.get('X-Razorpay-Signature')
    payload_body = request.data.decode('utf-8')
    
    generated_signature = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode('utf-8'),
        payload_body.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(generated_signature, webhook_signature):
        return "Invalid Signature", 400

    data = request.json
    
    if data['event'] == 'payment_link.paid':
        entity = data['payload']['payment_link']['entity']
        order_id = entity.get('reference_id')
        
        if order_id and order_id in pending_orders:
            chat_id = pending_orders[order_id]["chat_id"]
            course_id = pending_orders[order_id]["course_id"]
            
            course = get_course_data(course_id)
            if course:
                bot.send_message(
                    chat_id,
                    f"🎉 <b>Payment Successful!</b>\n\nHere is your requested content:\n{course['secret_text']}",
                    parse_mode="HTML"
                )
                
                date_now = datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
                try: 
                    user_info = bot.get_chat(chat_id)
                    uname = f"@{user_info.username}" if user_info.username else user_info.first_name
                except: uname = "Unknown"

                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO purchases (user_id, username, item_info, date) VALUES (?, ?, ?, ?)",
                        (chat_id, uname, f"{course_id} | Rate: ₹{course['amount']} | RAZORPAY_AUTO", date_now),
                    )
                    conn.commit()

                bot.send_message(
                    DB_CHANNEL_ID,
                    f"✅ <b>[AUTO-DELIVERED]</b>\n👤 <b>User:</b> {uname}\n📚 <b>Pack:</b> <code>{course_id}</code>\n🔖 <b>Order:</b> <code>{order_id}</code>\n💰 <b>Status:</b> Paid via Razorpay",
                    parse_mode="HTML"
                )

            del pending_orders[order_id]

    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    def run_bot():
        print("🚀 Bot is started...")
        bot.infinity_polling(skip_pending=True)

    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=port)