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
GROUP_CHAT_ID = -1004446762925  # ← ID ГРУППЫ
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
            {"text": "До 10", "value": "do_10", "score": 0},
            {"text": "10–30", "value": "10_30", "score": 1},
            {"text": "30–100", "value": "30_100", "score": 2},
            {"text": "Более 100", "value": "bolee_100", "score": 3}
        ]
    },
    {
        "text": "👥 Есть ли сейчас менеджеры по продажам?",
        "buttons": [
            {"text": "Да, есть команда", "value": "est_komanda", "score": 2},
            {"text": "Нет, ищем", "value": "net_ishchem", "score": 1}
        ]
    },
    {
        "text": "🎯 Готовы ли нанимать и обучать менеджеров?",
        "buttons": [
            {"text": "Да, готовы", "value": "gotovy", "score": 2},
            {"text": "Нет, ищем под ключ", "value": "pod_kluch", "score": 1}
        ]
    },
    {
        "text": "💰 Какой бюджет на отдел продаж?",
        "buttons": [
            {"text": "До 100 000 ₽", "value": "do_100k", "score": 1},
            {"text": "100 000 – 300 000 ₽", "value": "100_300k", "score": 2},
            {"text": "Более 300 000 ₽", "value": "bolee_300k", "score": 3},
            {"text": "Не знаю", "value": "ne_znayu", "score": 1}
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
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# === ВЕБ-СЕРВЕР ДЛЯ RENDER ===
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args): 
        pass

def run_health_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
        logger.info(f"✅ Health check server started on port {port}")
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
        
        # Отправляем админу
        await context.bot.send_message(chat_id=ADMIN_ID, text=message)
        
        # Отправляем в группу
        if GROUP_CHAT_ID:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"⚠️ Ошибка в боте: {error_title[:100]}")
        
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
        creds_dict = {
            "type": "service_account",
            "project_id": "my-project-add-01",
            "private_key_id": "3e07a1216dbba1cd0a8fb64aa012775d9dbee8fb",
            "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQDBZC1EYrFD+Y5H\nUZEAD3g++E7fz0eVuz/wsLcHmHaMWunV42+NSkupJeC0AXaFOMeCY5d1bfx8+QI0\nZYv1zg6U4VzVosLb+SCr/jQUadXn+rf1CgV7R5p4EilCrdvti5ST+XQWwBNT4x/A\n1xfSZfnvrk0aHF9H0ErOPoQMjGDcjHXvxas0/BP8g+Fa97pbLh0uk2bNae5nOsCg\nmS/jaTVeBd3zQDbCzSEbForS3Vysjs0Zp50mMM5V1YmZ0xtIVlIUg4g9u6m8trHx\nrUeySs+0lGHpdLTQrTUcuUozKuOYzX/cHK/N+nNwGnvDUb8IgDinW2SBXn2ryqAG\ngJkBImddAgMBAAECggEABDZuAatDBcJJ+LXnOkYGqPEolK7+S0hu1OEgkjQzwYxv\nGLc8KZV9+Zix4PHsNpbdSl2S1x5sItPGGhqWgSZPLSvjOrhxgAWt/MPMN3VJESUm\nEdrOnO1wc7T05vcvDykHIERi5JD5YCPWf/v00KLjM3b2Rn8/jb//in0UTCADByLS\nud0Zw0NO7pZhcaUZWOiUUmsK/r54/n0152ZfdAmbcP3ArnRTKWSCIsFfAt2yp9Ez\nkJ+Hns0XZ0KL7F19IhOXPkP/FRlOwjt5SL+5NMJ3fSGznv/QU9q+l1A0jzSZtDmm\nvkXYmeaIombou1QPnpTWmdOb4966j2LnbliDjv4R+QKBgQD3zBsmCgSZAkR3LSy3\npvW8OZGem4bFdpPiaoanwGVpHRW0tzHHhenBlMXMACCponKNsE/TQ27LBPbqbWWu\n5aaeHxzjZV5l60pJGXRhIC1PEa1z0Pj+A4Qyz1YndB+4cNTKm5DV3xT0aQUgfdre\nSYUMiMCUeuH1bxTz8QLk6e18RQKBgQDHywV9USZmABgqxK+9YQMX1o+gwHxR2QTi\nBFmZroh04+isV9XlkVG984XDOAwuTp9xwh8Ionogit/+XZfJ+x3Pc2hnlsQxS7Ia\nlurErnorID7MYGdkT+Ox8Jf8A2mCXmhIec8x87bKfWJ4xbuwG9Vv0I+n2HfxKnhJ\nVhKPQNyMOQKBgHTbQipMKyLlGNiC60WobNZY570+Zu4UH2V1Cw9tAeXyG1xf0A/h\nrPznZefwX3bf7tm2vc5JTKRdMPwYnw09q7eBwKPUGBJERYH3iRSMkhFpqrylXeac\nTemQMXblolfejdsGReU2ELG6HPrXnzGYxi/FBdx/nrOZsO3hSJYfYylpAoGAUMlj\nGt0pba00GHcXqLgFjCoSQaoTmvTp6IphwKa2Pq25c5bAwucT6n8B44JSSpc4GcOo\n0NECGQ6OrEgkDGQiFbRQzzJDertk9SN5IrZ6Z93OBs4kgIddRqJGkny+uRx7hnLa\nuRQXIaG5o6Qw1HEsyN3IeNIrDbViliTbtFlB1OECgYAieyFWJh+Fr1aM2i8rpI6E\n1wD5hDWjNvMz3NLh+KOHqa4pw6lBd/w37HOv1rwlnM6z8I7zfuVTVgKC+Z8OrQrA\nAQkujMDlJKxOw8ghDgDAAGDbxlWuB/opAiaKr590djmVxyovBCuW+he5T1pxoVEW\nNUC9Kr1xOfQaZu5iQG0YFA==\n-----END PRIVATE KEY-----\n",
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
        message = "\n".join([f"{i+1}. {a['answer']}" for i, a in enumerate(answers)])
        status = result['title']
        source = "Telegram бот"
        response_time = "—"
        
        row = [
            timestamp,
            str(user_id),
            f"@{username}" if username else "нет",
            first_name or "нет",
            message,
            status,
            "",
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

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = (
        "🤖 **Помощь по боту**\n\n"
        "📌 **Команды:**\n"
        "/start - Начать работу с ботом\n"
        "/help - Показать эту справку\n\n"
        "📊 **Диагностика:**\n"
        "Бот задаст 4 вопроса о вашем бизнесе и даст рекомендацию.\n\n"
        "📞 **Контакты руководителя:**\n"
        f"{', '.join(MANAGER_CONTACTS)}\n\n"
        "❓ По всем вопросам обращайтесь к администратору."
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def get_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Временная команда для получения chat_id группы"""
    chat_id = update.message.chat.id
    chat_type = update.message.chat.type
    await update.message.reply_text(
        f"📌 **Информация о чате**\n\n"
        f"🆔 Chat ID: `{chat_id}`\n"
        f"📋 Тип: {chat_type}\n\n"
        f"Скопируйте этот ID и вставьте в код: `GROUP_CHAT_ID = {chat_id}`"
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    logger.info(f"🔘 Кнопка: {data} от {user_id}")

    # 1. ОБРАБОТКА СТАРТА ДИАГНОСТИКИ
    if data == "start_diagnostic":
        user_answers[user_id] = []
        await query.edit_message_text(
            text=QUESTIONS[0]["text"],
            reply_markup=get_question_keyboard(0)
        )
        return

    # 2. ОБРАБОТКА КНОПКИ "СВЯЗАТЬСЯ С РУКОВОДИТЕЛЕМ"
    if data == "contact_manager":
        contacts = ", ".join(MANAGER_CONTACTS)
        await query.edit_message_text(
            f"📞 Свяжитесь с нашим руководителем:\n\n{contacts}\n\n"
            "Он поможет вам детально разобрать ваш кейс и предложит решение."
        )
        return

    # 3. ОБРАБОТКА ОТВЕТОВ НА ВОПРОСЫ
    if data.startswith("q"):
        parts = data.split("_")
        if len(parts) < 2:
            await query.edit_message_text("❌ Ошибка в данных кнопки")
            return

        # Извлекаем индекс вопроса (убираем "q")
        question_index = int(parts[0][1:])
        # Собираем ответ (все части после индекса)
        answer_value = "_".join(parts[1:])

        # Ищем выбранный ответ
        selected_answer = None
        for btn in QUESTIONS[question_index]["buttons"]:
            if btn["value"] == answer_value:
                selected_answer = btn
                break

        if not selected_answer:
            await query.edit_message_text("❌ Ошибка: ответ не найден")
            return

        # Сохраняем ответ
        if user_id not in user_answers:
            user_answers[user_id] = []
        user_answers[user_id].append({
            "question": QUESTIONS[question_index]["text"],
            "answer": selected_answer["text"],
            "score": selected_answer["score"]
        })

        # Показываем следующий вопрос или результат
        if question_index + 1 < len(QUESTIONS):
            await query.edit_message_text(
                text=QUESTIONS[question_index + 1]["text"],
                reply_markup=get_question_keyboard(question_index + 1)
            )
        else:
            # Подсчёт результата
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

            # Отправка в группу
            await send_to_group(context, user_id, query.from_user.username, query.from_user.first_name, result, user_answers[user_id])

            # Отправка админу
            await notify_admin(context, user_id, query.from_user.username, query.from_user.first_name, result, user_answers[user_id])

            # Финальное сообщение
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
            return

    # 4. ЕСЛИ НИЧЕГО НЕ ПОДОШЛО
    await query.edit_message_text("❌ Неизвестная команда. Используйте /start")

# === ОТПРАВКА В ГРУППУ ===
async def send_to_group(context, user_id, username, first_name, result, answers):
    """Отправляет уведомление в группу Telegram"""
    if not GROUP_CHAT_ID:
        logger.warning("⚠️ GROUP_CHAT_ID не задан, пропускаем отправку в группу")
        return
    
    try:
        # Формируем красивое сообщение
        text = (
            f"🔔 **НОВАЯ ДИАГНОСТИКА!**\n\n"
            f"👤 **Клиент:** {first_name}\n"
            f"🆔 **ID:** `{user_id}`\n"
            f"📱 **Username:** @{username if username else 'нет'}\n"
            f"⏰ **Время:** {datetime.now().strftime('%H:%M:%S')}\n\n"
            f"📊 **Результат:** {result['title']}\n\n"
            f"📋 **Ответы:**\n"
        )
        
        for i, ans in enumerate(answers, 1):
            text += f"{i}. {ans['answer']}\n"
        
        # Добавляем кнопку для связи
        keyboard = [[InlineKeyboardButton("📞 Связаться с клиентом", callback_data=f"contact_{user_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Отправляем в группу
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID, 
            text=text, 
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        logger.info(f"✅ Уведомление отправлено в группу для {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки в группу: {e}")

# === УВЕДОМЛЕНИЕ АДМИНА ===
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

    # Добавляем обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("getchatid", get_chat_id))
    
    # Добавляем обработчик кнопок
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🚀 Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    logger.info("🔄 Запуск health check сервера")
    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()
    main()
