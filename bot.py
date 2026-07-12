import os
import json
import sqlite3
import logging
import threading
import traceback
import asyncio
from html import escape
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен!")

ADMIN_ID = 7675037573
SPREADSHEET_ID = "1NnAV6rZh5S95mDzX8IbzJHWRYYmYFpDdwuFaA72Gm-E"
WORKSHEET_NAME = "Диагностика"
MANAGER_CONTACTS = ["@Darya_Pril06", "@anny_nizh"]
DB_NAME = "bot_data.db"

# --- ВОПРОСЫ ---
QUESTIONS = [
    {"text": "📊 Сколько заявок в месяц поступает в ваш бизнес?",
     "buttons": [{"text": "До 10", "value": "до_10", "score": 0},
                 {"text": "10–30", "value": "10_30", "score": 1},
                 {"text": "30–100", "value": "30_100", "score": 2},
                 {"text": "Более 100", "value": "более_100", "score": 3}]},
    {"text": "👥 Есть ли сейчас менеджеры по продажам?",
     "buttons": [{"text": "Да, есть команда", "value": "есть_команда", "score": 2},
                 {"text": "Нет, ищем", "value": "нет_ищем", "score": 1}]},
    {"text": "🎯 Готовы ли нанимать и обучать менеджеров?",
     "buttons": [{"text": "Да, готовы", "value": "готовы", "score": 2},
                 {"text": "Нет, ищем под ключ", "value": "под_ключ", "score": 1}]},
    {"text": "💰 Какой бюджет на отдел продаж?",
     "buttons": [{"text": "До 100 000 ₽", "value": "до_100к", "score": 1},
                 {"text": "100 000 – 300 000 ₽", "value": "100_300к", "score": 2},
                 {"text": "Более 300 000 ₽", "value": "более_300к", "score": 3},
                 {"text": "Не знаю", "value": "не_знаю", "score": 1}]}
]

RESULTS = {
    "own": {
        "title": "🟢 Вам подходит собственный отдел продаж",
        "description": "Для компаний, которым выгоднее развивать внутреннюю команду.",
        "details": "У вас стабильный поток заявок, есть бюджет и готовность вкладываться в развитие команды."
    },
    "outsource": {
        "title": "🟡 Вам выгоднее отдел продаж на аутсорсе",
        "description": "Для предпринимателей, которым важен быстрый результат без управления командой.",
        "details": "Вы хотите получить результат без головной боли с наймом и обучением."
    },
    "not_ready": {
        "title": "🔴 Пока строить отдел продаж рано",
        "description": "Сначала нужно увеличить поток заявок или доработать продукт.",
        "details": "Ваш текущий поток заявок или бюджет не позволяют эффективно запустить отдел продаж."
    }
}

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --- SQLITE (ИСПРАВЛЕНО) ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10, isolation_level=None) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS user_progress (
            user_id INTEGER PRIMARY KEY,
            step INTEGER DEFAULT 0,
            answers TEXT DEFAULT '[]',
            topic_id INTEGER DEFAULT 0
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS completed_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            result_key TEXT,
            answers TEXT,
            timestamp TEXT
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_user_id ON completed_tests(user_id)')
        conn.commit()

def get_db_connection():
    conn = sqlite3.connect(DB_NAME, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn

def get_user_progress(user_id):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT step, answers, topic_id FROM user_progress WHERE user_id=?", (user_id,))
        row = c.fetchone()
        if row:
            return {"step": row["step"], "answers": json.loads(row["answers"]), "topic_id": row["topic_id"]}
    return {"step": 0, "answers": [], "topic_id": 0}

def update_user_progress(user_id, step, answers, topic_id=0):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO user_progress (user_id, step, answers, topic_id)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
               step=excluded.step,
               answers=excluded.answers,
               topic_id=excluded.topic_id""",
            (user_id, step, json.dumps(answers, ensure_ascii=False), topic_id)
        )
        conn.commit()

def save_completed_test(user_id, result_key, answers):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO completed_tests (user_id, result_key, answers, timestamp) VALUES (?,?,?,?)",
            (user_id, result_key, json.dumps(answers, ensure_ascii=False), datetime.now().isoformat())
        )
        conn.commit()

def clear_user_progress(user_id):
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM user_progress WHERE user_id=?", (user_id,))
        conn.commit()

# --- GOOGLE SHEETS (ИСПРАВЛЕНО) ---
class GoogleSheets:
    def __init__(self):
        self.ws = None

    def reconnect(self):
        try:
            creds_json = os.getenv("GOOGLE_CREDS_JSON")
            if not creds_json:
                logger.error("❌ GOOGLE_CREDS_JSON не найден")
                self.ws = None
                return False

            creds_dict = json.loads(creds_json)
            scope = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            client = gspread.authorize(creds)
            spreadsheet = client.open_by_key(SPREADSHEET_ID)

            try:
                self.ws = spreadsheet.worksheet(WORKSHEET_NAME)
            except gspread.exceptions.WorksheetNotFound:
                self.ws = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=10)
                headers = ["Timestamp", "User ID", "Ник клиента", "Имя", "Ответы", "Результат"]
                self.ws.append_row(headers)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Google Sheets: {e}")
            self.ws = None
            return False

    async def append(self, row):
        if self.ws is None:
            if not self.reconnect():
                logger.error("❌ Не удалось подключиться к Google Sheets")
                return

        try:
            await asyncio.to_thread(self.ws.append_row, row)
            logger.info("✅ Данные записаны в Google Sheets")
        except Exception as e:
            logger.error(f"❌ Ошибка записи: {e}")
            self.ws = None
            if self.reconnect():
                try:
                    await asyncio.to_thread(self.ws.append_row, row)
                    logger.info("✅ Данные записаны повторно")
                except Exception as e2:
                    logger.error(f"❌ Повторная запись не удалась: {e2}")

sheets = GoogleSheets()

# --- ОБЁРТКА ДЛЯ ФОНОВОЙ ЗАПИСИ ---
async def save_to_sheets(row):
    try:
        await sheets.append(row)
    except Exception:
        logger.exception("Ошибка записи в Google Sheets")

# --- HEALTH CHECK (ИСПРАВЛЕНО) ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    allow_reuse_address = True

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def run_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    logger.info(f"✅ Health check server started on port {port}")
    server.serve_forever()

# --- ОБРАБОТЧИК ОШИБОК ---
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        error_details = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
        user_info = f"{update.effective_user.first_name} (@{update.effective_user.username}) ID: {update.effective_user.id}" if update and update.effective_user else None

        logger.error(f"❌ Ошибка: {error_details}")

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🚨 ОШИБКА В БОТЕ\n\n"
                f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"📋 {error_details[:1500]}\n\n"
                f"👤 {user_info if user_info else 'Неизвестно'}"
            )
        )
    except Exception as e:
        logger.critical(f"❌ Ошибка в обработчике ошибок: {e}")

# --- КНОПКИ ---
def get_question_keyboard(question_index):
    keyboard = []
    for btn in QUESTIONS[question_index]["buttons"]:
        keyboard.append([InlineKeyboardButton(btn["text"], callback_data=f"q:{question_index}:{btn['value']}")])
    return InlineKeyboardMarkup(keyboard)

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    clear_user_progress(user.id)

    welcome_text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Я помогу определить, какой формат отдела продаж подходит для вашего бизнеса.\n\n"
        "📌 Пройдите короткую диагностику из 4 вопросов, и я дам персональную рекомендацию."
    )
    keyboard = [[InlineKeyboardButton("🚀 Начать диагностику", callback_data="start_diagnostic")]]
    await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    logger.debug(f"🔘 Кнопка: {data} от {user_id}")

    if data == "start_diagnostic":
        progress = get_user_progress(user_id)
        if progress["step"] == 0:
            update_user_progress(user_id, 0, [])
        await query.edit_message_text(
            text=QUESTIONS[0]["text"],
            reply_markup=get_question_keyboard(0)
        )
        return

    elif data.startswith("q:"):
        try:
            _, question_index_str, answer_value = data.split(":")
            question_index = int(question_index_str)
        except ValueError:
            await query.edit_message_text("❌ Ошибка обработки данных. Попробуйте снова.")
            return

        progress = get_user_progress(user_id)

        if question_index != progress["step"]:
            await query.edit_message_text("⏳ Пожалуйста, отвечайте на текущий вопрос.")
            return

        selected_answer = next(
            (btn for btn in QUESTIONS[question_index]["buttons"] if btn["value"] == answer_value),
            None
        )

        if not selected_answer:
            await query.edit_message_text("❌ Ошибка")
            return

        answers = progress["answers"]
        answers.append({"question": QUESTIONS[question_index]["text"], "answer": selected_answer["text"], "score": selected_answer["score"]})
        next_step = question_index + 1

        if next_step < len(QUESTIONS):
            update_user_progress(user_id, next_step, answers)
            await query.edit_message_text(
                text=QUESTIONS[next_step]["text"],
                reply_markup=get_question_keyboard(next_step)
            )
        else:
            total_score = sum(item["score"] for item in answers)
            logger.info(f"📊 Пользователь {user_id}: сумма баллов = {total_score}")

            if total_score >= 7:
                result_key = "own"
            elif total_score >= 4:
                result_key = "outsource"
            else:
                result_key = "not_ready"

            result = RESULTS[result_key]

            save_completed_test(user_id, result_key, answers)

            # Фоновая запись в Google Sheets
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            answers_text = "\n".join([f"{i+1}. {a['answer']}" for i, a in enumerate(answers)])
            result_title = RESULTS[result_key]["title"]
            row = [
                timestamp,
                str(user_id),
                f"@{query.from_user.username}" if query.from_user.username else "нет",
                query.from_user.first_name or "нет",
                answers_text,
                result_title
            ]
            asyncio.create_task(save_to_sheets(row))

            final_text = f"{result['title']}\n\n{result['description']}\n\n📌 {result['details']}\n\nХотите обсудить ваш результат с руководителем?"
            keyboard = [
                [InlineKeyboardButton("📞 Связаться с руководителем", callback_data="contact_manager")],
                [InlineKeyboardButton("🔄 Пройти заново", callback_data="start_diagnostic")]
            ]
            await query.edit_message_text(final_text, reply_markup=InlineKeyboardMarkup(keyboard))

            await notify_admin(context, user_id, query.from_user.username, query.from_user.first_name, result, answers)
            clear_user_progress(user_id)
            return

    elif data == "contact_manager":
        contacts = ", ".join(MANAGER_CONTACTS)
        await query.edit_message_text(f"📞 Свяжитесь с нашим руководителем:\n\n{contacts}\n\nОн поможет вам детально разобрать ваш кейс и предложит решение.")

# --- УВЕДОМЛЕНИЕ АДМИНУ ---
async def notify_admin(context, user_id, username, first_name, result, answers):
    try:
        safe_first_name = escape(first_name or "нет")
        safe_username = escape(username) if username else "нет"
        safe_title = escape(result["title"])

        answers_text = ""
        for i, ans in enumerate(answers, 1):
            emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"][i-1] if i <= 4 else f"{i}."
            safe_question = escape(ans['question'])
            safe_answer = escape(ans['answer'])
            answers_text += f"{emoji} <b>{safe_question}</b>\n{safe_answer}\n\n"

        text = (
            f"🔔 <b>НОВЫЙ КЛИЕНТ ПРОШЕЛ ДИАГНОСТИКУ!</b>\n\n"
            f"👤 <b>Имя:</b> {safe_first_name}\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
            f"📱 <b>Username:</b> @{safe_username}\n\n"
            f"📊 <b>Результат:</b> {safe_title}\n\n"
            f"📋 <b>Ответы:</b>\n{answers_text}"
        )

        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode='HTML')
        logger.info(f"✅ Уведомление отправлено для {user_id}")
    except Exception:
        logger.exception(f"❌ Ошибка отправки уведомления для {user_id}")

# --- ГЛАВНАЯ ---
if __name__ == "__main__":
    init_db()
    sheets.reconnect()

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🚀 Бот успешно запущен!")
    app.run_polling()
