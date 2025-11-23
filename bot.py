import os
import sqlite3
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher.filters import Text
from dotenv import load_dotenv

# ==========================
#   НАСТРОЙКА ЛОГГЕРА
# ==========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==========================
#   ЗАГРУЗКА .env
# ==========================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
PAYMENT_INFO = os.getenv(
    "PAYMENT_INFO",
    "Оплатите на реквизиты гаранта и после оплаты нажмите кнопку «Я оплатил»."
)

if not BOT_TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN в .env")

# ==========================
#   TELEGRAM BOT
# ==========================
bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# ==========================
#   БАЗА ДАННЫХ
# ==========================
DB_PATH = "guarantor.db"
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()


def init_db():
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE,
        username TEXT,
        first_name TEXT,
        is_admin INTEGER DEFAULT 0
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        buyer_id INTEGER,
        seller_id INTEGER,
        amount REAL,
        description TEXT,
        status TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        deal_id INTEGER,
        action TEXT,
        created_at TEXT
    )
    """)
    conn.commit()


init_db()

# ==========================
#   КОНСТАНТЫ СТАТУСОВ
# ==========================
STATUS_AWAIT_SELLER_CONFIRM = "await_seller_confirm"       # ждём подтверждения продавца
STATUS_AWAIT_PAYMENT = "await_payment"                     # ждём оплаты от покупателя
STATUS_PAID_WAIT_DELIVERY = "paid_waiting_delivery"        # оплата прошла, ждём отправки товара
STATUS_WAIT_BUYER_CONFIRM = "waiting_buyer_confirm"        # товар отправлен, ждём подтверждения покупателя
STATUS_COMPLETED = "completed_success"                     # сделка успешно завершена
STATUS_DISPUTE = "dispute"                                 # спор
STATUS_RESOLVED_BUYER = "resolved_buyer"                   # спор решён в пользу покупателя
STATUS_RESOLVED_SELLER = "resolved_seller"                 # спор решён в пользу продавца
STATUS_RESOLVED_PARTIAL = "resolved_partial"               # частичный возврат
STATUS_REJECTED_BY_SELLER = "rejected_by_seller"           # продавец не согласился
STATUS_CANCELLED = "cancelled"                             # отменена

STATUS_NAMES = {
    STATUS_AWAIT_SELLER_CONFIRM: "Ожидает подтверждения продавца",
    STATUS_AWAIT_PAYMENT: "Ожидает оплаты",
    STATUS_PAID_WAIT_DELIVERY: "Оплата получена, ожидается отправка товара",
    STATUS_WAIT_BUYER_CONFIRM: "Ожидается подтверждение покупателя",
    STATUS_COMPLETED: "Завершена (успешно)",
    STATUS_DISPUTE: "СПОР",
    STATUS_RESOLVED_BUYER: "Спор решён в пользу покупателя",
    STATUS_RESOLVED_SELLER: "Спор решён в пользу продавца",
    STATUS_RESOLVED_PARTIAL: "Спор решён частично (частичный возврат)",
    STATUS_REJECTED_BY_SELLER: "Отклонена продавцом",
    STATUS_CANCELLED: "Отменена",
}

# ==========================
#   ПРОСТОЙ STATE-МАШИНГ
# ==========================
# Храним временные состояния пользователей в памяти (для создания новой сделки)
user_states = {}  # {tg_id: "state_name"}
user_temp = {}    # {tg_id: {"seller_id": ..., "amount": ..., "description": ...}}

STATE_NEW_DEAL_SELLER = "new_deal_seller"
STATE_NEW_DEAL_AMOUNT = "new_deal_amount"
STATE_NEW_DEAL_DESCRIPTION = "new_deal_description"
STATE_NEW_DEAL_CONFIRM = "new_deal_confirm"


def set_state(user_id: int, state: str | None):
    if state is None:
        user_states.pop(user_id, None)
        user_temp.pop(user_id, None)
    else:
        user_states[user_id] = state
        if user_id not in user_temp:
            user_temp[user_id] = {}


def get_state(user_id: int):
    return user_states.get(user_id)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def upsert_user(user: types.User):
    cursor.execute(
        """
        INSERT INTO users (tg_id, username, first_name, is_admin)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
        """,
        (user.id, user.username or "", user.first_name or "", 1 if is_admin(user.id) else 0)
    )
    conn.commit()


def log_action(deal_id: int, action: str):
    cursor.execute(
        "INSERT INTO logs (deal_id, action, created_at) VALUES (?, ?, ?)",
        (deal_id, action, datetime.utcnow().isoformat())
    )
    conn.commit()


# ==========================
#   КЛАВИАТУРЫ
# ==========================
def main_menu_kb():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🆕 Создать сделку", callback_data="menu_new_deal"))
    kb.add(InlineKeyboardButton("📜 Мои сделки", callback_data="menu_my_deals"))
    return kb


def confirm_new_deal_kb():
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data="new_deal_confirm_yes"),
        InlineKeyboardButton("❌ Отменить", callback_data="new_deal_confirm_no"),
    )
    return kb


def buyer_payment_kb(deal_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💸 Я оплатил", callback_data=f"deal_paid_{deal_id}"))
    kb.add(InlineKeyboardButton("❌ Отменить сделку", callback_data=f"deal_cancel_{deal_id}"))
    return kb


def seller_confirm_kb(deal_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Согласен", callback_data=f"seller_accept_{deal_id}"),
        InlineKeyboardButton("❌ Не согласен", callback_data=f"seller_reject_{deal_id}"),
    )
    return kb


def seller_delivery_kb(deal_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📦 Товар отправлен", callback_data=f"deal_sent_{deal_id}"))
    return kb


def buyer_confirm_kb(deal_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Всё ок", callback_data=f"buyer_ok_{deal_id}"),
        InlineKeyboardButton("⚠️ Есть проблема", callback_data=f"buyer_dispute_{deal_id}")
    )
    return kb


def admin_dispute_kb(deal_id: int):
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("👤 В пользу покупателя", callback_data=f"adm_buyer_{deal_id}"),
        InlineKeyboardButton("🧑‍💻 В пользу продавца", callback_data=f"adm_seller_{deal_id}"),
    )
    kb.add(InlineKeyboardButton("⚖️ Частичный возврат", callback_data=f"adm_partial_{deal_id}"))
    return kb


# ==========================
#   ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================
def get_user_display(tg_id: int) -> str:
    cursor.execute("SELECT username, first_name FROM users WHERE tg_id=?", (tg_id,))
    row = cursor.fetchone()
    if not row:
        return f"<code>{tg_id}</code>"
    username, first_name = row
    if username:
        return f"@{username}"
    return first_name or str(tg_id)


def get_deal(deal_id: int):
    cursor.execute("SELECT id, buyer_id, seller_id, amount, description, status, created_at, updated_at FROM deals WHERE id=?", (deal_id,))
    return cursor.fetchone()


def format_deal_text(row) -> str:
    if not row:
        return "Сделка не найдена."
    deal_id, buyer_id, seller_id, amount, description, status, created_at, updated_at = row
    status_name = STATUS_NAMES.get(status, status)
    return (
        f"🧾 <b>Сделка #{deal_id}</b>\n"
        f"👤 Покупатель: {get_user_display(buyer_id)}\n"
        f"💼 Продавец: {get_user_display(seller_id)}\n"
        f"💰 Сумма: <b>{amount:.2f}</b>\n"
        f"📦 Описание: {description}\n\n"
        f"📌 Статус: <b>{status_name}</b>\n"
        f"🕒 Создана: {created_at}\n"
        f"🔄 Обновлена: {updated_at}"
    )


def get_latest_paid_waiting_deal_for_seller(seller_id: int):
    """Последняя сделка продавца в статусе 'оплачено, ждём отправки'."""
    cursor.execute(
        """
        SELECT id, buyer_id, seller_id, amount, description, status, created_at, updated_at
        FROM deals
        WHERE seller_id=? AND status=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (seller_id, STATUS_PAID_WAIT_DELIVERY),
    )
    return cursor.fetchone()


# ==========================
#   ХЕНДЛЕРЫ
# ==========================
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    upsert_user(message.from_user)
    text = (
        "Привет! Я бот-гарант 🤝\n\n"
        "Я помогаю проводить безопасные сделки между покупателями и продавцами.\n"
        "Средства переводятся гаранту, а после успешного выполнения сделки — "
        "переводятся нужной стороне вручную.\n\n"
        "Выберите действие:"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@dp.message_handler(commands=["help"])
async def cmd_help(message: types.Message):
    text = (
        "ℹ️ <b>Справка</b>\n\n"
        "1. Покупатель создаёт сделку через бота.\n"
        "2. Продавец подтверждает условия.\n"
        "3. Покупатель оплачивает на реквизиты гаранта.\n"
        "4. Продавец отправляет товар/услугу.\n"
        "5. Покупатель подтверждает получение или открывает спор.\n"
        "6. В случае спора решение принимает админ-гарант.\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/help — справка\n"
        "/mydeals — список ваших сделок\n"
        "/deal ID_СДЕЛКИ — подробно о сделке"
    )
    await message.answer(text)


@dp.message_handler(commands=["mydeals"])
async def cmd_mydeals(message: types.Message):
    user_id = message.from_user.id
    cursor.execute(
        "SELECT id, amount, status, created_at FROM deals WHERE buyer_id=? OR seller_id=? ORDER BY id DESC LIMIT 20",
        (user_id, user_id),
    )
    rows = cursor.fetchall()
    if not rows:
        await message.answer("У вас пока нет сделок.")
        return
    lines = ["📜 <b>Ваши последние сделки:</b>"]
    for deal_id, amount, status, created_at in rows:
        status_name = STATUS_NAMES.get(status, status)
        lines.append(f"• #{deal_id} — {amount:.2f} — {status_name} — {created_at}")
    lines.append("\nПодробнее: /deal ID_СДЕЛКИ")
    await message.answer("\n".join(lines))


@dp.message_handler(commands=["deal"])
async def cmd_deal(message: types.Message):
    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/deal 123</code>")
        return
    deal_id = int(parts[1])
    row = get_deal(deal_id)
    await message.answer(format_deal_text(row))


# ==========================
#   АДМИН-КОМАНДА /admin
# ==========================
@dp.message_handler(commands=["admin"])
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text = (
        "👮‍♂️ <b>Админ-панель</b>\n\n"
        "Доступные функции:\n"
        "• Просмотр спорных сделок\n"
        "• Завершение споров\n\n"
        "Команды:\n"
        "/disputes — все сделки в статусе СПОР"
    )
    await message.answer(text)


@dp.message_handler(commands=["disputes"])
async def cmd_disputes(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    cursor.execute(
        "SELECT id, buyer_id, seller_id, amount, status, created_at FROM deals WHERE status=? ORDER BY id DESC",
        (STATUS_DISPUTE,),
    )
    rows = cursor.fetchall()
    if not rows:
        await message.answer("Спорных сделок нет.")
        return
    lines = ["⚠️ <b>Спорные сделки:</b>"]
    for deal_id, buyer_id, seller_id, amount, status, created_at in rows:
        lines.append(
            f"• #{deal_id} — {amount:.2f} — {STATUS_NAMES.get(status, status)} "
            f"({get_user_display(buyer_id)} vs {get_user_display(seller_id)}) — {created_at}"
        )
    await message.answer("\n".join(lines))


# ==========================
#   INLINE-МЕНЮ ГЛАВНОЕ
# ==========================
@dp.callback_query_handler(Text(equals="menu_new_deal"))
async def cb_menu_new_deal(call: types.CallbackQuery):
    user_id = call.from_user.id
    set_state(user_id, STATE_NEW_DEAL_SELLER)
    await call.message.edit_text(
        "🆕 Создание сделки.\n\n"
        "Шаг 1/3.\n"
        "Отправь <b>сообщение, пересланное от продавца</b> "
        "или введи его <b>Telegram ID</b> (цифрами).\n\n"
        "Так бот сможет связаться с продавцом.",
        reply_markup=None,
    )
    await call.answer()


@dp.callback_query_handler(Text(equals="menu_my_deals"))
async def cb_menu_my_deals(call: types.CallbackQuery):
    """Показываем список сделок по кнопке 'Мои сделки'."""
    user_id = call.from_user.id
    cursor.execute(
        "SELECT id, amount, status, created_at FROM deals WHERE buyer_id=? OR seller_id=? ORDER BY id DESC LIMIT 20",
        (user_id, user_id),
    )
    rows = cursor.fetchall()
    if not rows:
        await call.message.edit_text("У вас пока нет сделок.", reply_markup=main_menu_kb())
        await call.answer()
        return

    lines = ["📜 <b>Ваши последние сделки:</b>"]
    for deal_id, amount, status, created_at in rows:
        status_name = STATUS_NAMES.get(status, status)
        lines.append(f"• #{deal_id} — {amount:.2f} — {status_name} — {created_at}")
    lines.append("\nПодробнее: /deal ID_СДЕЛКИ")

    await call.message.edit_text("\n".join(lines), reply_markup=main_menu_kb())
    await call.answer()


# ==========================
#   СОЗДАНИЕ СДЕЛКИ — ШАГИ
# ==========================
@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_NEW_DEAL_SELLER, content_types=types.ContentTypes.ANY)
async def new_deal_step_seller(message: types.Message):
    user_id = message.from_user.id
    seller_id = None

    # 1) Если пересланное сообщение
    if message.forward_from:
        seller_id = message.forward_from.id
        upsert_user(message.forward_from)

    # 2) Если прислали цифры (ID)
    elif message.text and message.text.strip().isdigit():
        seller_id = int(message.text.strip())

    if not seller_id:
        await message.answer(
            "Не смог понять продавца 🤔\n"
            "Пожалуйста, перешли любое его сообщение сюда или отправь его Telegram ID цифрами."
        )
        return

    user_temp[user_id]["seller_id"] = seller_id
    set_state(user_id, STATE_NEW_DEAL_AMOUNT)
    await message.answer(
        "Шаг 2/3.\n"
        "Введи <b>сумму сделки</b> (число, можно с точкой, например 1500 или 199.99)."
    )


@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_NEW_DEAL_AMOUNT)
async def new_deal_step_amount(message: types.Message):
    user_id = message.from_user.id
    text = message.text.replace(",", ".") if message.text else ""
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer("Сумма указана некорректно. Введи положительное число, например <code>1500</code> или <code>199.99</code>.")
        return

    user_temp[user_id]["amount"] = amount
    set_state(user_id, STATE_NEW_DEAL_DESCRIPTION)
    await message.answer(
        "Шаг 3/3.\n"
        "Опиши товар/услугу: что именно продаётся, важные условия, сроки и т.п."
    )


@dp.message_handler(lambda m: get_state(m.from_user.id) == STATE_NEW_DEAL_DESCRIPTION)
async def new_deal_step_description(message: types.Message):
    user_id = message.from_user.id
    description = message.text.strip() if message.text else ""
    if not description:
        await message.answer("Пожалуйста, опиши товар/услугу текстом.")
        return

    user_temp[user_id]["description"] = description
    set_state(user_id, STATE_NEW_DEAL_CONFIRM)

    seller_id = user_temp[user_id]["seller_id"]
    amount = user_temp[user_id]["amount"]

    text = (
        "Проверь данные сделки:\n\n"
        f"👤 Ты (покупатель): {get_user_display(user_id)}\n"
        f"💼 Продавец: <code>{seller_id}</code>\n"
        f"💰 Сумма: <b>{amount:.2f}</b>\n"
        f"📦 Описание: {description}\n\n"
        "Если всё верно — подтверди создание сделки."
    )
    await message.answer(text, reply_markup=confirm_new_deal_kb())


@dp.callback_query_handler(Text(startswith="new_deal_confirm_"))
async def cb_new_deal_confirm(call: types.CallbackQuery):
    user_id = call.from_user.id
    state = get_state(user_id)

    if state != STATE_NEW_DEAL_CONFIRM:
        await call.answer("Нет активного процесса создания сделки.", show_alert=True)
        return

    if call.data == "new_deal_confirm_no":
        set_state(user_id, None)
        await call.message.edit_text("Создание сделки отменено.", reply_markup=main_menu_kb())
        await call.answer()
        return

    # Подтвердили
    temp = user_temp.get(user_id, {})
    seller_id = temp.get("seller_id")
    amount = temp.get("amount")
    description = temp.get("description")

    if not (seller_id and amount and description):
        set_state(user_id, None)
        await call.message.edit_text("Ошибка: не хватает данных сделки. Попробуйте создать заново.", reply_markup=main_menu_kb())
        await call.answer()
        return

    now = datetime.utcnow().isoformat()
    cursor.execute(
        """
        INSERT INTO deals (buyer_id, seller_id, amount, description, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, seller_id, amount, description, STATUS_AWAIT_SELLER_CONFIRM, now, now)
    )
    conn.commit()
    deal_id = cursor.lastrowid
    log_action(deal_id, f"Создана сделка (покупатель {user_id}, продавец {seller_id}, сумма {amount})")

    set_state(user_id, None)

    # Уведомляем покупателя
    await call.message.edit_text(
        f"✅ Сделка #{deal_id} создана!\n\n"
        "Сейчас продавцу будет отправлен запрос на подтверждение.\n"
        "Статус: <b>Ожидает подтверждения продавца</b>",
        reply_markup=main_menu_kb()
    )

    # Уведомляем продавца (если бот может ему написать)
    try:
        seller_text = (
            f"🤝 Вас пригласили в сделку через гаранта.\n\n"
            f"{format_deal_text(get_deal(deal_id))}\n\n"
            "Если вы согласны с условиями — подтвердите участие:"
        )
        await bot.send_message(seller_id, seller_text, reply_markup=seller_confirm_kb(deal_id))
    except Exception as e:
        logger.warning(f"Не удалось отправить сообщение продавцу {seller_id}: {e}")

    await call.answer()


# ==========================
#   ПРОДАВЕЦ ПОДТВЕРЖДАЕТ
# ==========================
@dp.callback_query_handler(Text(startswith="seller_accept_"))
async def cb_seller_accept(call: types.CallbackQuery):
    user_id = call.from_user.id
    parts = call.data.split("_")
    deal_id = int(parts[-1])

    row = get_deal(deal_id)
    if not row:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    _, buyer_id, seller_id, amount, description, status, _, _ = row

    if user_id != seller_id:
        await call.answer("Вы не являетесь продавцом в этой сделке.", show_alert=True)
        return
    if status != STATUS_AWAIT_SELLER_CONFIRM:
        await call.answer("Эта сделка уже обработана.", show_alert=True)
        return

    now = datetime.utcnow().isoformat()
    cursor.execute(
        "UPDATE deals SET status=?, updated_at=? WHERE id=?",
        (STATUS_AWAIT_PAYMENT, now, deal_id)
    )
    conn.commit()
    log_action(deal_id, f"Продавец {user_id} подтвердил сделку")

    await call.message.edit_text(
        f"Вы подтвердили участие в сделке #{deal_id}.\n"
        f"Ожидаем оплату от покупателя."
    )

    # Уведомляем покупателя
    text_buyer = (
        f"✅ Продавец подтвердил участие в сделке #{deal_id}!\n\n"
        f"{format_deal_text(get_deal(deal_id))}\n\n"
        "Теперь вам нужно произвести оплату на реквизиты гаранта:\n\n"
        f"{PAYMENT_INFO}"
    )
    try:
        await bot.send_message(buyer_id, text_buyer, reply_markup=buyer_payment_kb(deal_id))
    except Exception as e:
        logger.warning(f"Не удалось уведомить покупателя {buyer_id} об оплате: {e}")

    await call.answer("Сделка подтверждена.")


@dp.callback_query_handler(Text(startswith="seller_reject_"))
async def cb_seller_reject(call: types.CallbackQuery):
    user_id = call.from_user.id
    parts = call.data.split("_")
    deal_id = int(parts[-1])

    row = get_deal(deal_id)
    if not row:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    _, buyer_id, seller_id, amount, description, status, _, _ = row

    if user_id != seller_id:
        await call.answer("Вы не являетесь продавцом в этой сделке.", show_alert=True)
        return
    if status != STATUS_AWAIT_SELLER_CONFIRM:
        await call.answer("Эта сделка уже обработана.", show_alert=True)
        return

    now = datetime.utcnow().isoformat()
    cursor.execute(
        "UPDATE deals SET status=?, updated_at=? WHERE id=?",
        (STATUS_REJECTED_BY_SELLER, now, deal_id)
    )
    conn.commit()
    log_action(deal_id, f"Продавец {user_id} отклонил сделку")

    await call.message.edit_text(f"Вы отклонили сделку #{deal_id}.", reply_markup=main_menu_kb())
    try:
        await bot.send_message(
            buyer_id,
            f"❌ Продавец отклонил сделку #{deal_id}.\n"
            "Сделка закрыта.",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить покупателя {buyer_id} об отказе продавца: {e}")

    await call.answer("Сделка отклонена.")


# ==========================
#   ПОКУПАТЕЛЬ: ОПЛАТИЛ / ОТМЕНА
# ==========================
@dp.callback_query_handler(Text(startswith="deal_paid_"))
async def cb_deal_paid(call: types.CallbackQuery):
    user_id = call.from_user.id
    deal_id = int(call.data.split("_")[-1])

    row = get_deal(deal_id)
    if not row:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    _, buyer_id, seller_id, amount, description, status, _, _ = row

    if user_id != buyer_id:
        await call.answer("Вы не являетесь покупателем в этой сделке.", show_alert=True)
        return
    if status != STATUS_AWAIT_PAYMENT:
        await call.answer("Сделка не находится в статусе ожидания оплаты.", show_alert=True)
        return

    now = datetime.utcnow().isoformat()
    cursor.execute(
        "UPDATE deals SET status=?, updated_at=? WHERE id=?",
        (STATUS_PAID_WAIT_DELIVERY, now, deal_id)
    )
    conn.commit()
    log_action(deal_id, f"Покупатель {user_id} сообщил об оплате")

    await call.message.edit_text(
        f"Вы отметили, что оплатили сделку #{deal_id}.\n"
        "Ожидаем отправку товара от продавца."
    )

    # Уведомляем продавца
    text_seller = (
        f"💸 Покупатель оплатил сделку #{deal_id}.\n\n"
        f"{format_deal_text(get_deal(deal_id))}\n\n"
        "Отправьте товар покупателю (логин/пароль, файл и т.п.) туда, где вы общаетесь.\n"
        "Затем либо перешлите это сообщение в этот чат, либо нажмите кнопку ниже:"
    )
    try:
        await bot.send_message(seller_id, text_seller, reply_markup=seller_delivery_kb(deal_id))
    except Exception as e:
        logger.warning(f"Не удалось уведомить продавца {seller_id} об оплате: {e}")

    await call.answer("Оплата отмечена.")


@dp.callback_query_handler(Text(startswith="deal_cancel_"))
async def cb_deal_cancel(call: types.CallbackQuery):
    user_id = call.from_user.id
    deal_id = int(call.data.split("_")[-1])

    row = get_deal(deal_id)
    if not row:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    _, buyer_id, seller_id, amount, description, status, _, _ = row

    if user_id != buyer_id:
        await call.answer("Только покупатель может отменить сделку на этом этапе.", show_alert=True)
        return
    if status != STATUS_AWAIT_PAYMENT:
        await call.answer("Сделка не в статусе ожидания оплаты.", show_alert=True)
        return

    now = datetime.utcnow().isoformat()
    cursor.execute(
        "UPDATE deals SET status=?, updated_at=? WHERE id=?",
        (STATUS_CANCELLED, now, deal_id)
    )
    conn.commit()
    log_action(deal_id, f"Покупатель {user_id} отменил сделку до оплаты")

    await call.message.edit_text(f"Сделка #{deal_id} отменена до платы.", reply_markup=main_menu_kb())
    try:
        await bot.send_message(
            seller_id,
            f"❌ Покупатель отменил сделку #{deal_id} до оплаты.",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить продавца {seller_id} об отмене: {e}")

    await call.answer("Сделка отменена.")


# ==========================
#   ПРОДАВЕЦ: ТОВАР ОТПРАВЛЕН (КНОПКА)
# ==========================
@dp.callback_query_handler(Text(startswith="deal_sent_"))
async def cb_deal_sent(call: types.CallbackQuery):
    user_id = call.from_user.id
    deal_id = int(call.data.split("_")[-1])

    row = get_deal(deal_id)
    if not row:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    _, buyer_id, seller_id, amount, description, status, _, _ = row

    if user_id != seller_id:
        await call.answer("Вы не являетесь продавцом в этой сделке.", show_alert=True)
        return
    if status != STATUS_PAID_WAIT_DELIVERY:
        await call.answer("Сделка не в статусе ожидания отправки товара.", show_alert=True)
        return

    now = datetime.utcnow().isoformat()
    cursor.execute(
        "UPDATE deals SET status=?, updated_at=? WHERE id=?",
        (STATUS_WAIT_BUYER_CONFIRM, now, deal_id)
    )
    conn.commit()
    log_action(deal_id, f"Продавец {user_id} отметил отправку товара кнопкой")

    await call.message.edit_text(
        f"Вы отметили, что отправили товар по сделке #{deal_id}.\n"
        "Ожидаем подтверждение от покупателя.",
        reply_markup=main_menu_kb()
    )

    # Уведомляем покупателя
    text_buyer = (
        f"📦 Продавец сообщил, что отправил товар по сделке #{deal_id}.\n\n"
        f"{format_deal_text(get_deal(deal_id))}\n\n"
        "Подтвердите результат сделки:"
    )
    try:
        await bot.send_message(buyer_id, text_buyer, reply_markup=buyer_confirm_kb(deal_id))
    except Exception as e:
        logger.warning(f"Не удалось уведомить покупателя {buyer_id} об отправке товара: {e}")

    await call.answer("Отправка товара отмечена.")


# ==========================
#   ПРОДАВЕЦ: ОТПРАВКА ТОВАРА СООБЩЕНИЕМ
# ==========================
@dp.message_handler(content_types=types.ContentTypes.ANY)
async def fallback_or_delivery_handler(message: types.Message):
    """
    Если у продавца есть активная сделка в статусе 'оплачено, ждём отправки'
    и он отправляет сюда сообщение (логин/пароль, файл, скрин и т.п.),
    бот воспринимает это как отправку товара — пересылает покупателю и
    запускает этап подтверждения.
    """
    user_id = message.from_user.id
    state = get_state(user_id)

    # Сначала проверяем, нет ли стадии создания сделки
    if state is not None and state != STATE_NEW_DEAL_SELLER \
       and state != STATE_NEW_DEAL_AMOUNT and state != STATE_NEW_DEAL_DESCRIPTION \
       and state != STATE_NEW_DEAL_CONFIRM:
        # На всякий случай, но сейчас стейты используются только в создании сделки
        await message.answer("Не понял сообщение. Если хочешь отменить создание сделки, напиши /cancel.")
        return

    # Проверяем, является ли пользователь продавцом в оплаченной, но не отправленной сделке
    row = get_latest_paid_waiting_deal_for_seller(user_id)
    if row:
        deal_id, buyer_id, seller_id, amount, description, status, created_at, updated_at = row

        # Обновляем статус
        now = datetime.utcnow().isoformat()
        cursor.execute(
            "UPDATE deals SET status=?, updated_at=? WHERE id=?",
            (STATUS_WAIT_BUYER_CONFIRM, now, deal_id)
        )
        conn.commit()
        log_action(deal_id, f"Продавец {user_id} отправил сообщение с товаром")

        # Сообщаем продавцу
        await message.answer(
            f"Ваше сообщение отправлено покупателю по сделке #{deal_id}.\n"
            "Теперь ждём подтверждения от покупателя.",
            reply_markup=main_menu_kb()
        )

        # Пересылаем сообщение покупателю
        try:
            await bot.copy_message(
                chat_id=buyer_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            await bot.send_message(
                buyer_id,
                f"📦 Продавец отправил сообщение по сделке #{deal_id}.\n\n"
                f"{format_deal_text(get_deal(deal_id))}\n\n"
                "Подтвердите результат сделки:",
                reply_markup=buyer_confirm_kb(deal_id)
            )
        except Exception as e:
            logger.warning(f"Не удалось переслать сообщение покупателю {buyer_id}: {e}")

        return

    # Если особой логики нет — обычный фолбэк
    if get_state(user_id):
        await message.answer("Не понял сообщение. Если хочешь отменить создание сделки, напиши /cancel.")
    else:
        await message.answer("Используй /start для открытия меню или /help для справки.")


# ==========================
#   ПОКУПАТЕЛЬ: ВСЁ ОК / ПРОБЛЕМА
# ==========================
@dp.callback_query_handler(Text(startswith="buyer_ok_"))
async def cb_buyer_ok(call: types.CallbackQuery):
    user_id = call.from_user.id
    deal_id = int(call.data.split("_")[-1])

    row = get_deal(deal_id)
    if not row:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    _, buyer_id, seller_id, amount, description, status, _, _ = row

    if user_id != buyer_id:
        await call.answer("Вы не являетесь покупателем в этой сделке.", show_alert=True)
        return
    if status != STATUS_WAIT_BUYER_CONFIRM:
        await call.answer("Сделка не в статусе ожидания вашего подтверждения.", show_alert=True)
        return

    now = datetime.utcnow().isoformat()
    cursor.execute(
        "UPDATE deals SET status=?, updated_at=? WHERE id=?",
        (STATUS_COMPLETED, now, deal_id)
    )
    conn.commit()
    log_action(deal_id, f"Покупатель {user_id} подтвердил успешную сделку")

    await call.message.edit_text(
        f"Вы подтвердили, что сделка #{deal_id} завершена успешно.\n\n"
        "Гарант переведёт средства продавцу вручную согласно своим условиям.",
        reply_markup=main_menu_kb()
    )

    try:
        await bot.send_message(
            seller_id,
            f"✅ Покупатель подтвердил успешное завершение сделки #{deal_id}.\n"
            "Гарант переведёт вам средства согласно условиям сервиса."
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить продавца {seller_id} об успешном завершении: {e}")

    # Уведомляем админов
    for adm in ADMIN_IDS:
        try:
            await bot.send_message(
                adm,
                f"✅ Сделка #{deal_id} завершена успешно.\n"
                f"Не забудьте произвести перевод средств продавцу."
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа {adm} о завершении сделки: {e}")

    await call.answer("Сделка завершена.")


@dp.callback_query_handler(Text(startswith="buyer_dispute_"))
async def cb_buyer_dispute(call: types.CallbackQuery):
    user_id = call.from_user.id
    deal_id = int(call.data.split("_")[-1])

    row = get_deal(deal_id)
    if not row:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    _, buyer_id, seller_id, amount, description, status, _, _ = row

    if user_id != buyer_id:
        await call.answer("Вы не являетесь покупателем в этой сделке.", show_alert=True)
        return
    if status != STATUS_WAIT_BUYER_CONFIRM:
        await call.answer("Сделка не в статусе ожидания вашего подтверждения.", show_alert=True)
        return

    now = datetime.utcnow().isoformat()
    cursor.execute(
        "UPDATE deals SET status=?, updated_at=? WHERE id=?",
        (STATUS_DISPUTE, now, deal_id)
    )
    conn.commit()
    log_action(deal_id, f"Покупатель {user_id} открыл спор по сделке")

    await call.message.edit_text(
        f"Вы открыли спор по сделке #{deal_id}.\n\n"
        "Админы-гаранты рассмотрят ситуацию и примут решение.",
        reply_markup=main_menu_kb()
    )

    # Уведомляем продавца
    try:
        await bot.send_message(
            seller_id,
            f"⚠️ Покупатель открыл спор по сделке #{deal_id}.\n"
            "Ожидается решение гаранта."
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить продавца {seller_id} об открытии спора: {e}")

    # Уведомляем админов
    for adm in ADMIN_IDS:
        try:
            await bot.send_message(
                adm,
                f"⚠️ Открыт СПОР по сделке #{deal_id}.\n\n"
                f"{format_deal_text(get_deal(deal_id))}\n\n"
                "Примите решение:",
                reply_markup=admin_dispute_kb(deal_id)
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа {adm} о споре: {e}")

    await call.answer("Спор открыт.")


# ==========================
#   АДМИНЫ: РЕШЕНИЕ СПОРА
# ==========================
@dp.callback_query_handler(Text(startswith="adm_"))
async def cb_admin_dispute(call: types.CallbackQuery):
    user_id = call.from_user.id
    if not is_admin(user_id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return

    parts = call.data.split("_")
    action = parts[1]
    deal_id = int(parts[-1])

    row = get_deal(deal_id)
    if not row:
        await call.answer("Сделка не найдена.", show_alert=True)
        return
    _, buyer_id, seller_id, amount, description, status, _, _ = row

    if status != STATUS_DISPUTE:
        await call.answer("Эта сделка уже не в статусе спора.", show_alert=True)
        return

    now = datetime.utcnow().isoformat()

    if action == "buyer":
        new_status = STATUS_RESOLVED_BUYER
        result_text = "Спор решён в пользу покупателя"
        log_text = f"Админ {user_id} решил спор в пользу покупателя"
    elif action == "seller":
        new_status = STATUS_RESOLVED_SELLER
        result_text = "Спор решён в пользу продавца"
        log_text = f"Админ {user_id} решил спор в пользу продавца"
    elif action == "partial":
        new_status = STATUS_RESOLVED_PARTIAL
        result_text = "Спор решён частично (частичный возврат)"
        log_text = f"Админ {user_id} решил спор частично (частичный возврат)"
    else:
        await call.answer("Неизвестное действие.", show_alert=True)
        return

    cursor.execute(
        "UPDATE deals SET status=?, updated_at=? WHERE id=?",
        (new_status, now, deal_id)
    )
    conn.commit()
    log_action(deal_id, log_text)

    await call.message.edit_text(
        f"Вы приняли решение по сделке #{deal_id}.\n\n"
        f"{result_text}.\n"
        "Не забудьте провести выплаты вручную согласно решению.",
        reply_markup=main_menu_kb()
    )

    # Уведомляем стороны
    notify_text = (
        f"⚖️ По сделке #{deal_id} принято решение:\n"
        f"{result_text}.\n\n"
        "Перевод средств будет осуществлён гарантом вручную."
    )
    for uid in {buyer_id, seller_id}:
        try:
            await bot.send_message(uid, notify_text)
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {uid} о решении спора: {e}")

    await call.answer("Решение зафиксировано.")


# ==========================
#   КОМАНДА /cancel
# ==========================
@dp.message_handler(commands=["cancel"])
async def cmd_cancel(message: types.Message):
    set_state(message.from_user.id, None)
    await message.answer("Текущий процесс отменён.", reply_markup=main_menu_kb())


# ==========================
#   ЗАПУСК БОТА
# ==========================
if __name__ == "__main__":
    logger.info("Бот-гарант запущен.")
    executor.start_polling(dp, skip_updates=True)
