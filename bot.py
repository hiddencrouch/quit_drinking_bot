import logging
import sqlite3
import pytz
from datetime import datetime, time, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    CallbackQueryHandler, ConversationHandler, MessageHandler, filters
)

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8504635044:AAFGM95ucHqlQ4E_oxM8Rt3wTEeZpmezXnk"
DB_NAME = "quit.db"

# График шагов (Шаг: День от старта)
SCHEDULE = {i: i for i in range(1, 11)}  # 1-10
SCHEDULE.update({
    11: 12, 12: 14, 13: 16, 14: 18, 15: 20, 16: 22, 17: 24, 18: 26, 19: 28, 20: 30,
    21: 33, 22: 36, 23: 39, 24: 42, 25: 45, 26: 48, 27: 51, 28: 54, 29: 58, 30: 62,
    31: 66, 32: 70, 33: 74, 34: 78, 35: 82, 36: 87, 37: 92, 38: 97, 39: 102, 40: 107,
    41: 112, 42: 117, 43: 122, 44: 127, 45: 132, 46: 137, 47: 142, 48: 147, 49: 152, 50: 157
})

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)


# --- РАБОТА С БД ---
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def db_get_user(user_id):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def db_upsert_user(user_id, **kwargs):
    conn = get_db_connection()
    # Если пользователя нет, создаем с дефолтными значениями
    conn.execute("INSERT OR IGNORE INTO users (id, step, start_date) VALUES (?, 0, NULL)", (user_id,))

    updates = []
    params = []
    for k, v in kwargs.items():
        updates.append(f"{k} = ?")
        params.append(v)

    if updates:
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)

    conn.commit()
    conn.close()


def db_get_content(step_id):
    conn = get_db_connection()
    step_url = conn.execute("SELECT url FROM steps WHERE id = ?", (step_id,)).fetchone()
    article = conn.execute("SELECT title, url FROM articles WHERE id = ?", (step_id,)).fetchone()
    conn.close()
    return step_url, article


# --- ЛОГИКА УВЕДОМЛЕНИЙ ---

async def send_step_notification(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    user_id = job.user_id
    user = db_get_user(user_id)

    if not user or not user['start_date']:
        return

    step_num = user['step'] + 1  # Следующий шаг

    # Получаем контент
    step_row, article_row = db_get_content(step_num)

    text = f"📅 **Шаг {step_num}**\n\n"
    if step_row:
        text += f"📝 [Дневник №{step_num}]({step_row['url']})\n"

    # Статьи отправляем только для первых 10 шагов (по условию)
    if step_num <= 10 and article_row:
        text += f"📖 Статья: [{article_row['title']}]({article_row['url']})\n"

    keyboard = [[InlineKeyboardButton("✅ Выполнено", callback_data=f"done_{step_num}")]]

    await context.bot.send_message(
        chat_id=user_id,
        text=text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def schedule_next_job(user_id, application, force_now=False):
    """
    Планирует следующий шаг.
    Для 1-го шага: отправляет сразу (или в день старта).
    Для остальных: считает разницу дней по графику и прибавляет к дате ПОСЛЕДНЕГО выполненного шага.
    """
    user = db_get_user(user_id)
    if not user:
        return

    current_step = user['step']
    next_step = current_step + 1

    if next_step > 50:
        return  # Курс завершен

    # 1. Определяем базовую дату (от чего отсчитываем) и сколько дней ждать
    if current_step == 0:
        # Если это самый первый шаг — базой является дата старта
        if not user['start_date']: return
        base_date = datetime.strptime(user['start_date'], "%Y-%m-%d").date()
        days_to_add = 0  # Первый шаг в день старта (или сразу, если force_now)
    else:
        # Если шаг > 0, считаем разницу между текущим и следующим по графику
        prev_schedule_day = SCHEDULE.get(current_step)
        next_schedule_day = SCHEDULE.get(next_step)

        if not prev_schedule_day or not next_schedule_day:
            return  # Ошибка в графике

        days_to_add = next_schedule_day - prev_schedule_day

        # Базой является дата выполнения ПРЕДЫДУЩЕГО шага
        if not user['step_date']:
            # Если вдруг нет даты выполнения, используем дату старта как аварийный вариант
            base_date = datetime.strptime(user['start_date'], "%Y-%m-%d").date()
        else:
            base_date = datetime.strptime(user['step_date'], "%Y-%m-%d %H:%M:%S").date()

    # 2. Вычисляем целевую дату
    target_date = base_date + timedelta(days=days_to_add)

    # 3. Формируем время уведомления с учетом часового пояса
    notif_hour = user['notification_time'] if user['notification_time'] is not None else 9
    tz_offset = int(user['timezone']) if user['timezone'] else 0

    target_dt = datetime.combine(target_date, time(hour=notif_hour)) - timedelta(hours=tz_offset)
    target_dt = pytz.utc.localize(target_dt)

    now = datetime.now(pytz.utc)

    # 4. Очистка старых задач и планирование новой
    current_jobs = application.job_queue.get_jobs_by_name(str(user_id))
    for job in current_jobs:
        job.schedule_removal()

    # Если это первый шаг и он вызван при настройке (force_now), или время уже прошло
    if force_now or target_dt <= now:
        # Если это не первый шаг и время прошло сегодня — отправляем сразу.
        # Но если "просрочено" на несколько дней, бот все равно отправит сразу, чтобы догнать.
        application.job_queue.run_once(send_step_notification, 5, user_id=user_id, name=str(user_id))
    else:
        delay = (target_dt - now).total_seconds()
        application.job_queue.run_once(send_step_notification, delay, user_id=user_id, name=str(user_id))


# --- ХЕНДЛЕРЫ ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_upsert_user(user_id)  # Создаем запись, если нет

    text = (
        "Привет! Я бот для сопровождения курса по методу Шичко.\n"
        "Мы пройдем 50 шагов к свободе.\n\n"
        "Для настройки мне нужно знать ваш часовой пояс (смещение от UTC) и желаемое время уведомлений."
    )
    keyboard = [[InlineKeyboardButton("🚀 Начать настройку", callback_data="setup_start")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "setup_start":
        await query.edit_message_text(
            "Введите ваше смещение от UTC (например, для Москвы +3 введите `3`, для Европы `1`):")
        return 1  # Состояние WAIT_TZ

    if data.startswith("done_"):
        step_done = int(data.split("_")[1])
        # Фиксируем выполнение
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db_upsert_user(user_id, step=step_done, step_date=now_str)

        await query.edit_message_text(
            f"✅ Шаг {step_done} отмечен выполненным! Следующий материал придет согласно графику.")

        # Планируем следующий
        schedule_next_job(user_id, context.application)


async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tz = int(update.message.text)
        context.user_data['tz'] = tz
        await update.message.reply_text("Отлично. Теперь введите час для уведомлений (0-23):")
        return 2  # Состояние WAIT_TIME
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число (например: 3).")
        return 1


async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hour = int(update.message.text)
        if not (0 <= hour <= 23): raise ValueError

        user_id = update.effective_user.id
        tz = context.user_data['tz']
        start_date = datetime.now().strftime("%Y-%m-%d")

        # Сохраняем настройки и стартуем
        db_upsert_user(user_id, timezone=str(tz), notification_time=hour, start_date=start_date, step=0)

        await update.message.reply_text(f"Настройки сохранены! Курс начат {start_date}. Первое задание придет сейчас.")

        # Запускаем процесс (первое задание сразу)
        schedule_next_job(user_id, context.application, force_now=True)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("Введите число от 0 до 23.")
        return 2


async def stop_course(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Стираем дату старта, но оставляем шаг (или можно обнулять, зависит от ТЗ "отказаться")
    # В ТЗ: "удаляется стартовая дата. Когда начинает заново, то обнуляется".
    db_upsert_user(user_id, start_date=None)

    # Удаляем задачи
    current_jobs = context.application.job_queue.get_jobs_by_name(str(user_id))
    for job in current_jobs:
        job.schedule_removal()

    await update.message.reply_text("Курс остановлен. Уведомления отключены. Напишите /start, чтобы начать заново.")


async def restore_jobs(application):
    """Восстанавливает задачи при перезапуске бота"""
    conn = get_db_connection()
    users = conn.execute("SELECT * FROM users WHERE start_date IS NOT NULL").fetchall()
    conn.close()

    count = 0
    for user in users:
        schedule_next_job(user['id'], application)
        count += 1
    logging.info(f"Restored jobs for {count} users.")


# --- ЗАПУСК ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    # Стейт машина для настройки
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_handler, pattern="^setup_start$")],
        states={
            1: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_timezone)],
            2: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_time)],
        },
        fallbacks=[CommandHandler("start", start)]
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop_course))
    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^done_"))

    # Восстановление задач при старте
    app.job_queue.run_once(lambda ctx: restore_jobs(app), 1)

    app.run_polling()