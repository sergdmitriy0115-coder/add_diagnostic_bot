import os
import json
import logging
import threading
import re
import asyncio
import traceback
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- НАСТРОЙКИ (ТВОИ ДАННЫЕ) ---
BOT_TOKEN = "7582978597:AAFeGb-xtXNw8QYHH3aVrmxKO_3geWqFoTU"
ADMIN_ID = 7675037573
LOG_FILE = "logs.txt"

# --- GOOGLE SHEETS ---
SPREADSHEET_ID = "1NnAV6rZh5S95mDzX8IbzJHWRYYmYFpDdwuFaA72Gm-E"
WORKSHEET_NAME = "Диагностика"

# --- КОНТАКТЫ РУКОВОДИТЕЛЯ ---
MANAGER_CONTACTS = ["@Darya_Pril06", "@anny_nizh"]

# --- ВОПРОСЫ И КНОПКИ ---
QUESTIONS = [
    {
        "text": "📊 Сколько заявок в месяц поступает в ваш бизнес?",
        "buttons": [
            {"text": "До 10", "value": "до_10", "score": 0},
            {"text": "10–30", "value": "10_30", "score": 1},
            {"text": "30–100", "value": "30_100", "score": 2},
            {"text": "Более 100", "value": "более_100", "score": 3}
        ]
    },
    {
        "text": "👥 Есть ли сейчас менеджеры по продажам?",
        "buttons": [
            {"text": "Да, есть команда", "value": "есть_команда", "score": 2},
            {"text": "Нет, ищем", "value": "нет_ищем", "score": 1}
        ]
    },
    {
        "text": "🎯 Готовы ли нанимать и обучать менеджеров?",
        "buttons": [
            {"text": "Да, готовы", "value": "готовы", "score": 2},
            {"text": "Нет, ищем под ключ", "value": "под_ключ", "score": 1}
        ]
    },
    {
        "text": "💰 Какой бюджет на отдел продаж?",
        "buttons": [
            {"text": "До 100 000 ₽", "value": "до_100к", "score": 1},
            {"text": "100 000 – 300 000 ₽", "value": "100_300к", "score": 2},
            {"text": "Более 300 000 ₽", "value": "более_300к", "score": 3},
            {"text": "Не знаю", "value": "не_знаю", "score": 1}
        ]
    }
]

# --- РЕЗУЛЬТАТЫ ---
RESULTS = {
    "own": {
        "title": "🟢 Вам подходит собственный отдел продаж",
        "description": "Для компаний, которым выгоднее развивать внутреннюю команду и выстраивать системные продажи.",
        "details": "У вас стабильный поток заявок, есть бюджет и готовность вкладываться в развитие команды. Собственный отдел продаж даст вам полный контроль над процессами, возможность выстраивать долгосрочные отношения с клиентами и масштабировать бизнес без ограничений."
    },
    "outsource": {
        "title": "🟡 Вам выгоднее отдел продаж на аутсорсе",
        "description": "Для предпринимателей, которым важен быстрый результат без управления командой и найма.",
        "details": "Вы хотите получить результат без головной боли с наймом и обучением. Аутсорс-команда уже готова к работе, имеет отлаженные скрипты и может быстро запустить продажи. Это экономит ваше время и ресурсы."
    },
    "not_ready": {
        "title": "🔴 Пока строить отдел продаж рано",
        "description": "Сначала нужно увеличить поток заявок, доработать продукт или укрепить маркетинг.",
        "details": "Ваш текущий поток заявок или бюджет не позволяют эффективно запустить отдел продаж. Мы рекомендуем сначала сфокусироваться на маркетинге и увеличении входящего потока, а затем вернуться к вопросу построения отдела продаж."
    }
}

# --- Хранилище для ответов пользователя ---
user_answers = {}

# --- Логирование ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# === ВЕБ-СЕРВЕР ДЛЯ RENDER ===
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): pass

def run_health_server():
    try:
        server = HTTPServer(('0.0.0.0', 10000), HealthCheckHandler)
        logger.info("✅ Health check server started")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Health server error: {e}")

# === ОТПРАВКА ОШИБОК ===
async def send_error_notification(context, error_title, error_details, user_info=None):
    try:
        error_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"🚨 ОШИБКА В БОТЕ\n\n⏰ Время: {error_time}\n📌 Тип: {error_title}\n\n📋 Детали:\n{error_details[:1500]}\n"
        if user_info:
            message += f"\n👤 Пользователь: {user_info}"
        await context.bot.send_message(chat_id=ADMIN_ID, text=message)
        logger.info(f"✅ Уведомление отправлено")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки уведомления: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        error_details = traceback.format_exc()
        user_info = None
        if update and update.effective_user:
            user = update.effective_user
            user_info = f"{user.first_name} (@{user.username}) ID: {user.id}"
        logger.error(f"❌ Ошибка: {error_details}")
        await send_error_notification(context, "Исключение в обработчике", error_details, user_info)
    except Exception as e:
        logger.critical(f"❌ Критическая ошибка: {e}")

# === GOOGLE SHEETS ===
def init_google_sheets():
    logger.info("🔄 Подключение к Google Sheets...")
    try:
        # Твой JSON-ключ
        creds_dict = {
            "type": "service_account",
            "project_id": "my-project-add-01",
            "private_key_id": "6f2a80c82ef9bcf7882db291a82baf8acf9f4bfc",
            "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCnVxDJIYKp6cLk\nwISC6NKWYWVlDXq/9vGc580Z9sxwpDYLdFv3OYgAHG+bv00aPAFaWL127dZrNKbZ\ntWvcCH82MgjxK4rh1iUNdJDpluupuN+v6CsDkLDNudtpOfzVAq293ONzy07Oa/zC\nkaHQn4eAiLQIiAmXMuAIHyDjGa7O9D6kDqtS/K1p3Z40rVebbGKIZkex9sd1Traa\nmUlIxm66TT6CnlRUduaOW89APuQBO5BVIbvH5Yt5wGp3r915HUhSx7pn5bMm/MNa\n1N6d8LLReLmhPvFljgBmeqa4EvK+D1r15mDbNVxCB7ZH2UeJXHO8CJavAQuBkNpA\nxNEdePb7AgMBAAECggEAF54Mou5lVBjHZmSbbyRv8ERvvIrh+6zdHdGDW3o/EVjD\nveseva4zeRyKTfd6aMz2PeuPVfUsUXYdZFWcEvJqDdqS84K7N7NzCEqe1zDzMsGC\nZH/Gblrh8S8dfeTuv5uArO67dVDI3w5TnpxSM7EIPUZN7nRQsjO+dbb6+8JYryGE\nN0rQetP3avRB05R6nYtRKy5cHcUDF9gXq9pK0t0HWKbUGz2t6J+lQpvsdZuxIDy0\nLGxjekXp2viFmPYTWpnLmVIJTtZEl9oGjS0tk3iSrkAv1E05zdtcY2Tz/G++8Lj2\nHCADRxudKaLov4/3JfOdu+qAsl6QJhUCiqY6DRAcIQKBgQDPSort+SD53pblhsoG\na/8QKZdu6FABsaHXhG+LOtOvfaS3nxNVXhLUQ1LsonX8ATzMNY821057881SOAfD\nb/Q653vgz03/ymox8G5XZl+xKYYCLlc/Dhskti8nGZ5NfeNjYfYcWH/u7wuikvVP\n+Ns1c4jnB55sEBTLLxbDlMfhWwKBgQDOqUsQKDwJ7TwZdP00Jrl0rR6o2OXk5C1O\nkl2JDGN4Z/RT2ad/0alrecpKmT3V+GN/6M+z0gmZW9eYdrAbA3DegMw2kTNfcO0B\ngKkSKopKYfhVNYP8mN94bmZj47Ya/YAKiMuK7oq9yeJB4wIy0tb325UiAXCHtMNv\nU2tqHrKS4QKBgE7TALt3ZaO+kdDcDYydmpMxzaTd8DaEro8+jA/8oax08aLlebuX\nlz9iDnFvYcAfVFgu8bOf8fdOgUAHkGQv+UZA6ilVi0p+VR2CWOMhSbgbmxrPNlwC\n6C1wncOXiUvcWBBdmvGycYuRGPKMQX5Umj7cHS4FBqf/AXk2AckDlXJLAoGAS5+r\ntjfi8Ib9jRtAZMse5lFLfOISDlZpNe1diP8djzwLLnvhTWa9pnSkz/OPqzL/xhi9\nmMHtfU8cb9BO1TPHI8Th9b3gnLZIJFqeg+VJQbrkEtpIeDDA5eMQWNFFHE9TgYdZ\nZHeyEY1E3HNjpJF+1KhnxE/ei+pb8esGzYh6NEECgYBCsBkNYEv4fewsTcn1+hsA\n/GUZRDeiM7gh8u4zTj1dFfc5y5NaARshZTeEB8/ilk59ngB9xVyMirVtDk64GH4b\nlknZGZA32+txtswK3cKM1FgVvdLZKqpvlwobacGlD8b97TIeBBWiSLonVy/nWry8\n58dzVIH9BqBfq6c3naFfFw==\n-----END PRIVATE KEY-----\n",
            "client_email": "add-new-bot@my-project-add-01.iam.gserviceaccount.com",
            "client_id": "109256266568147492925",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": "https://www.googleapis.com/robot/v1/metadata/x509/add-new-bot%40my-project-add-01.iam.gserviceaccount.com",
            "universe_domain": "googleapis.com"
        }
        
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(SPREADSHEET_ID)
        
        try:
            worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
            logger.info("✅ Лист 'Диагностика' найден")
        except gspread.exceptions.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=WORKSHEET_NAME, rows=1000, cols=20)
            headers = ["Timestamp", "User ID", "Ник клиента", "Имя", "Сообщение", "Статус", "Заметки", "Источник", "Время ответа"]
            worksheet.append_row(headers)
            logger.info("✅ Создан новый лист с заголовками")
            
        return worksheet
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Google Sheets: {e}")
        return None

worksheet = init_google_sheets()

def log_to_sheets(user_id, username, first_name, answers, result):
    """Записывает данные в Google Sheets"""
    if not worksheet:
        logger.warning("⚠️ Пропускаем запись в таблицу: worksheet не инициализирован")
        return
    
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Формируем сообщение из ответов
        message = "\n".join([f"{i+1}. {a['answer']}" for i, a in enumerate(answers)])
        status = result['title']
        source = "Telegram бот"
        response_time = "—"  # TODO: можно добавить замер времени
        
        row = [
            timestamp,
            str(user_id),
            f"@{username}" if username else "нет",
            first_name or "нет",
            message,
            status,
            "",  # заметки
            source,
            response_time
        ]
        worksheet.append_row(row)
        logger.info(f"✅ Данные записаны в таблицу для {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка записи в таблицу: {e}")

# === КНОПКИ ===
def get_question_keyboard(question_index):
    keyboard = []
    for btn in QUESTIONS[question_index]["buttons"]:
        keyboard.append([InlineKeyboardButton(btn["text"], callback_data=f"q{question_index}_{btn['value']}")])
    return InlineKeyboardMarkup(keyboard)

# === ОСНОВНЫЕ ОБРАБОТЧИКИ ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"🚀 /start от {user.id}")

    welcome_text = (
        f"👋 Привет, {user.first_name}!\n\n"
        "Я помогу определить, какой формат отдела продаж подходит для вашего бизнеса.\n\n"
        "📌 Пройдите короткую диагностику из 4 вопросов, и я дам персональную рекомендацию.\n\n"
        "Готовы начать?"
    )
    keyboard = [[InlineKeyboardButton("🚀 Начать диагностику", callback_data="start_diagnostic")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    logger.info(f"🔘 Кнопка: {data} от {user_id}")

    if data == "start_diagnostic":
        user_answers[user_id] = []
        await query.edit_message_text(
            text=QUESTIONS[0]["text"],
            reply_markup=get_question_keyboard(0)
        )
        return

    if data.startswith("q"):
        parts = data.split("_")
        if len(parts) < 2:
            await query.edit_message_text("❌ Ошибка")
            return

        question_index = int(parts[0][1:])
        answer_value = "_".join(parts[1:])

        selected_answer = None
        for btn in QUESTIONS[question_index]["buttons"]:
            if btn["value"] == answer_value:
                selected_answer = btn
                break

        if not selected_answer:
            await query.edit_message_text("❌ Ошибка")
            return

        if user_id not in user_answers:
            user_answers[user_id] = []
        user_answers[user_id].append({
            "question": QUESTIONS[question_index]["text"],
            "answer": selected_answer["text"],
            "score": selected_answer["score"]
        })

        if question_index + 1 < len(QUESTIONS):
            await query.edit_message_text(
                text=QUESTIONS[question_index + 1]["text"],
                reply_markup=get_question_keyboard(question_index + 1)
            )
        else:
            total_score = sum(item["score"] for item in user_answers[user_id])
            logger.info(f"📊 Пользователь {user_id}: сумма баллов = {total_score}")

            if total_score >= 7:
                result_key = "own"
            elif total_score >= 4:
                result_key = "outsource"
            else:
                result_key = "not_ready"

            result = RESULTS[result_key]

            # Запись в Google Sheets
            log_to_sheets(user_id, query.from_user.username, query.from_user.first_name, user_answers[user_id], result)

            final_text = (
                f"{result['title']}\n\n"
                f"{result['description']}\n\n"
                f"📌 {result['details']}\n\n"
                "Хотите обсудить ваш результат с руководителем?"
            )
            keyboard = [
                [InlineKeyboardButton("📞 Связаться с руководителем", callback_data="contact_manager")],
                [InlineKeyboardButton("🔄 Пройти заново", callback_data="start_diagnostic")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(final_text, reply_markup=reply_markup)

            await notify_admin(context, user_id, query.from_user.username, query.from_user.first_name, result, user_answers[user_id])
            return

    elif data == "contact_manager":
        contacts = ", ".join(MANAGER_CONTACTS)
        await query.edit_message_text(
            f"📞 Свяжитесь с нашим руководителем:\n\n{contacts}\n\n"
            "Он поможет вам детально разобрать ваш кейс и предложит решение."
        )

async def notify_admin(context, user_id, username, first_name, result, answers):
    try:
        text = (
            f"🔔 **НОВЫЙ КЛИЕНТ ПРОШЕЛ ДИАГНОСТИКУ!**\n\n"
            f"👤 **Имя:** {first_name}\n"
            f"🆔 **ID:** `{user_id}`\n"
            f"📱 **Username:** @{username if username else 'нет'}\n\n"
            f"📊 **Результат:** {result['title']}\n\n"
            f"📋 **Ответы:**\n"
        )
        for i, ans in enumerate(answers, 1):
            text += f"{i}. {ans['answer']}\n"

        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode='Markdown')
        logger.info(f"✅ Уведомление админу отправлено для {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки уведомления: {e}")

# === ГЛАВНАЯ ===
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🚀 Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    logger.info("🔄 Запуск health check сервера")
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    main()
