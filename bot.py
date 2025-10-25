import requests
import asyncio
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
CHAT_TOKEN = os.getenv("CHAT_TOKEN")

COOKIES = {
    "ACCESS_TOKEN": ACCESS_TOKEN,
    "CHAT_TOKEN": CHAT_TOKEN,
}

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36"
}

API_URL = "https://seller.ggsel.net/api/v1/conversations"
ORDERS_API_URL = "https://seller.ggsel.net/api/v1/orders"

# Текст кнопки в постоянном нижнем меню
BUTTON_TEXT = "Проверить сообщения"
BUTTON_TEXT_ORDERS = "🧾 Проверить заказы"

# === Логика проверки ===
def get_unread():
    """Возвращает список непрочитанных чатов"""
    params = {
        "show_only_unread": "true",
        "page": 1,
        "limit": 20,
        "sort[last_messages_at]": "desc"
    }
    r = requests.get(API_URL, params=params, headers=HEADERS, cookies=COOKIES)
    r.raise_for_status()
    return r.json().get("data", [])

def get_recent_orders():
    params = {
        "page": 1,
        "limit": 10,
        "sort[created_at]": "desc",
    }
    r = requests.get(ORDERS_API_URL, params=params, headers=HEADERS, cookies=COOKIES)
    r.raise_for_status()
    data = r.json()
    orders = []
    if isinstance(data, dict) and "data" in data:
        orders = data["data"]
    elif isinstance(data, list):
        orders = data
    else:
        return []

    # Показываем только заказы со статусом "paid"
    filtered_orders = []
    for order in orders:
        status = order.get("status")
        if isinstance(status, str) and status.lower() == "paid":
            filtered_orders.append(order)
    return filtered_orders

def format_alert(chat):
    """Форматирует сообщение для отправки"""
    username = chat["user"]["username"]
    order_id = chat["order_id"]
    offer_title = chat["offer"]["title"] if chat["offer"] else "—"
    msg = chat["last_message"]

    if msg["is_current_user"] is False and msg["read"] is False:
        return (
            f"💬 Новое сообщение от <b>{username}</b>\n"
            f"📦 Заказ #{order_id} — <i>{offer_title}</i>\n"
            f"🕒 {msg['created_at']}\n"
            f"💭 <code>{msg['text']}</code>"
        )
    return None

def format_order_alert(order):
    title = order.get("offer_title", "—")
    email = order.get("buyer_email", "—")
    amount = order.get("amount", 0)
    status = order.get("status", "—")
    created_at = order.get("created_at", "—")
    number = order.get("number") or order.get("id", "—")
    return (
        f"🧾 Новый заказ №<b>{number}</b>\n"
        f"📦 Товар: <i>{title}</i>\n"
        f"📧 Покупатель: <code>{email}</code>\n"
        f"💰 Сумма: <b>{amount}</b>\n"
        f"📌 Статус: <b>{status}</b>\n"
        f"🕒 {created_at}"
    )

# === Телеграм команды и логика ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Включаем постоянное нижнее меню
    keyboard = [[KeyboardButton(BUTTON_TEXT), KeyboardButton(BUTTON_TEXT_ORDERS)]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Привет! 👋\nКнопка внизу: проверяй новые сообщения на GGSel когда удобно.",
        reply_markup=reply_markup
    )

    # Инициализируем хранилище просмотренных сообщений для авто-оповещений
    chat_id = update.effective_chat.id
    seen_map = context.application.bot_data.setdefault("seen_keys", {})
    seen_map.setdefault(chat_id, set())
    orders_seen_map = context.application.bot_data.setdefault("seen_orders", {})
    orders_seen_map.setdefault(chat_id, set())

    # Настраиваем авто-проверку каждую минуту
    job_queue = getattr(context, "job_queue", None)
    if job_queue is not None:
        job_name = f"auto_check_{chat_id}"
        for job in job_queue.get_jobs_by_name(job_name):
            job.schedule_removal()
        job_queue.run_repeating(
            auto_check,
            interval=60,
            first=5,
            chat_id=chat_id,
            name=job_name,
        )
        # Планируем проверку заказов каждые x минут
        orders_job_name = f"auto_orders_{chat_id}"
        for job in job_queue.get_jobs_by_name(orders_job_name):
            job.schedule_removal()
        job_queue.run_repeating(
            auto_orders_check,
            interval=60,
            first=10,
            chat_id=chat_id,
            name=orders_job_name,
        )
    else:
        # Фолбэк без JobQueue: запускаем фоновую задачу
        task_map = context.application.bot_data.setdefault("bg_tasks", {})
        t1 = task_map.get((chat_id, "msgs"))
        if t1 is None or t1.done():
            t1 = asyncio.create_task(_auto_check_loop(context.application, chat_id, 60))
            task_map[(chat_id, "msgs")] = t1
        t2 = task_map.get((chat_id, "orders"))
        if t2 is None or t2.done():
            t2 = asyncio.create_task(_auto_orders_loop(context.application, chat_id, 300))
            task_map[(chat_id, "orders")] = t2

async def manual_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю новые сообщения...")
    try:
        unread = get_unread()
        messages = []
        for chat in unread:
            alert = format_alert(chat)
            if alert:
                messages.append(alert)
        if messages:
            await update.message.reply_text("\n\n".join(messages), parse_mode="HTML")
        else:
            await update.message.reply_text("✅ Новых сообщений нет.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")

async def manual_check_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Проверяю новые заказы...")
    chat_id = update.effective_chat.id
    try:
        orders = get_recent_orders()
        if not orders:
            await update.message.reply_text("✅ Новых заказов нет.")
            return
        orders_seen_map = context.application.bot_data.setdefault("seen_orders", {})
        seen_set = orders_seen_map.setdefault(chat_id, set())

        alerts = []
        for order in orders:
            oid = order.get("id") or order.get("number")
            if oid in seen_set:
                continue
            seen_set.add(oid)
            alerts.append(format_order_alert(order))

        if alerts:
            await update.message.reply_text("\n\n".join(alerts), parse_mode="HTML")
        else:
            await update.message.reply_text("✅ Новых заказов нет.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}")

async def _auto_check_once(app: Application, chat_id: int):
    try:
        unread = get_unread()
        alerts = []
        seen_map = app.bot_data.setdefault("seen_keys", {})
        seen_set = seen_map.setdefault(chat_id, set())

        for chat in unread:
            alert = format_alert(chat)
            if not alert:
                continue
            msg = chat.get("last_message", {})
            created_at = msg.get("created_at")
            order_id = chat.get("order_id")
            key = f"{order_id}:{created_at}"
            if key in seen_set:
                continue
            seen_set.add(key)
            alerts.append(alert)

        if alerts:
            await app.bot.send_message(chat_id=chat_id, text="\n\n".join(alerts), parse_mode="HTML")
    except Exception as e:
        print(f"Auto-check error for {chat_id}: {e}")

async def auto_check(context: ContextTypes.DEFAULT_TYPE):
    # Callback для JobQueue
    chat_id = context.job.chat_id
    await _auto_check_once(context.application, chat_id)

async def _auto_check_loop(app: Application, chat_id: int, interval_seconds: int):
    # Фолбэк-цикл, если JobQueue недоступен
    await asyncio.sleep(5)
    while True:
        await _auto_check_once(app, chat_id)
        await asyncio.sleep(interval_seconds)

async def _auto_orders_once(app: Application, chat_id: int):
    try:
        orders = get_recent_orders()
        if not orders:
            return
        orders_seen_map = app.bot_data.setdefault("seen_orders", {})
        seen_set = orders_seen_map.setdefault(chat_id, set())
        alerts = []
        for order in orders:
            oid = order.get("id") or order.get("number")
            if oid in seen_set:
                continue
            seen_set.add(oid)
            alerts.append(format_order_alert(order))
        if alerts:
            await app.bot.send_message(chat_id=chat_id, text="\n\n".join(alerts), parse_mode="HTML")
    except Exception as e:
        print(f"Auto-orders error for {chat_id}: {e}")

async def auto_orders_check(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    await _auto_orders_once(context.application, chat_id)

async def _auto_orders_loop(app: Application, chat_id: int, interval_seconds: int):
    await asyncio.sleep(5)
    while True:
        await _auto_orders_once(app, chat_id)
        await asyncio.sleep(interval_seconds)

# === Запуск бота ===
def main():
    print("🚀 Бот запускается...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^Проверить сообщения$"), manual_check))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^🧾 Проверить заказы$"), manual_check_orders))
    print("✅ Бот запущен. Открой в Telegram и отправь /start")
    app.run_polling()

if __name__ == "__main__":
    main()
