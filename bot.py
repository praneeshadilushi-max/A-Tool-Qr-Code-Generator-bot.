import os
import io
import qrcode
import time
import threading 
from datetime import datetime, date
from urllib.parse import urlparse

# Telegram Bot Library (Synchronous)
import telebot
from telebot import types

# SQLite Library (Standard Python Library - No external server needed)
import sqlite3 

# --- SETTINGS ---
TOKEN="8263513374:AAFCBOtk0VAJ7NscBSaCnFU0IcwIfmQQNvY" # ⚠️ මෙය ඔබගේ සත්‍ය ටෝකනයෙන් වෙනස් කරන්න
ADMIN_ID = 7874548648 # ⚠️ මෙය ඔබගේ සත්‍ය Admin ID එකෙන් වෙනස් කරන්න

SOFT_LIMIT_SECONDS = 8         
DAILY_LIMIT = 400          
MAX_TEXT_LENGTH = 500       

# SQLite Database File Name
DB_NAME = 'qr_bot_data.db' 

# List of banned keywords
BANNED_KEYWORDS = [
    "scam", "phishing", "malware", "illegal", "porn", "sex", "bomb", "fraud",
    "hack", "virus"
]

# In-memory tracking (for soft limit only)
last_qr_time = {}     
active_users = set()   


# --- DATABASE SETUP (SQLite) ---

def init_db():
    """SQLite Database Setup and Table Creation."""
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # 1. qr_limits table (User Limits)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS qr_limits (
                user_id INTEGER PRIMARY KEY,
                daily_count INTEGER NOT NULL DEFAULT 0,
                last_date DATE NOT NULL
            );
        """)
        
        # 2. qr_history table (QR History)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS qr_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                qr_data TEXT NOT NULL,
                time DATETIME NOT NULL
            );
        """)
        
        conn.commit()
        conn.close()
        print("✅ SQLite Database Setup Complete.")
        return True
        
    except Exception as e:
        print(f"🚨 SQLite Database Setup Error: {e}")
        return False

# --- DATABASE SYNCHRONOUS FUNCTIONS (For Telebot) ---

def get_user_limits(user_id) -> tuple[int, date]:
    """Retrieves user count and checks/resets daily limit."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    today_str = date.today().isoformat()
    today_date = date.today()

    try:
        cursor.execute("SELECT daily_count, last_date FROM qr_limits WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        
        if row is None:
            # New user: Insert with 0 count
            cursor.execute("INSERT INTO qr_limits (user_id, daily_count, last_date) VALUES (?, 0, ?)", (user_id, today_str))
            conn.commit()
            return 0, today_date
        
        db_count, db_last_date_str = row
        db_last_date = datetime.strptime(db_last_date_str, '%Y-%m-%d').date()
        
        if db_last_date < today_date:
            # Day reset: Update count to 0 and date to today
            cursor.execute("UPDATE qr_limits SET daily_count = 0, last_date = ? WHERE user_id = ?", (today_str, user_id))
            conn.commit()
            return 0, today_date
        
        return db_count, db_last_date
    
    except Exception as e:
        print(f"🚨 SQLite data read error: {e}")
        return DAILY_LIMIT + 1, today_date
    finally:
        conn.close()

def update_user_limits(user_id, new_count, last_date: date):
    """Updates user's daily count and last date."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    last_date_str = last_date.isoformat()

    try:
        cursor.execute(
            """
            INSERT INTO qr_limits (user_id, daily_count, last_date)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET daily_count = ?, last_date = ?
            """,
            (user_id, new_count, last_date_str, new_count, last_date_str)
        )
        conn.commit()
    except Exception as e:
        print(f"🚨 SQLite data update error: {e}")
    finally:
        conn.close()

def add_history(user_id, username, qr_data):
    """Adds a new QR generation record to history."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    now_str = datetime.now().isoformat()
    
    try:
        cursor.execute(
            "INSERT INTO qr_history (user_id, username, qr_data, time) VALUES (?, ?, ?, ?)",
            (user_id, username, qr_data[:100], now_str) # Limit data for efficiency
        )
        conn.commit()
    except Exception as e:
        print(f"🚨 SQLite history add error: {e}")
    finally:
        conn.close()


def get_user_today_count(user_id) -> int:
    """Convenience function to get only the count."""
    count, _ = get_user_limits(user_id)
    return count

def get_today_history():
    """Retrieves all history records from today."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # SQLite DATETIME stores as ISO8601 strings, we compare by date part
    today_start = date.today().isoformat() 
    
    try:
        # Select records where the date part of the 'time' column matches today
        records = cursor.execute(
            "SELECT user_id, username, qr_data, time FROM qr_history WHERE date(time) = ?",
            (today_start,)
        ).fetchall()
        
        # Convert fetched rows to a list of dictionaries for consistent output structure
        columns = ['user_id', 'username', 'qr_data', 'time']
        return [dict(zip(columns, row)) for row in records]
        
    except Exception as e:
        print(f"🚨 SQLite history read error: {e}")
        return []
    finally:
        conn.close()

# --- ADMIN CLEANUP FUNCTIONS ---
def cleanup_all_history():
    """
    Cleans up all data from the history table (qr_history) 
    AND resets daily count for all users in qr_limits table.
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    today_str = date.today().isoformat()
    try:
        # 1. Clean up qr_history table
        cursor.execute("DELETE FROM qr_history")
        
        # 2. RESET daily_count for ALL users in the qr_limits table
        cursor.execute("UPDATE qr_limits SET daily_count = 0, last_date = ?", (today_str,)) 
        
        conn.commit()
        return True
    except Exception as e:
        print(f"🚨 SQLite history clean error: {e}")
        return False
    finally:
        conn.close()
        
def cleanup_percentage_history(percentage: int) -> int:
    """
    Deletes a percentage of the OLDEST records from the qr_history table only.
    (qr_limits table is not affected by percentage cleanup).
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        total_count = cursor.execute("SELECT COUNT(*) FROM qr_history").fetchone()[0]
        if total_count == 0:
            return 0

        limit_to_delete = int(total_count * (percentage / 100.0))
        if limit_to_delete == 0:
            return 0
        
        # Get IDs of the oldest records to delete (based on time)
        ids_to_delete = cursor.execute(
            "SELECT id FROM qr_history ORDER BY time ASC LIMIT ?", 
            (limit_to_delete,)
        ).fetchall()
        
        if not ids_to_delete:
            return 0
            
        # Execute deletion
        ids_tuple = tuple(item[0] for item in ids_to_delete)
        # Construct the SQL query with placeholders for security
        placeholders = ','.join(['?'] * len(ids_tuple))
        cursor.execute(f"DELETE FROM qr_history WHERE id IN ({placeholders})", ids_tuple)
        
        deleted_count = cursor.rowcount
        conn.commit()
        return deleted_count
        
    except Exception as e:
        print(f"🚨 SQLite percentage clean error ({percentage}%): {e}")
        return -1
    finally:
        conn.close()
        

# --- BOT IMPLEMENTATION ---

bot = telebot.TeleBot(TOKEN)

def is_text_malicious(text):
    
    if len(text) > MAX_TEXT_LENGTH:
        return True, "TEXT_TOO_LONG"
    
    text_lower = text.lower()
    
    # Banned Keyword Check (High Risk filtering)
    if any(keyword in text_lower for keyword in BANNED_KEYWORDS):
        return True, "BANNED_KEYWORD"

    return False, None

# KEYBOARDS
def main_menu_keyboard():
    
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn1 = types.KeyboardButton("🌀 Generate QR Code")
    btn2 = types.KeyboardButton("📅 History")
    btn3 = types.KeyboardButton("ℹ️ About")
    btn4 = types.KeyboardButton("🔒 Privacy & Policies")
    btn5 = types.KeyboardButton("📞 Contact")
    btn6 = types.KeyboardButton("🆘 Help")
    keyboard.add(btn1, btn2)
    keyboard.add(btn3, btn4)
    keyboard.add(btn5, btn6)
    return keyboard

def back_button_keyboard():
   
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    back_btn = types.KeyboardButton("🔙 Back")
    keyboard.add(back_btn)
    return keyboard

# LIMIT CHECK FUNCTIONS
def can_generate_qr(user_id):
    
    now = datetime.now()
    last_time = last_qr_time.get(user_id)
    
    # 1. Soft limit check (8 seconds)
    if last_time:
        diff = (now - last_time).total_seconds()
        if diff < SOFT_LIMIT_SECONDS:
            return False, "soft", SOFT_LIMIT_SECONDS - int(diff) 
            
    # 2. Daily limit check (400 QR) 
    count, _ = get_user_limits(user_id)
    if count >= DAILY_LIMIT:
        return False, "daily", 0
        
    return True, None, 0

def update_qr_tracking(user_id):
    
    now = datetime.now()
    last_qr_time[user_id] = now
    
   
    count, current_date = get_user_limits(user_id)
    new_count = count + 1
    update_user_limits(user_id, new_count, current_date)


def start_live_countdown(chat_id):
   
    try:
        # Initial message
        countdown_msg = bot.send_message(chat_id, f"⏱️ Please wait **{SOFT_LIMIT_SECONDS} seconds** to generate another Qr code.", parse_mode="Markdown")

        for i in range(SOFT_LIMIT_SECONDS - 1, 0, -1):
            time.sleep(1) # Wait 1 second
            # Edit message for countdown
            bot.edit_message_text(f"⏱️ Please wait **{i} seconds** to generate another Qr code.", chat_id, countdown_msg.message_id, parse_mode="Markdown")

        time.sleep(1) 

        # Final message update
        bot.edit_message_text("✅ Now you can generate another Qr code. Send text to generate another Qr code.", chat_id, countdown_msg.message_id)

    except telebot.apihelper.ApiTelegramException as e:
        if 'message is not modified' not in str(e) and 'message to edit not found' not in str(e):
            print(f"Error during countdown: {e}")
    except Exception as e:
        print(f"General error in countdown: {e}")

# BASIC COMMANDS
@bot.message_handler(commands=['start'])
def start_message(message):
    text = (
        "👋 Welcome!\n\n"
        "You can use your **text** to generate Qr code using this bot.\n\n"
        "⬇️ You can see the menu of this bot below."
    )
    bot.send_message(message.chat.id, text, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

@bot.message_handler(commands=['menu'])
def menu_message(message):
    bot.send_message(
        message.chat.id,
        "📋 This is the main menu. Please select an option below:",
        reply_markup=main_menu_keyboard()
    )

# INFO COMMANDS
@bot.message_handler(commands=['about'], func=lambda m: m.text == "ℹ️ About" or m.text == "/about")
def about_message(message):
    text = (
        "ℹ️ *About Ak-Tool Qr Code Generator Bot.*\n\n"
        "This bot is designed to provide you to a fast, reliable and secure way to convert text, url  and some other types of data into quality qr codes.\n\n"
        f"⏱️ **Soft Limit (තත්පර {SOFT_LIMIT_SECONDS})**:\n"
        f"📅 **Daily Limit ({DAILY_LIMIT} QR)**: සියලුම පරිශීලකයින්ට සාධාරණ සේවාවක් ලබා දීම සඳහා.\n\n"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

@bot.message_handler(commands=['privacy'], func=lambda m: m.text == "🔒 Privacy & Policies" or m.text == "/privacy")
def privacy_message(message):
    text = (
        "🔒 *Privacy Policy & Terms of Use*\n\n"
        "1. Data Collection and Storage\n"
        "Our Bot collects the following data to maintain service stability and comply with regulations:\n"
        "• **User ID:** Used for managing limits and tracking history.\n"
        "• **QR Code Content (Text Data):** Stored temporarily in a **SQLite Database (Local File)** for monitoring and preventing illegal use.\n\n" 
        "🔒 **Privacy Guarantee:** Your data will **not be shared** with any external third parties.\n\n"
        "2. Terms of Use and Limits\n"
        "By using this Bot, you agree to the following terms:\n"
        "🚫 **Prohibited Content:** It is strictly forbidden to create QR Codes for illegal, harmful, or fraudulent content (*scam/phishing*).\n"
        "• **Moderation:** We use **strict filters** to screen and reject prohibited content.\n\n"
        "i. Service Limits:\n"
        f"⏱️ **Soft Limit:** A time gap of **{SOFT_LIMIT_SECONDS} seconds** must be maintained between each QR code generation to ensure server stability.\n"
        f"📅 **Daily Limit:** You are limited to a maximum of **{DAILY_LIMIT} QR codes** per day to ensure fair access for all users.\n\n"
        "⚠️ Service suspension may apply for repeated violations of these terms."
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

@bot.message_handler(commands=['contact'], func=lambda m: m.text == "📞 Contact" or m.text == "/contact")
def contract_message(message):
    text = (
        "📞 *Contact Info*\n\n"
        "Developer: Akeesha Hewage.\n\n"
        "Do you have any questions or problems? You can contact us using the methods below.\n\n"
        "01.Email: praneeshadilushi@gmail.com\n"
        "02.Telegram: @praneeshaAk\n\n"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

@bot.message_handler(commands=['help'], func=lambda m: m.text == "🆘 Help" or m.text == "/help")
def help_message(message):
    text = (
        "🆘 *Help*\n\n"
        "This section provides assistance and information on how to use the bot \n\n"
        "📖 User Commands.\n"
        "1. /generate → Start Qr code generating.\n"
        "2. /stop→ To stop Qr code generation proccess.\n"
        "3. /history → To watch today count of Qr code generating you.\n"
         "4. /about → About this bot.\n"
         "5. /privacy →To read privacy policies and terms in use of our bot.\n"
         "6. /help→ To get kowledge and help about user commands in this bot.\n"
         "7. /download → To get know steps of download Qr codes have you generating. \n"
         "8. /contact→ To contact bot admin to get some help or get some knowledge about bot.\n\n"
         "📌️ If you have any question about steps of download Qr codes use /download command.\n\n"
         "📌️ If you have any question about this bot you can contact us. Use /contact command or contact button in menu to watch contact information."
         )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

@bot.message_handler(commands=['download'])
def download_message(message):
    text = (
        "⬇️ *Download QR Codes As Images*\n\n"
        "01. Click on Qr image.\n"
        "02. Click on three dots icon in upper side.\n"
        "03. Select **Save to Gallery**"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=back_button_keyboard())

# QR GENERATION MODE
@bot.message_handler(commands=['generate'], func=lambda m: m.text == "🌀 Generate QR Code" or m.text == "/generate")
def start_generate(message):
    user_id = message.from_user.id
    active_users.add(user_id)
    bot.reply_to(
        message,
        "Now you can send **text** — this bot can generate a qr code for your text!\nYou can use /stop command any time to stop qr code generation.",
        parse_mode="Markdown"
    )

@bot.message_handler(commands=['stop'])
def stop_generate(message):
    user_id = message.from_user.id
    if user_id in active_users:
        active_users.remove(user_id)
        bot.reply_to(message,  "🛑 QR generation has been stopped.")
    else:
        bot.reply_to(message, "⚠️ You are not currently in QR generation mode.")

@bot.message_handler(commands=['history'], func=lambda m: m.text == "📅 History" or m.text == "/history")
def show_history(message):
    user_id = message.from_user.id
    count = get_user_today_count(user_id) 
    
    bot.reply_to(message, f"📅 **You have generated** {count} / {DAILY_LIMIT} Qr codes today.", parse_mode="Markdown")

# ADMIN COMMANDS
@bot.message_handler(commands=['adminhistory'])
def admin_history(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "🚫 You are not admin!")
        return

    today_records = get_today_history()
    
    if not today_records:
        bot.send_message(message.chat.id, "No history data stored today.")
        return

    # Count QR codes per user
    user_counts = {}
    for r in today_records:
        uid = r['user_id']
        uname = r.get('username', 'N/A')
        key = (uid, uname) 
        user_counts[key] = user_counts.get(key, 0) + 1

    text = f"📊 *User QR Summary for {date.today().strftime('%Y-%m-%d')}*\n\n"
    for (uid, uname), count in user_counts.items():
        display_name = f"@{uname}" if uname != 'N/A' else "No Username"
        text += f"👤 *{display_name}* | 🆔 `{uid}`\nQR Codes: {count}\n\n"

    bot.send_message(message.chat.id, text, parse_mode="Markdown")


@bot.message_handler(commands=['adminhistory_clean'])
def admin_clean_db(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "🚫 You are not admin!")
        return

    keyboard = types.InlineKeyboardMarkup()
   
    confirm_btn = types.InlineKeyboardButton("✅ Confirm Delete ALL and Reset Limits", callback_data="confirm_clean_all")
    cancel_btn = types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_clean")
    keyboard.add(confirm_btn, cancel_btn)
    bot.send_message(
        message.chat.id,
        "⚠️ Do you want delete data of**History Table** and **Reset ALL User Daily Limits**? This cannot return!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@bot.message_handler(commands=['adminhistory_clean_25', 'adminhistory_clean_50', 'adminhistory_clean_75', 'adminhistory_clean_90'])
def admin_clean_percentage(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "🚫 You are not admin!")
        return

   
    try:
        command_part = message.text.split('_')[-1]
        percentage = int(command_part.replace('%', ''))
    except:
        bot.reply_to(message, "⚠️ Invalid clean command format. Use /adminhistory_clean_XX")
        return

    keyboard = types.InlineKeyboardMarkup()
    
    confirm_btn = types.InlineKeyboardButton(f"✅ Confirm Delete {percentage}% History (Oldest)", callback_data=f"confirm_clean_{percentage}")
    cancel_btn = types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_clean")
    keyboard.add(confirm_btn, cancel_btn)
    
    bot.send_message(
        message.chat.id,
        f"⚠️ ** Do you want to delete {percentage}% old data from History Table**? **Daily Limits will NOT be Reset!** This operation cannot returned!",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# CALLBACK HANDLER
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    # Only allow Admin to clean the DB
    if call.data.startswith("confirm_clean") or call.data == "cancel_clean":
        if call.from_user.id != ADMIN_ID:
            bot.answer_callback_query(call.id, "🚫 You are not authorized.")
            return
        
    bot.answer_callback_query(call.id) # Answer the query to remove the 'loading' status

    if call.data == "confirm_clean_all":
        # Full clean operation (Resets Limits AND Cleans History)
        
        # Edit message to show progress/waiting
        bot.edit_message_text("⏳ Full database cleanup and limit reset is in progress...", call.message.chat.id, call.message.message_id)

        if cleanup_all_history():
            
            bot.edit_message_text(
                "✅*Database Cleaned!* \n\n All **History data** has been cleaned and **All User Daily Limits** have been reset to 0.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown"
            )
        else:
            bot.edit_message_text("❌ A database clean operation error occurred.", call.message.chat.id, call.message.message_id)

    elif call.data.startswith("confirm_clean_"):
        # Percentage clean operation 
        try:
            percentage = int(call.data.split('_')[-1])
        except ValueError:
            bot.edit_message_text("❌ Clean error cannot be identified.", call.message.chat.id, call.message.message_id)
            return

        bot.edit_message_text(f"⏳ {percentage}% of history data delecting now...", call.message.chat.id, call.message.message_id)
        
        # Call the percentage cleanup function
        deleted_count = cleanup_percentage_history(percentage) 

        if deleted_count >= 0:
            bot.edit_message_text(
                f"✅ Database Cleaned! {percentage}% of the oldest history data (a total of {deleted_count} records) has been deleted.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="Markdown"
            )
        else:
            bot.edit_message_text(f"❌ Database clean operation has been occur error. (Deleted: {deleted_count})", call.message.chat.id, call.message.message_id)


    elif call.data == "cancel_clean":
        bot.edit_message_text(
            "❌ Database clean admin operation has been cancelled.",
            call.message.chat.id,
            call.message.message_id
        )

    elif call.data == "why_soft_limit":
        bot.send_message(
            call.message.chat.id,
            f"⏱️ Soft Limit (seconds {SOFT_LIMIT_SECONDS}): This limit ensures server stability and fair usage for everyone. Its purpose is to prevent the server from becoming overloaded.",
            parse_mode="Markdown"
        )

    elif call.data == "why_daily_limit":
        bot.send_message(
            call.message.chat.id,
            f"📅 Daily Limit ({DAILY_LIMIT} QR): This limit restricts QR code generation to {DAILY_LIMIT} per day in order to control spam and ensure uninterrupted service for all users.",
            parse_mode="Markdown"
        )

# === BACK BUTTON HANDLER ===
@bot.message_handler(func=lambda m: m.text == "🔙 Back")
def go_back_to_menu(message):
   
    user_id = message.from_user.id
    if user_id in active_users:
        active_users.remove(user_id) # Exit the generate mode
    menu_message(message) # Go back to main menu

# MAIN MENU BUTTON HANDLERS
@bot.message_handler(func=lambda m: m.text == "🌀 Generate QR Code")
def open_generate_from_button(message):
    start_generate(message)

@bot.message_handler(func=lambda m: m.text == "📅 History")
def open_history_from_button(message):
    show_history(message)

@bot.message_handler(func=lambda m: m.text == "ℹ️ About")
def open_about_from_button(message):
    about_message(message)

@bot.message_handler(func=lambda m: m.text == "🔒 Privacy & Policies")
def open_privacy_from_button(message):
    privacy_message(message)

@bot.message_handler(func=lambda m: m.text == "📞 Contact")
def open_contact_from_button(message):
    contract_message(message)

@bot.message_handler(func=lambda m: m.text == "🆘 Help")
def open_help_from_button(message):
    help_message(message)

# === QR TEXT HANDLER ===
@bot.message_handler(func=lambda m: True and m.content_type == 'text')
def generate_qr_from_text(message):
    user_id = message.from_user.id
    user_name = message.from_user.username or message.from_user.first_name
    text = message.text

    # Ignore messages if not in active mode and not a known command/button
    if user_id not in active_users:
        if text not in ["🌀 Generate QR Code", "📅 History", "ℹ️ About", "🔒 Privacy & Policies", "📞 Contact", "🆘 Help", "🔙 Back"]:
             bot.reply_to(message, "⚠️ Please click the **🌀 Generate QR Code** button on the menu or use the /generate command to start."
, parse_mode="Markdown")
        return

    # Content Moderation Check 
    is_harmful, reason = is_text_malicious(text)
    if is_harmful:
        if user_id != ADMIN_ID:
             # Send alert to Admin
             bot.send_message(ADMIN_ID, f"🚨 CONTENT VIOLATION attempt by User ID: {user_id}. Reason: {reason}. Text: `{text[:100]}...`")
        bot.reply_to(
            message,
            f"❌ **Content rejected.**\n"
            f"This Bot does not allow the generation of QR Codes for illegal, harmful or fraudulent content (including scams and phishing).\n"
            "Please adhere to the Terms of Use.",
            parse_mode="Markdown"
        )
        return

    #  Limit Checks
    can_generate, reason, remaining_time = can_generate_qr(user_id)
    keyboard = types.InlineKeyboardMarkup()

    if not can_generate:
        if reason == "soft":
            keyboard.add(types.InlineKeyboardButton("Why This Limit", callback_data="why_soft_limit"))
            bot.reply_to(message, f"❕Please wait another **{remaining_time} seconds**.", reply_markup=keyboard, parse_mode="Markdown")
        elif reason == "daily":
            keyboard.add(types.InlineKeyboardButton("Why This Limit", callback_data="why_daily_limit"))
            bot.reply_to(message, f"🛑 Daily Limit Reached. You have already generated **{DAILY_LIMIT}** QR codes. Please return tomorrow. If you want know about daily limit click button below.", reply_markup=keyboard, parse_mode="Markdown")
        return

    # Generation Logic
    # Update tracking before generation (Updates SQLite)
    update_qr_tracking(user_id)

    # 1. Generate QR Image to BytesIO (in-memory)
    qr_img = qrcode.make(text)
    bio = io.BytesIO() 
    qr_img.save(bio, format='PNG')
    bio.seek(0)
    
    # 2. Send 'Generating' status and then the final QR
    msg = bot.send_message(message.chat.id, "⏳ Please wait, generating Qr code...")

    # Send QR photo
    caption_text = text if len(text) < 200 else f"{text[:200]}..."

    bot.send_photo(message.chat.id, bio, caption=f"✅ Qr code has been successfully generated.\n\n📝 Text: `{caption_text}`", parse_mode="Markdown")
    
    # Delete the temporary status message
    try:
        bot.delete_message(message.chat.id, msg.message_id)
    except Exception:
        pass 

    # 3. Add to database history (Adds to SQLite History table)
    add_history(user_id, user_name, text)

    # 4. Start the 8-second live countdown in the chat
    threading.Thread(target=start_live_countdown, args=(message.chat.id,)).start()

# --- START BOT ---
if __name__ == '__main__':
    # 1. Database Initialization (Synchronous SQLite)
    if init_db():
        print("✅ Database Setup Complete. Starting Bot Polling...")
    else:
        print("❌ Critical Error: Could not initialize SQLite. Exiting.")
        exit() 

    # 2. Bot Polling (Synchronous)
    print("Bot is running...")
    bot.polling(none_stop=True, interval=0)
