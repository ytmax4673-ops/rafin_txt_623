#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import time
import shutil
import zipfile
import tarfile
import sqlite3
import signal
import ast
import importlib
import importlib.util
import html as html_lib
import logging
import json
import re
from datetime import datetime, timezone

# -------------------- স্বয়ংক্রিয় ডিপেন্ডেন্সি ইনস্টল --------------------
def install_requirements():
    requirements = [
        "pyTelegramBotAPI",
        "requests",
        "psutil"
    ]
    for package in requirements:
        try:
            if package == "pyTelegramBotAPI":
                import telebot
            elif package == "psutil":
                import psutil
            elif package == "requests":
                import requests
            print(f"✅ {package} আগে থেকেই ইনস্টল আছে")
        except ImportError:
            print(f"📦 {package} ইনস্টল করা হচ্ছে...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
                print(f"✅ {package} সফলভাবে ইনস্টল হয়েছে")
            except Exception as e:
                print(f"❌ {package} ইনস্টল করতে ব্যর্থ: {e}")

install_requirements()

import psutil
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# -------------------- কনফিগারেশন --------------------
BOT_TOKEN = "8652590549:AAEjicgMln7dJvHO5jNzNGlwinXZCsenCzo"   # আপনার টোকেন দিন
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "8110781954").split(",") if x.strip().isdigit()]

CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "90.0"))
MEMORY_THRESHOLD = float(os.environ.get("MEMORY_THRESHOLD", "90.0"))
MAX_RUNNING_PROCESSES = int(os.environ.get("MAX_RUNNING_PROCESSES", "15"))
MAX_FILES_PER_USER = int(os.environ.get("MAX_FILES_PER_USER", "10"))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "metadata.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")

for d in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    os.makedirs(d, exist_ok=True)

START_TIME = datetime.now(timezone.utc)

# -------------------- লগিং --------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------------------- ডাটাবেজ --------------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def init_db():
    with db_lock:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                filename TEXT,
                orig_name TEXT,
                path TEXT,
                uploaded_at TEXT,
                file_type TEXT,
                pid INTEGER,
                status TEXT DEFAULT 'Stopped',
                env_vars TEXT,
                auto_restart INTEGER DEFAULT 0
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                started_at TEXT,
                finished_at TEXT,
                pid INTEGER,
                log_path TEXT,
                exit_code INTEGER
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TEXT,
                last_seen TEXT
            )
        ''')
        conn.commit()

init_db()

# -------------------- ডাটাবেজ হেলপার --------------------
def add_file_record(user_id, username, filename, orig_name, path, file_type, env_vars=None):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id, username, filename, orig_name, path, uploaded_at, file_type, env_vars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, filename, orig_name, path, datetime.now(timezone.utc).isoformat(), file_type, json.dumps(env_vars) if env_vars else None)
        )
        conn.commit()
        return cur.lastrowid

def list_user_files(user_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, filename, orig_name, uploaded_at, file_type, status, pid FROM files WHERE user_id=? ORDER BY id DESC",
        (user_id,)
    )
    return cur.fetchall()

def get_file_record(file_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
    return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET pid=?, status=? WHERE id=?",
            (pid, status, file_id)
        )
        conn.commit()

def update_file_env(file_id, env_vars):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET env_vars=? WHERE id=?",
            (json.dumps(env_vars), file_id)
        )
        conn.commit()

def set_auto_restart(file_id, enabled):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET auto_restart=? WHERE id=?",
            (1 if enabled else 0, file_id)
        )
        conn.commit()

def get_all_users():
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, joined_at, last_seen FROM users ORDER BY joined_at DESC")
    return cur.fetchall()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO runs (file_id, started_at, pid, log_path) VALUES (?, ?, ?, ?)",
            (file_id, datetime.now(timezone.utc).isoformat(), pid, log_path)
        )
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE runs SET finished_at=?, exit_code=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), exit_code, run_id)
        )
        conn.commit()

# -------------------- প্রসেস ম্যানেজমেন্ট --------------------
processes = {}
proc_lock = threading.Lock()
auto_restart_flags = {}

def get_system_load():
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        proc_count = len(processes)
        disk = psutil.disk_usage('/').percent
        return float(cpu), float(mem), proc_count, disk
    except:
        return 0.0, 0.0, 0, 0

def should_stop_due_to_load():
    cpu, mem, proc_count, _ = get_system_load()
    if proc_count >= MAX_RUNNING_PROCESSES:
        return True, f"অনেক বেশি প্রসেস চলছে ({proc_count}/{MAX_RUNNING_PROCESSES})"
    if cpu >= CPU_THRESHOLD:
        return True, f"CPU লোড বেশি ({cpu:.1f}%)"
    if mem >= MEMORY_THRESHOLD:
        return True, f"মেমোরি ব্যবহার বেশি ({mem:.1f}%)"
    return False, None

# -------------------- ফাইল ইউটিলিটি --------------------
def get_file_type(filename):
    name = filename.lower()
    if name.endswith(".py"): return "python"
    if name.endswith(".js"): return "javascript"
    if name.endswith(".zip"): return "zip"
    if any(name.endswith(ext) for ext in [".tar", ".tar.gz", ".tgz"]): return "archive"
    if name.endswith(".env"): return "env"
    return "unknown"

def extract_archive(file_path, extract_dir):
    try:
        if file_path.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path, 'r') as z:
                z.extractall(extract_dir)
        elif file_path.lower().endswith((".tar.gz", ".tgz")):
            with tarfile.open(file_path, 'r:gz') as t:
                t.extractall(extract_dir)
        elif file_path.lower().endswith(".tar"):
            with tarfile.open(file_path, 'r') as t:
                t.extractall(extract_dir)
        else:
            return False, "অসমর্থিত আর্কাইভ ফরম্যাট"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(directory):
    priority = ["main.py", "bot.py", "app.py", "server.py", "index.py", "script.py",
                "main.js", "bot.js", "app.js", "server.js", "index.js", "script.js"]
    for root, _, files in os.walk(directory):
        for f in priority:
            if f in files:
                return os.path.join(root, f)
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith((".py", ".js")):
                return os.path.join(root, f)
    return None

def install_requirements_from_file(req_path, chat_id, file_name):
    try:
        if not os.path.exists(req_path):
            return True, "requirements.txt নেই"
        with open(req_path, 'r') as f:
            reqs = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        if not reqs:
            return True, "requirements.txt খালি"
        success = 0
        failed = []
        for pkg in reqs:
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                               capture_output=True, timeout=120, check=True)
                success += 1
            except Exception:
                failed.append(pkg)
        msg = f"{success} টি প্যাকেজ ইনস্টল করা হয়েছে"
        if failed:
            msg += f", {len(failed)} টি ব্যর্থ: {', '.join(failed[:5])}"
        return len(failed) == 0, msg
    except Exception as e:
        return False, f"ত্রুটি: {str(e)}"

def extract_imports(file_path):
    imports = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.add(node.module.split('.')[0])
    except:
        pass
    return imports

def install_missing_imports(imports, chat_id, file_name):
    missing = []
    for mod in imports:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return True, "সব ইমপোর্ট পাওয়া গেছে"
    pip_map = {'telebot': 'pyTelegramBotAPI', 'PIL': 'Pillow', 'cv2': 'opencv-python',
               'Crypto': 'pycryptodome', 'bs4': 'beautifulsoup4'}
    success = 0
    failed = []
    for mod in missing:
        pkg = pip_map.get(mod, mod)
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                           capture_output=True, timeout=120, check=True)
            success += 1
        except Exception:
            failed.append(mod)
    msg = f"{success} টি মডিউল ইনস্টল করা হয়েছে"
    if failed:
        msg += f", {len(failed)} টি ব্যর্থ: {', '.join(failed)}"
    return len(failed) == 0, msg

# -------------------- টেলিগ্রাম বট --------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ---------- কনফ্লিক্ট এড়াতে ওয়েবহুক রিমুভ ----------
try:
    bot.remove_webhook()
    time.sleep(0.5)
except:
    pass

# ---------- কীবোর্ড ----------
def main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("🌌 চ্যানেল"), KeyboardButton("📂 ফাইল আপলোড"))
    kb.add(KeyboardButton("📁 আমার প্রকল্প"), KeyboardButton("⚡ সিস্টেম স্ট্যাটাস"))
    kb.add(KeyboardButton("📊 পরিসংখ্যান"), KeyboardButton("👑 মালিককে ডাক"))
    kb.add(KeyboardButton("⏹️ সব বন্ধ কর"), KeyboardButton("ℹ️ বট সম্পর্কে"))
    return kb

def file_actions_kb(file_id, is_running, auto_restart):
    kb = InlineKeyboardMarkup(row_width=2)
    if is_running:
        kb.add(InlineKeyboardButton("⏹️ থামাও", callback_data=f"stop:{file_id}"),
               InlineKeyboardButton("🔄 পুনরায় চালু", callback_data=f"restart:{file_id}"))
    else:
        kb.add(InlineKeyboardButton("▶️ চালু কর", callback_data=f"start:{file_id}"),
               InlineKeyboardButton("🔄 পুনরায় চালু", callback_data=f"restart:{file_id}"))
    kb.add(InlineKeyboardButton("🗑️ মুছে ফেল", callback_data=f"delete:{file_id}"),
           InlineKeyboardButton("📜 লগ দেখ", callback_data=f"logs:{file_id}"))
    kb.add(InlineKeyboardButton("⬇️ লগ ডাউনলোড", callback_data=f"download_log:{file_id}"),
           InlineKeyboardButton("✏️ নাম বদলাও", callback_data=f"rename:{file_id}"))
    kb.add(InlineKeyboardButton("♻️ অটো-রিস্টার্ট", callback_data=f"auto:{file_id}") if not auto_restart else
           InlineKeyboardButton("✅ অটো-রিস্টার্ট চালু", callback_data=f"auto:{file_id}"))
    kb.add(InlineKeyboardButton("🔙 ফিরে যাও", callback_data="back_to_files"))
    return kb

def confirm_kb(action, file_id):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ হ্যাঁ", callback_data=f"confirm_{action}:{file_id}"),
           InlineKeyboardButton("❌ না", callback_data=f"cancel_{action}:{file_id}"))
    return kb

# ---------- হ্যান্ডলার ----------
@bot.message_handler(commands=['start', 'help'])
def start_handler(message):
    user = message.from_user
    user_id = user.id
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO users (user_id, username, joined_at, last_seen) VALUES (?, ?, ?, ?)",
            (user_id, user.username or "", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    files = list_user_files(user_id)
    welcome = f"""
🌌 <b><u>অলৌকিক জগতে স্বাগতম</u></b> 🌌

╔═══════════════════════════╗
║   <b>A L I E N   R A F I N   </b>   ║
╚═══════════════════════════╝

🚀 <b>সতর্কতা</b>: এই বট আপনার কোডকে <i>ছায়াপথের গভীরে</i> উড়িয়ে দেয়।
🌀 <i>অতীন্দ্রিয় শক্তিতে ২৪/৭ হোস্টিং</i>

👤 <b>ব্যবহারকারী</b>: {html_lib.escape(user.first_name or 'ব্যবহারকারী')}
🆔 <b>আইডি</b>: <code>{user_id}</code>
📂 <b>প্রকল্প</b>: {len(files)} / {MAX_FILES_PER_USER}

<u>🔮 যা যা করতে পারবেন</u>:
• 🌌 Python ও JS স্ক্রিপ্ট চালাতে পারবেন ২৪/৭
• 📦 ডিপেন্ডেন্সি ও ইমপোর্ট অটো‑ইনস্টল
• 📜 রিয়েল‑টাইম লগ ও প্রসেস কন্ট্রোল
• ♻️ ক্র্যাশ হলে অটো‑রিস্টার্ট (অপশনাল)
• 🌐 এনভায়রনমেন্ট ভেরিয়েবল সাপোর্ট
• 💀 <b>প্রেতাত্মা মোড</b> – ব্যাকগ্রাউন্ডে চলে

👇 <b>নিচের বাটনগুলো ব্যবহার করে আপনার স্ক্রিপ্ট চালান!</b>
"""
    bot.send_message(message.chat.id, welcome, reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🌌 চ্যানেল")
def updates_handler(m):
    bot.send_message(m.chat.id, "🌌 <b>আমাদের চ্যানেলে জয়েন করুন</b>\nhttps://t.me/RAFIN_TxT_BOT_X")

@bot.message_handler(func=lambda m: m.text == "👑 মালিককে ডাক")
def contact_handler(m):
    bot.send_message(m.chat.id, "👑 <b>মালিককে ডাকুন</b>: @alien_rafin")

@bot.message_handler(func=lambda m: m.text == "⚡ সিস্টেম স্ট্যাটাস")
def status_handler(m):
    cpu, mem, proc_count, disk = get_system_load()
    uptime = datetime.now(timezone.utc) - START_TIME
    days, rem = divmod(uptime.total_seconds(), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m"
    text = f"""
🌌 <b><u>সিস্টেম স্ট্যাটাস</u></b>

⚡ <b>CPU</b>: {cpu:.1f}%  💾 <b>মেমোরি</b>: {mem:.1f}%
💿 <b>ডিস্ক</b>: {disk:.1f}%  📌 <b>চলমান</b>: {proc_count} / {MAX_RUNNING_PROCESSES}
⏳ <b>আপটাইম</b>: {uptime_str}

🌀 <i>সিস্টেম ঠিকঠাক চলছে...</i>
"""
    bot.send_message(m.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📊 পরিসংখ্যান")
def stats_handler(m):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM files")
    users = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files")
    total_files = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
    running = cur.fetchone()[0] or 0
    cpu, mem, _, _ = get_system_load()
    text = f"""
🌌 <b><u>পরিসংখ্যান</u></b>

👥 <b>মোট ব্যবহারকারী</b>: {users}
📁 <b>মোট প্রকল্প</b>: {total_files}
🚀 <b>চলমান</b>: {running}
⚡ <b>CPU</b>: {cpu:.1f}%
💾 <b>মেমোরি</b>: {mem:.1f}%

🌀 <i>অনেক আত্মা এখানে আশ্রয় পেয়েছে...</i>
"""
    bot.send_message(m.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "ℹ️ বট সম্পর্কে")
def info_handler(m):
    cpu, mem, proc_count, disk = get_system_load()
    uptime = datetime.now(timezone.utc) - START_TIME
    uptime_str = str(uptime).split('.')[0]
    text = f"""
🌌 <b><u>বট সম্পর্কে</u></b>

🤖 <b>ভার্সন</b>: ৩.০ (মহাকাশীয়)
👨‍💻 <b>ডেভেলপার</b>: RAFIN TxT
📅 <b>আপটাইম</b>: {uptime_str}
💻 <b>সিস্টেম</b>: {sys.platform}
🐍 <b>Python</b>: {sys.version.split()[0]}

⚙️ <b>সীমা</b>:
• প্রতি ব্যবহারকারী সর্বোচ্চ প্রকল্প: {MAX_FILES_PER_USER}
• সর্বোচ্চ সমান্তরাল প্রসেস: {MAX_RUNNING_PROCESSES}
• CPU থ্রেশহোল্ড: {CPU_THRESHOLD}%
• মেমোরি থ্রেশহোল্ড: {MEMORY_THRESHOLD}%

📊 <b>বর্তমান লোড</b>:
• CPU: {cpu:.1f}%
• মেমোরি: {mem:.1f}%
• ডিস্ক: {disk:.1f}%
• সক্রিয় প্রসেস: {proc_count}

🌀 <i>এই বট চলে বিশুদ্ধ মহাজাগতিক শক্তিতে।</i>
"""
    bot.send_message(m.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📁 আমার প্রকল্প")
def my_files_handler(m):
    send_files_list(m.chat.id, m.from_user.id)

def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "🌌 <b>আপনার প্রকল্প</b>\n\nকোনো প্রকল্প আপলোড করা হয়নি। 'ফাইল আপলোড' বাটনে ক্লিক করুন।")
        return
    text = "🌌 <b>আপনার প্রকল্প</b>\n\nএকটি প্রকল্পে ট্যাপ করুন পরিচালনা করতে:"
    kb = InlineKeyboardMarkup()
    for f in files:
        file_id, filename, orig_name, uploaded, file_type, status, pid = f
        emoji = "🟢" if status == "Running" else "🔴"
        btn_text = f"{emoji} {orig_name} ({file_type})"
        kb.add(InlineKeyboardButton(btn_text, callback_data=f"manage:{file_id}"))
    bot.send_message(chat_id, text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📂 ফাইল আপলোড")
def upload_handler(m):
    bot.send_message(m.chat.id,
        "🌌 <b>একটি ফাইল আপলোড করুন</b>\n\n"
        "আমাকে <b>Python</b> (.py), <b>JavaScript</b> (.js) অথবা একটি <b>ZIP/TAR</b> আর্কাইভ পাঠান।\n\n"
        "✅ আর্কাইভ থাকলে আমি অটো‑এক্সট্র্যাক্ট করব, মূল ফাইল খুঁজে বের করব, ডিপেন্ডেন্সি ইনস্টল করব এবং চালু করব।\n"
        "✅ আপনি চাইলে একটি <b>.env</b> ফাইল আপলোড করে এনভায়রনমেন্ট ভেরিয়েবল সেট করতে পারেন।\n\n"
        "🌀 <i>বাকিটা আমি সামলাচ্ছি...</i>"
    )

@bot.message_handler(content_types=['document'])
def document_handler(message):
    user = message.from_user
    user_id = user.id
    if len(list_user_files(user_id)) >= MAX_FILES_PER_USER:
        bot.reply_to(message, f"❌ প্রকল্প সীমা অতিক্রম ({MAX_FILES_PER_USER})। প্রথমে কিছু ফাইল মুছুন।")
        return

    doc = message.document
    orig_name = doc.file_name or "unknown"
    file_type = get_file_type(orig_name)

    if file_type == "env":
        handle_env_upload(message, user_id, orig_name)
        return

    try:
        file_info = bot.get_file(doc.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ ডাউনলোড ব্যর্থ: {str(e)}")
        return

    user_dir = os.path.join(UPLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    safe_name = f"{int(time.time())}_{orig_name}"
    file_path = os.path.join(user_dir, safe_name)
    with open(file_path, 'wb') as f:
        f.write(file_bytes)

    final_path = file_path
    extracted_dir = None
    main_file = None

    if file_type in ("zip", "archive"):
        bot.reply_to(message, "🌀 আর্কাইভ এক্সট্র্যাক্ট করা হচ্ছে...")
        extracted_dir = os.path.join(TEMP_DIR, f"extracted_{user_id}_{int(time.time())}")
        os.makedirs(extracted_dir, exist_ok=True)
        ok, err = extract_archive(file_path, extracted_dir)
        if not ok:
            bot.reply_to(message, f"❌ এক্সট্র্যাক্ট ত্রুটি: {err}")
            os.remove(file_path)
            return
        main_file = find_main_file(extracted_dir)
        if not main_file:
            bot.reply_to(message, "❌ আর্কাইভে কোনো প্রধান Python/JS ফাইল পাওয়া যায়নি।")
            shutil.rmtree(extracted_dir, ignore_errors=True)
            os.remove(file_path)
            return
        final_path = extracted_dir
        file_type = get_file_type(main_file)

    env_vars = None
    if extracted_dir:
        env_path = os.path.join(extracted_dir, ".env")
        if os.path.exists(env_path):
            env_vars = parse_env_file(env_path)

    file_id = add_file_record(user_id, user.username, safe_name, orig_name, final_path, file_type, env_vars)

    if file_type in ("python", "javascript") and extracted_dir is None:
        bot.reply_to(message, f"✅ ফাইল <b>{html_lib.escape(orig_name)}</b> আপলোড হয়েছে! স্বয়ংক্রিয়ভাবে চালু করা হচ্ছে...")
        start_file_process(file_id, message.chat.id)
    else:
        bot.reply_to(message, f"✅ ফাইল <b>{html_lib.escape(orig_name)}</b> আপলোড হয়েছে! পরিচালনা করতে 'আমার প্রকল্প' এ যান।")

def handle_env_upload(message, user_id, orig_name):
    files = list_user_files(user_id)
    if not files:
        bot.reply_to(message, "❌ আপনার কোনো প্রকল্প নেই। প্রথমে একটি স্ক্রিপ্ট আপলোড করুন, তারপর .env দিন।")
        return
    latest = files[0]
    file_id = latest['id']
    try:
        file_info = bot.get_file(message.document.file_id)
        content = bot.download_file(file_info.file_path).decode('utf-8')
        env_vars = parse_env_content(content)
        update_file_env(file_id, env_vars)
        bot.reply_to(message, f"✅ এনভায়রনমেন্ট ভেরিয়েবল আপডেট করা হয়েছে <b>{html_lib.escape(latest['orig_name'])}</b> এর জন্য")
    except Exception as e:
        bot.reply_to(message, f"❌ .env পার্স করতে ব্যর্থ: {str(e)}")

def parse_env_file(path):
    try:
        with open(path, 'r') as f:
            return parse_env_content(f.read())
    except:
        return {}

def parse_env_content(content):
    env = {}
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            if '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

# -------------------- প্রসেস চালু/বন্ধ --------------------
def start_file_process(file_id, chat_id):
    should_stop, reason = should_stop_due_to_load()
    if should_stop:
        bot.send_message(chat_id, f"⚠️ চালু করা যাচ্ছে না: {reason}")
        return

    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ প্রকল্প পাওয়া যায়নি")
        return

    path = file_record["path"]
    orig_name = file_record["orig_name"]
    file_type = file_record["file_type"]
    env_vars = json.loads(file_record["env_vars"]) if file_record["env_vars"] else {}

    target_file = None
    working_dir = None
    if os.path.isdir(path):
        target_file = find_main_file(path)
        if not target_file:
            bot.send_message(chat_id, "❌ ডিরেক্টরিতে কোনো প্রধান ফাইল নেই")
            return
        working_dir = os.path.dirname(target_file)
    else:
        if not os.path.exists(path):
            bot.send_message(chat_id, f"❌ ফাইল নেই: {path}")
            return
        target_file = path
        working_dir = os.path.dirname(path)

    ext = os.path.splitext(target_file)[1].lower()

    if ext == ".py":
        req_path = os.path.join(working_dir, "requirements.txt")
        if os.path.exists(req_path):
            bot.send_message(chat_id, "🌀 requirements.txt ইনস্টল করা হচ্ছে...")
            ok, msg = install_requirements_from_file(req_path, chat_id, orig_name)
            bot.send_message(chat_id, f"📦 {msg}")
        bot.send_message(chat_id, "🌀 অনুপস্থিত ইমপোর্ট চেক করা হচ্ছে...")
        imports = extract_imports(target_file)
        if imports:
            ok, msg = install_missing_imports(imports, chat_id, orig_name)
            bot.send_message(chat_id, f"📦 {msg}")

    if ext == ".py":
        cmd = [sys.executable, target_file]
    elif ext == ".js":
        cmd = ["node", target_file]
    else:
        bot.send_message(chat_id, f"❌ অসমর্থিত ফাইল টাইপ: {ext}")
        return

    log_name = f"file_{file_id}_{int(time.time())}.log"
    log_path = os.path.join(LOGS_DIR, log_name)

    try:
        env = os.environ.copy()
        env.update(env_vars)
        with open(log_path, 'w') as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=working_dir,
                env=env,
                text=True
            )

        run_id = record_run_start(file_id, process.pid, log_path)
        update_file_status(file_id, process.pid, "Running")

        with proc_lock:
            processes[file_id] = {
                'process': process,
                'run_id': run_id,
                'log_path': log_path,
                'started_at': datetime.now(timezone.utc).isoformat()
            }

        bot.send_message(chat_id,
            f"✅ <b>{html_lib.escape(orig_name)}</b> চালু হয়েছে!\n"
            f"📝 PID: <code>{process.pid}</code>\n"
            f"📁 লগ: <code>{log_name}</code>\n"
            f"🌀 <i>আপনার স্ক্রিপ্ট এখন আমার দখলে...</i>"
        )

        def monitor():
            try:
                exit_code = process.wait()
            except:
                exit_code = -1
            finally:
                update_file_status(file_id, None, "Stopped")
                record_run_finish(run_id, exit_code)
                with proc_lock:
                    processes.pop(file_id, None)
                auto = get_file_record(file_id)
                if auto and auto['auto_restart']:
                    if exit_code != 0:
                        bot.send_message(chat_id, f"🔄 <b>{html_lib.escape(orig_name)}</b> ক্র্যাশ করেছে (exit {exit_code})। পুনরায় চালু করা হচ্ছে...")
                        time.sleep(2)
                        start_file_process(file_id, chat_id)
                    else:
                        bot.send_message(chat_id, f"✅ <b>{html_lib.escape(orig_name)}</b> ঠিকমতো শেষ হয়েছে।")
                else:
                    if exit_code != 0:
                        bot.send_message(chat_id, f"⚠️ <b>{html_lib.escape(orig_name)}</b> বন্ধ হয়েছে, exit code {exit_code}")

        threading.Thread(target=monitor, daemon=True).start()

    except Exception as e:
        bot.send_message(chat_id, f"❌ চালু করতে ব্যর্থ: {str(e)}")

def stop_file_process(file_id):
    with proc_lock:
        if file_id not in processes:
            return False
        proc_info = processes[file_id]
        proc = proc_info['process']
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        except:
            pass
        processes.pop(file_id, None)
    update_file_status(file_id, None, "Stopped")
    return True

def get_file_logs(file_id, lines=50):
    with proc_lock:
        if file_id in processes:
            log_path = processes[file_id]['log_path']
            if os.path.exists(log_path):
                with open(log_path, 'r') as f:
                    content = f.readlines()
                return ''.join(content[-lines:]) if content else "এখনো কোনো লগ নেই"
    cur = conn.cursor()
    cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1", (file_id,))
    row = cur.fetchone()
    if row and row[0] and os.path.exists(row[0]):
        with open(row[0], 'r') as f:
            content = f.readlines()
        return ''.join(content[-lines:]) if content else "কোনো লগ নেই"
    return "লগ ফাইল পাওয়া যায়নি"

# -------------------- কলব্যাক --------------------
pending_rename = {}

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = call.data
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    msg_id = call.message.message_id

    if data == "back_to_files":
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
        send_files_list(chat_id, user_id)
        return

    parts = data.split(":")
    action = parts[0]

    if action == "manage":
        file_id = int(parts[1])
        show_file_management(chat_id, file_id, user_id, msg_id)
    elif action == "start":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "চালু করা হচ্ছে...")
        start_file_process(file_id, chat_id)
        time.sleep(1)
        show_file_management(chat_id, file_id, user_id, msg_id)
    elif action == "stop":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "থামানো হচ্ছে...")
        stop_file_process(file_id)
        bot.send_message(chat_id, "⏹️ প্রসেস থামানো হয়েছে")
        time.sleep(1)
        show_file_management(chat_id, file_id, user_id, msg_id)
    elif action == "restart":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "পুনরায় চালু করা হচ্ছে...")
        stop_file_process(file_id)
        time.sleep(2)
        start_file_process(file_id, chat_id)
        time.sleep(1)
        show_file_management(chat_id, file_id, user_id, msg_id)
    elif action == "delete":
        file_id = int(parts[1])
        kb = confirm_kb("delete", file_id)
        bot.edit_message_text("🗑️ আপনি কি সত্যিই এই প্রকল্পটি মুছতে চান?", chat_id, msg_id, reply_markup=kb)
    elif action == "confirm_delete":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "মুছে ফেলা হচ্ছে...")
        file_record = get_file_record(file_id)
        if file_record:
            stop_file_process(file_id)
            fpath = file_record["path"]
            try:
                if os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
                elif os.path.exists(fpath):
                    os.remove(fpath)
            except:
                pass
            remove_file_record(file_id)
        bot.send_message(chat_id, "🗑️ প্রকল্প মুছে ফেলা হয়েছে")
        send_files_list(chat_id, user_id)
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
    elif action == "cancel_delete":
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
        show_file_management(chat_id, int(parts[1]), user_id, None)
    elif action == "logs":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "লগ আনছে...")
        logs = get_file_logs(file_id)
        file_record = get_file_record(file_id)
        fname = file_record["orig_name"] if file_record else "অজানা"
        if len(logs) > 4000:
            logs = logs[-4000:]
            logs = "... (ছাঁটা) ...\n" + logs
        text = f"🌌 <b>{html_lib.escape(fname)} এর লগ</b>\n\n<pre>{html_lib.escape(logs)}</pre>"
        bot.send_message(chat_id, text)
    elif action == "download_log":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "লগ প্রস্তুত করা হচ্ছে...")
        with proc_lock:
            if file_id in processes:
                log_path = processes[file_id]['log_path']
            else:
                cur = conn.cursor()
                cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1", (file_id,))
                row = cur.fetchone()
                log_path = row[0] if row and row[0] else None
        if log_path and os.path.exists(log_path):
            with open(log_path, 'rb') as f:
                bot.send_document(chat_id, f, caption="🌌 লগ ফাইল")
        else:
            bot.send_message(chat_id, "❌ লগ ফাইল পাওয়া যায়নি")
    elif action == "rename":
        file_id = int(parts[1])
        pending_rename[user_id] = file_id
        bot.send_message(chat_id, "✏️ এই প্রকল্পের নতুন নাম পাঠান (এক্সটেনশন সহ):")
    elif action == "auto":
        file_id = int(parts[1])
        file_record = get_file_record(file_id)
        if not file_record:
            bot.answer_callback_query(call.id, "প্রকল্প পাওয়া যায়নি")
            return
        current = file_record['auto_restart']
        new_val = 0 if current else 1
        set_auto_restart(file_id, new_val)
        bot.answer_callback_query(call.id, f"অটো‑রিস্টার্ট {'চালু' if new_val else 'বন্ধ'} করা হয়েছে")
        show_file_management(chat_id, file_id, user_id, msg_id)

@bot.message_handler(func=lambda m: m.from_user.id in pending_rename)
def rename_handler(message):
    user_id = message.from_user.id
    file_id = pending_rename.pop(user_id, None)
    if not file_id:
        return
    new_name = message.text.strip()
    if not new_name:
        bot.reply_to(message, "❌ নাম সঠিক নয়")
        return
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE files SET orig_name=? WHERE id=?", (new_name, file_id))
        conn.commit()
    bot.reply_to(message, f"✅ প্রকল্পের নাম পরিবর্তন করে <b>{html_lib.escape(new_name)}</b> রাখা হয়েছে")
    send_files_list(message.chat.id, user_id)

@bot.message_handler(func=lambda m: m.text == "⏹️ সব বন্ধ কর")
def stop_all_handler(message):
    user_id = message.from_user.id
    files = list_user_files(user_id)
    stopped = 0
    for f in files:
        if f['status'] == 'Running':
            if stop_file_process(f['id']):
                stopped += 1
    bot.send_message(message.chat.id, f"⏹️ {stopped} টি চলমান প্রসেস বন্ধ করা হয়েছে।")

# -------------------- অ্যাডমিন কমান্ড --------------------
@bot.message_handler(commands=['admin'])
def admin_handler(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ আপনি অ্যাডমিন নন।")
        return
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, COUNT(*) as file_count FROM files GROUP BY user_id")
    users = cur.fetchall()
    text = "👑 <b>অ্যাডমিন প্যানেল</b>\n\n"
    for u in users:
        text += f"👤 {u['username'] or 'অজানা'} (ID: {u['user_id']}) – {u['file_count']} টি প্রকল্প\n"
    with proc_lock:
        running = list(processes.keys())
    text += f"\n🚀 চলমান প্রসেস: {len(running)}"
    bot.send_message(message.chat.id, text)

# -------------------- প্রকল্প পরিচালনা দেখানো --------------------
def show_file_management(chat_id, file_id, user_id, message_id=None):
    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ প্রকল্প পাওয়া যায়নি")
        return
    if file_record["user_id"] != user_id and user_id not in ADMIN_IDS:
        bot.send_message(chat_id, "❌ অ্যাক্সেস নিষিদ্ধ")
        return

    is_running = file_id in processes
    auto_restart = bool(file_record['auto_restart'])

    status = "🟢 চলছে" if is_running else "🔴 বন্ধ"
    pid = f"\nPID: {file_record['pid']}" if file_record['pid'] else ""
    env = f"\nENV: {len(json.loads(file_record['env_vars']) if file_record['env_vars'] else {})} টি ভেরিয়েবল" if file_record['env_vars'] else ""

    text = f"""
🌌 <b>প্রকল্প পরিচালনা</b>

📁 <b>প্রকল্প</b>: {html_lib.escape(file_record['orig_name'])}
📊 <b>টাইপ</b>: {file_record['file_type']}
📈 <b>স্ট্যাটাস</b>: {status}{pid}{env}
⏰ <b>আপলোড</b>: {file_record['uploaded_at'][:16]}
♻️ <b>অটো‑রিস্টার্ট</b>: {'✅ চালু' if auto_restart else '❌ বন্ধ'}

🌀 <i>সাবধানে পরিচালনা করুন – এই প্রকল্প এখন আমার হাতে।</i>
"""
    kb = file_actions_kb(file_id, is_running, auto_restart)
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except:
            bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

# -------------------- বট চালু (প্রেতাত্মা মোড) --------------------
def run_daemon():
    try:
        if os.fork() > 0:
            sys.exit(0)
        os.setsid()
        if os.fork() > 0:
            sys.exit(0)
        sys.stdout = open('/dev/null', 'w')
        sys.stderr = open('/dev/null', 'w')
        os.chdir('/')
    except:
        pass

def start_bot():
    logger.info("🌌 RAFIN TxT NEBULA BOT (কনফ্লিক্ট ফিক্স) চালু হচ্ছে...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50)
        except Exception as e:
            logger.error(f"পোলিং ত্রুটি: {e}")
            time.sleep(5)

if __name__ == "__main__":
    # ব্যাকগ্রাউন্ডে চালাতে নিচের লাইনটি আনকমেন্ট করুন
    # run_daemon()
    start_bot()