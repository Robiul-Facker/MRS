import logging
import pyotp
import asyncio
import warnings
import html
import re
import sqlite3
import httpx
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

# অনাকাঙ্ক্ষিত ইন্টারনাল ওয়ার্নিং হাইড করার জন্য
warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- 💠 ১. কনফিগারেশন 💠 ---
BOT_TOKEN = "8337640596:AAEH4XOyW8Xxauzfix7XUSsqYhUQ0f9cspw"
BASE_URL = "https://x.mnitnetwork.com/mapi/v1"
USER_EMAIL = "mdrobiulshaek556@gmail.com"
USER_PASS = "Robiul@159358"
OTP_GROUP_ID = -1003853823094  # আপনার ওটিপি গ্রুপ আইডি
OTP_GROUP_LINK = "https://t.me/stexsmsotp"
ADMIN_ID = 6864515052  # এডমিন আইডি

# লগিং সেটআপ
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.ERROR)

# --- 🔐 ২. গ্লোবাল ক্লায়েন্ট ও লগইন লজিক ---
BROWSER_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'en-US,en-GB;q=0.9,en;q=0.8',
    'content-type': 'application/json',
    'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36'
}

http_client = httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=30.0)
auth_token = None

async def perform_login():
    global auth_token
    url = f"{BASE_URL}/mauth/login"
    payload = {"email": USER_EMAIL, "password": USER_PASS}
    try:
        response = await http_client.post(url, json=payload)
        if response.status_code == 200:
            auth_token = response.json().get('data', {}).get('token')
            if auth_token:
                http_client.headers.update({
                    'Authorization': f'Bearer {auth_token}',
                    'mauthtoken': auth_token,
                })
                http_client.cookies.set('mauthtoken', auth_token)
                return True
    except Exception as e:
        logging.error(f"Login Error: {e}")
    return False

# --- 🗄 ৩. ডাটাবেস লজিক ---
def get_db_connection():
    # মাল্টি-থ্রেডিং এ ক্র্যাশ এড়াতে check_same_thread=False যোগ করা হয়েছে
    return sqlite3.connect('otp_bot.db', timeout=20.0, check_same_thread=False)

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS active_numbers 
                      (number TEXT PRIMARY KEY, chat_id INTEGER, expiry_time DATETIME)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS users 
                      (chat_id INTEGER PRIMARY KEY, username TEXT, join_date TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS statistics 
                      (id INTEGER PRIMARY KEY, type TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

def cleanup_expired_numbers():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("DELETE FROM active_numbers WHERE expiry_time < ?", (current_time,))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"DB Cleanup Error: {e}")

def log_user(chat_id, username):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO users (chat_id, username, join_date) VALUES (?, ?, ?)", 
                       (chat_id, username, datetime.now().strftime('%Y-%m-%d')))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"DB Log User Error: {e}")

def log_stat(stat_type):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO statistics (type, timestamp) VALUES (?, ?)", 
                       (stat_type, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"DB Log Stat Error: {e}")

def get_admin_stats():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        today = datetime.now().strftime('%Y-%m-%d')
        cursor.execute("SELECT COUNT(*) FROM statistics WHERE type='number_requested' AND timestamp LIKE ?", (f"{today}%",))
        today_numbers = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM statistics WHERE type='otp_received' AND timestamp LIKE ?", (f"{today}%",))
        today_otps = cursor.fetchone()[0]
        conn.close()
        return total_users, today_numbers, today_otps
    except Exception as e:
        logging.error(f"DB Get Stats Error: {e}")
        return 0, 0, 0

def get_all_users():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM users")
        users = [row[0] for row in cursor.fetchall()]
        conn.close()
        return users
    except Exception as e:
        logging.error(f"DB Get All Users Error: {e}")
        return []

def save_number_owner(number, chat_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        expiry = (datetime.now() + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            "REPLACE INTO active_numbers (number, chat_id, expiry_time) VALUES (?, ?, ?)", 
            (str(number), chat_id, expiry)
        )
        conn.commit()
        conn.close()
        log_stat('number_requested')
    except Exception as e:
        logging.error(f"DB Save Number Owner Error: {e}")

# --- 🛠 ৪. ইউটিলিটি ও ওটিপি এক্সট্রাক্টর ---
def extract_otp(text):
    if not text: return "N/A"
    clean_text = text.replace("<#>", "").strip()
    match = re.search(r'(\d[\s-]?){3,8}\d', clean_text)
    if match:
        return match.group(0).replace(" ", "").replace("-", "")
    return "N/A"

def get_masked_number(number):
    num_str = str(number).strip()
    if len(num_str) > 7:
        return f"{num_str[:6]}****{num_str[-4:]}"
    return num_str

async def get_console_data(search_query=None):
    if not auth_token:
        await perform_login()
    
    url = f"{BASE_URL}/mdashboard/console/info"
    try:
        resp = await http_client.get(url, timeout=15.0)
        if resp.status_code == 200:
            data = resp.json().get('data', {})
            logs = data.get('logs', [])
            
            if not logs: 
                return "📭 Console empty 🚫"

            filtered_logs = []
            if search_query:
                query = search_query.lower()
                for i in logs:
                    app_name = str(i.get('app_name', '')).lower()
                    sms_body = str(i.get('sms', '') or i.get('otp', '') or '').lower()
                    if query in app_name or query in sms_body:
                        filtered_logs.append(i)
            else:
                filtered_logs = logs

            if not filtered_logs:
                return f"📭 '{html.escape(search_query)}' Not Matching Result 🤷‍♂️"

            title = f"🔍 <b>SEARCH RESULT: {html.escape(search_query.upper())}</b>" if search_query else "🚀 <b>LIVE CONSOLE</b>"
            console_text = f"{title}\n━━━━━━━━━━━━━━━━━━\n"
            
            for i in filtered_logs[:15]: 
                app_name = html.escape(str(i.get('app_name', 'N/A')))
                time_val = html.escape(str(i.get('time', 'N/A')))
                srv_range = html.escape(str(i.get('range', 'N/A')))
                number = html.escape(str(i.get('number', 'N/A')))
                safe_sms = html.escape(str(i.get('sms') or i.get('otp') or 'Waiting...'))

                console_text += (
                    f"🌐 App: <b>{app_name}</b>\n"
                    f"🕒 Time: {time_val}\n"
                    f"📱 Num: <code>{number}</code>\n"
                    f"🎯 Range: <code>{srv_range}</code>\n"
                    f"➜ SMS: <code>{safe_sms}</code>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                )
            return console_text
        return "❌ API Response Error"
    except Exception as e: 
        logging.error(f"Console Error: {e}")
        return f"⚠️ Error: {str(e)}"

# --- ⌨️ ৫. কিবোর্ড সেটিংস ---
def get_main_menu(chat_id):
    keyboard = [
        [KeyboardButton("📱 Get Number"), KeyboardButton("🚀 Live Console")], 
        [KeyboardButton("⚙️ Set Range"), KeyboardButton("2FA KEY 🗝")],
        [KeyboardButton("🎧 Support/Help")]
    ]
    if chat_id == ADMIN_ID:
        keyboard.append([KeyboardButton("👑 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_console_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search App", callback_data="btn_search_menu"),
         InlineKeyboardButton("🔄 Refresh", callback_data="console_refresh")]
    ])

def get_search_shortcuts():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 Facebook", callback_data="srch_FACEBOOK"),
         InlineKeyboardButton("🟢 WhatsApp", callback_data="srch_WHATSAPP")],
        [InlineKeyboardButton("📸 Instagram", callback_data="srch_INSTAGRAM"),
         InlineKeyboardButton("⌨️ Custom Input", callback_data="srch_CUSTOM")],
        [InlineKeyboardButton("🔙 Back to Console", callback_data="console_refresh")]
    ])

def get_admin_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Refresh Stats", callback_data="adm_refresh"),
         InlineKeyboardButton("🔌 Check API Status", callback_data="adm_check_api")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="adm_broadcast")]
    ])

# --- 🛰 ৬. ৫-সেকেন্ড ওটিপি মনিটরিং লুপ ---
async def monitor_otp_task(chat_id, number, context, reply_to_id, user_range):
    check_url = f"{BASE_URL}/mdashboard/getnum/info"
    search_number = str(number)[-10:] 

    for _ in range(180): # ৫ সেকেন্ড পর পর মোট ১৮০ বার = ১৫ মিনিট
        try:
            if not auth_token: 
                await perform_login()
                
            today = datetime.now().strftime('%Y-%m-%d')
            params = {
                'date': today, 
                'page': 1, 
                'search': search_number, 
                'status': ''
            }
            
            resp = await http_client.get(check_url, params=params, timeout=15.0)
            
            if resp.status_code == 200:
                data_list = resp.json().get('data', {}).get('numbers', [])
                if data_list:
                    target = data_list[0]
                    full_sms = target.get('otp') or target.get('sms') or target.get('message')
                    
                    if full_sms and str(full_sms).strip().lower() != 'waiting...':
                        extracted_otp = extract_otp(full_sms)
                        
                        app_name = html.escape(str(target.get('full_number', 'Service')))
                        country = html.escape(str(target.get('country', 'N/A')))
                        safe_sms = html.escape(str(full_sms))
                        masked_num = get_masked_number(number)

                        # ১. ইউজারকে পার্সোনাল মেসেজ পাঠানো
                        user_text = (
                            f"✅ <b>OTP RECEIVED!</b>\n━━━━━━━━━━━━━━\n"
                            f"📱 <b>Number:</b> <code>{number}</code>\n"
                            f"🛠 <b>Service:</b> <code>{app_name}</code>\n"
                            f"📩 <b>OTP:</b> <code>{extracted_otp}</code>\n━━━━━━━━━━━━━━"
                        )
                        await context.bot.send_message(chat_id=chat_id, text=user_text, parse_mode='HTML')

                        # ২. গ্রুপ মেসেজের বাটন
                        group_buttons = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("🤖 Bot Link", url="https://t.me/mrsrobiotp_bot"),
                                InlineKeyboardButton("📢 Channel", url="https://t.me/hiddenearningidea")
                            ]
                        ])

                        # ৩. গ্রুপে প্রিমিয়াম অ্যালার্ট পাঠানো
                        group_msg = (
                            f"🔔 <b>PREMIUM ALERT</b>\n"
                            f"━━━━━━━━━━━━━━\n"
                            f"📞 <b>Phone:</b> <code>{masked_num}</code>\n"
                            f"🌐 <b>Range:</b> <code>{user_range}</code>\n"
                            f"🌍 <b>Country:</b> <code>{country}</code>\n"
                            f"🛠 <b>Service:</b> <code>{app_name}</code>\n\n"
                            f"📩 <b>OTP:</b> <code>{extracted_otp}</code>\n\n"
                            f"💬 <code>{safe_sms}</code>\n"
                            f"━━━━━━━━━━━━━━"
                        )
                        await context.bot.send_message(
                            chat_id=OTP_GROUP_ID, 
                            text=group_msg, 
                            parse_mode='HTML', 
                            reply_markup=group_buttons
                        )
                        
                        # স্ট্যাটিস্টিক লগ করা
                        log_stat('otp_received')
                        
                        # ওটিপি পাওয়ার পর সাথে সাথে ডাটাবেজ থেকে নম্বরটি রিমুভ করা
                        try:
                            conn = get_db_connection()
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM active_numbers WHERE number = ?", (str(number),))
                            conn.commit()
                            conn.close()
                        except Exception as db_err:
                            logging.error(f"Error deleting number after OTP success: {db_err}")
                            
                        return 
                        
        except Exception as e:
            logging.error(f"Monitoring Loop Error: {e}")
            
        await asyncio.sleep(5)
        
    # ১৫ মিনিট শেষ হলে এবং ওটিপি না আসলে অটো-ডিলিট লজিক
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM active_numbers WHERE number = ?", (str(number),))
        conn.commit()
        conn.close()
        
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"❌ <b>Time Out!</b> No OTP received for <code>{number}</code> within 15 minutes. Number removed.", 
            parse_mode='HTML'
        )
    except Exception as timeout_err:
        logging.error(f"Error handling timeout cleanup: {timeout_err}")

# --- 📱 ৭. নম্বর রিকোয়েস্ট লজিক ---
async def fetch_and_send_numbers(update, context, edit_query=None):
    user_range = context.user_data.get('range', '99206XXX')
    if not auth_token: 
        await perform_login()
    chat_id = update.effective_chat.id
    
    async def get_num():
        try:
            r = await http_client.post(f"{BASE_URL}/mdashboard/getnum/number", json={"range": user_range, "remove_plus": True}, timeout=15.0)
            if r.status_code == 200:
                return r.json().get('data', {}).get('full_number')
        except: 
            return None
        return None

    n1 = await get_num()
    await asyncio.sleep(0.3)
    n2 = await get_num()

    if not n1 and not n2:
        msg = f"🚫 No Numbers In this Range (<code>{user_range}</code>)\n⚡ Try Another Range "
        if edit_query:
            await edit_query.message.reply_text(msg, parse_mode='HTML')
        else:
            await update.message.reply_text(msg, parse_mode='HTML')
        return

    # এখানে লেখাটি ১৫ মিনিট করে দেওয়া হয়েছে
    msg = f"✅ <b>Numbers Assigned!</b>\n🎯 Range: <code>{user_range}</code>\n━━━━━━━━━━━━━━\n📱 <b>Num 1:</b> <code>{n1 if n1 else 'Failed'}</code>\n📱 <b>Num 2:</b> <code>{n2 if n2 else 'Failed'}</code>\n━━━━━━━━━━━━━━\n⌛ Waiting OTP For 15 minute......."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Change Both", callback_data="change_nums"), InlineKeyboardButton("📢 OTP Group", url=OTP_GROUP_LINK)]])
    
    if edit_query:
        sent = await edit_query.edit_message_text(msg, parse_mode='HTML', reply_markup=kb)
    else:
        sent = await update.message.reply_text(msg, parse_mode='HTML', reply_markup=kb)
    
    if n1:
        save_number_owner(n1, chat_id)
        asyncio.create_task(monitor_otp_task(chat_id, n1, context, sent.message_id, user_range))
    if n2:
        save_number_owner(n2, chat_id)
        asyncio.create_task(monitor_otp_task(chat_id, n2, context, sent.message_id, user_range))

# --- 🕹 ৮. বাটন ও মেসেজ কলব্যাকস ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data
    chat_id = update.effective_chat.id

    try:
        if data == "cancel_action":
            context.user_data['waiting_for_range'] = False
            context.user_data['waiting_for_2fa'] = False
            context.user_data['waiting_for_search'] = False
            await query.edit_message_text("🚫 <b>Operation Cancelled!</b>", parse_mode='HTML')
            await query.message.reply_text("🔙 <b>Back to main menu:</b>", reply_markup=get_main_menu(chat_id), parse_mode='HTML')
            return

        elif data == "console_refresh":
            await query.edit_message_text("⏳ <b>Console is refreshing... Please wait.</b>", parse_mode='HTML')
            text = await get_console_data()
            await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_console_buttons())

        elif data == "refresh_2fa":
            secret_key = context.user_data.get('saved_2fa_key')
            if secret_key:
                totp = pyotp.TOTP(secret_key)
                current_code = totp.now()
                time_remaining = totp.interval - (datetime.now().timestamp() % totp.interval)
                updated_text = (
                    f"🔑 <b>2FA AUTHENTICATOR CODE</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"🔐 Your Code: <code>{current_code}</code>\n"
                    f"⏳ Valid for: <b>{int(time_remaining)} seconds</b>\n━━━━━━━━━━━━━━━━━━"
                )
                refresh_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 New Code", callback_data="refresh_2fa")]])
                await query.edit_message_text(updated_text, parse_mode='HTML', reply_markup=refresh_kb)

        elif data == "btn_search_menu":
            await query.edit_message_text("🔎 <b>Select Your Service:</b>", parse_mode='HTML', reply_markup=get_search_shortcuts())
        
        elif data == "change_nums":
            await query.edit_message_text("🔄 <b>Requesting new numbers...</b>", parse_mode='HTML')
            await fetch_and_send_numbers(update, context, edit_query=query)
        
        elif data.startswith("srch_"):
            choice = data.replace("srch_", "")
            if choice == "CUSTOM":
                await query.message.reply_text("✍️ <b>Send Service Name:</b>", parse_mode='HTML')
                context.user_data['waiting_for_search'] = True
            else:
                await query.edit_message_text(f"🔍 <b>Scanning console for {choice}...</b>", parse_mode='HTML')
                text = await get_console_data(search_query=choice)
                await query.edit_message_text(text, parse_mode='HTML', reply_markup=get_search_shortcuts())

        elif chat_id == ADMIN_ID:
            if data == "adm_refresh":
                t_users, t_nums, t_otps = get_admin_stats()
                adm_text = f"👑 <b>ADMIN PANEL STATS</b>\n━━━━━━━━━━━━━━━━━━\n👥 Total Users: <b>{t_users}</b>\n📱 Numbers Today: <b>{t_nums}</b>\n📩 OTPs Today: <b>{t_otps}</b>\n━━━━━━━━━━━━━━━━━━"
                await query.edit_message_text(adm_text, parse_mode='HTML', reply_markup=get_admin_buttons())
            elif data == "adm_check_api":
                login_success = await perform_login()
                status_text = "✅ <b>API Connection: OK!</b>" if login_success else "❌ <b>API Connection: FAILED!</b>"
                await query.edit_message_text(status_text, parse_mode='HTML', reply_markup=get_admin_buttons())
            elif data == "adm_broadcast":
                context.user_data['waiting_for_broadcast'] = True
                await query.message.reply_text("✍️ <b>সব ইউজারের কাছে পাঠানোর মেসেজটি টাইপ করুন:</b>", parse_mode='HTML')
                
    except Exception as e:
        logging.error(f"Callback Error: {e}")

async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    user_text = update.message.text.strip()
    chat_id = update.effective_chat.id
    username = update.effective_user.username or f"User_{chat_id}"
    
    log_user(chat_id, username)
    
    if chat_id == ADMIN_ID and context.user_data.get('waiting_for_broadcast'):
        context.user_data['waiting_for_broadcast'] = False
        all_users = get_all_users()
        sent = 0
        for u_id in all_users:
            try:
                await context.bot.send_message(chat_id=u_id, text=f"📢 <b>NOTICE</b>\n━━━━━━━━━━━━━━\n{user_text}", parse_mode='HTML')
                sent += 1
                await asyncio.sleep(0.05)
            except: pass
        await update.message.reply_text(f"✅ Broadcast Sent to {sent}/{len(all_users)} users.")
        return

    if context.user_data.get('waiting_for_range'):
        user_text = user_text.upper().replace("XXX", "")
        if user_text.isdigit():
            formatted_range = f"{user_text}XXX"
            context.user_data['range'] = formatted_range
            context.user_data['waiting_for_range'] = False
            await update.message.reply_text(
                f"✅ <b>Range Saved Successfully!</b>\n🎯 Current Range: <code>{formatted_range}</code>", 
                parse_mode='HTML', 
                reply_markup=get_main_menu(chat_id)
            )
        else:
            await update.message.reply_text("❌ <b>Invalid Input!</b>\nঅনুগ্রহ করে শুধুমাত্র সংখ্যা দিন (যেমন: 20126)", parse_mode='HTML')
        return

    if context.user_data.get('waiting_for_search'):
        context.user_data['waiting_for_search'] = False
        result = await get_console_data(search_query=user_text)
        await update.message.reply_text(result, parse_mode='HTML', reply_markup=get_search_shortcuts())
        return

    if context.user_data.get('waiting_for_2fa'):
        context.user_data['waiting_for_2fa'] = False
        try:
            clean_key = user_text.replace(" ", "")
            totp = pyotp.TOTP(clean_key)
            context.user_data['saved_2fa_key'] = clean_key
            time_remaining = totp.interval - (datetime.now().timestamp() % totp.interval)
            msg = (
                f"✅ <b>2FA Key Saved!</b>\n"
                f"🔑 Code: <code>{totp.now()}</code>\n"
                f"⏳ Valid for: <b>{int(time_remaining)} seconds</b>"
            )
            refresh_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh Code", callback_data="refresh_2fa")]])
            await update.message.reply_text(msg, parse_mode='HTML', reply_markup=refresh_kb)
        except:
            await update.message.reply_text("❌ Invalid 2FA Key Structure.")
        return

    # কিবোর্ড ক্লিকে অ্যাকশন ট্র্রিগার
    if user_text == "🚀 Live Console":
        wait_msg = await update.message.reply_text("⏳ <b>Fetching Live Console...</b>", parse_mode='HTML')
        text = await get_console_data()
        await wait_msg.delete()
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=get_console_buttons())
    elif user_text == "📱 Get Number":
        await fetch_and_send_numbers(update, context)
    elif user_text == "⚙️ Set Range":
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]])
        await update.message.reply_text("✍️ <b>Input Your Range (Example: 99206):</b>", parse_mode='HTML', reply_markup=cancel_kb)
        context.user_data['waiting_for_range'] = True
    elif user_text == "2FA KEY 🗝":
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action")]])
        await update.message.reply_text("✍️ <b>Enter Your 2FA Secret Key:</b>", parse_mode='HTML', reply_markup=cancel_kb)
        context.user_data['waiting_for_2fa'] = True
    elif user_text == "🎧 Support/Help":
        support_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Click to Message Admin", url="https://t.me/Hei_Admin_24_bot")]
        ])
        await update.message.reply_text(
            "🙋‍♂️ কোন সাহায্য বা সহায়তার প্রয়োজন? নিচে ক্লিক করে আমাদের অ্যাডমিনের সাথে যোগাযোগ করুন:", 
            reply_markup=support_kb
        )
    elif user_text == "👑 Admin Panel" and chat_id == ADMIN_ID:
        t_users, t_nums, t_otps = get_admin_stats()
        adm_text = f"👑 <b>ADMIN PANEL</b>\n━━━━━━━━━━━━━━━━━━\n👥 Total Users: <b>{t_users}</b>\n📱 Numbers Today: <b>{t_nums}</b>\n📩 OTPs Today: <b>{t_otps}</b>\n━━━━━━━━━━━━━━━━━━"
        await update.message.reply_text(adm_text, parse_mode='HTML', reply_markup=get_admin_buttons())

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    log_user(chat_id, update.effective_user.username or "User")
    await perform_login()
    
    # প্রথম স্টার্টেই ডাটাবেজ ক্লিনের মেকানিজম চালু থাকবে
    cleanup_expired_numbers()
    
    # ✨ সব ফোনেই সাপোর্ট করবে এমন গ্লোয়িং টেক্সট ডিজাইন
    welcome_text = (
        f"👋 <b>Welcome to MRS ROBI PREMIUM BOT!</b> ✨\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Range ভিত্তিক ভার্চুয়াল নম্বর থেকে ওটিপি (OTP) নেওয়ার সবচেয়ে ফাস্ট এবং প্রিমিয়াম অটোমেশন বটে আপনাকে স্বাগতম। 🎉\n\n"
        
        f"🚀 <b>আমাদের প্রধান ফিচারসমূহ:</b>\n"
        f"⚡ <code>Get Number</code> - মাত্র এক ক্লিকে ২টি লাইভ নম্বর্যাক্টিভ করুন।\n"
        f"📊 <code>Live Console</code> - রানিং সব নম্বরের ওটিপি এবং মেসেজ লাইভ ট্র্যাক করুন।\n"
        f"⚙️ <code>Set Range</code> - আপনার কাজের সুবিধামতো কাস্টম রেঞ্জ সেট করুন।\n"
        f"🔑 <code>2FA Key</code> - বটের ভেতরেই টু-ফ্যাক্টর অথেন্টিকেটর কোড জেনারেট করুন।\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <b>মনে রাখবেন:</b> প্রতিটি নম্বর ডাটাবেজে সর্বোচ্চ <b>১৫ মিনিট</b> থাকবে। এর মধ্যে ওটিপি আসলে তা অটোমেটিক আপনার ইনবক্সে এবং আমাদের মেইন ওটিপি গ্রুপে চলে যাবে।\n\n"
        
        f"👇 কাজ শুরু করতে নিচের মেনু বাটনগুলো ব্যবহার করুন:"
    )
    
    await update.message.reply_text(
        text=welcome_text, 
        reply_markup=get_main_menu(chat_id), 
        parse_mode='HTML'
    )

# --- 🚀 মেইন রানার ---
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).read_timeout(30).write_timeout(30).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_messages))
    
    print("🤖 Bot started successfully...")
    app.run_polling()

if __name__ == '__main__':
    main()
