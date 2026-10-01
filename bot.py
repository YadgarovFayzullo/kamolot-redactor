"""Telegram-бот для приёма статей в журнал.

Этапы:
  1. Анкета автора (FSM): ФИО → статус → место работы → тема → область науки → язык
  2. Подтверждение анкеты → заявка сохраняется в БД, автору показываются реквизиты оплаты
  3. Автор отправляет чек (фото / PDF) → администратор подтверждает или отклоняет
  4. После подтверждения оплаты автору сообщается, что статья в работе — заявка завершена

Этапы 3–4 хранятся в SQLite, поэтому переживают перезапуск бота.
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# ID администраторов / чата редакции. Несколько — через запятую.
EDITOR_CHAT_IDS = [int(x) for x in os.getenv("EDITOR_CHAT_IDS", "").replace(" ", "").split(",") if x]
DB_PATH = os.getenv("DB_PATH", "orders.db")

JOURNAL_NAME = os.getenv("JOURNAL_NAME", "Pedagogika va Psixologiya. Ilmiy-nazariy va metodik jurnal")
PAYMENT_AMOUNT = os.getenv("PAYMENT_AMOUNT", "130 000 so'm")
CARD_NUMBER = os.getenv("CARD_NUMBER", "")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "nayimov_82").lstrip("@")

AUTHOR_STATUSES = [
    ["Professor", "Dotsent"],
    ["O'qituvchi", "Bakalavr talabasi"],
    ["Magistrant", "Doktorant"],
    ["Tayanch doktorant", "Mustaqil izlanuvchi"],
    ["Boshqa"],
]

LANGUAGES = [
    ["🇺🇿 O'zbek", "🇷🇺 Rus"],
    ["🇬🇧 Ingliz", "🇹🇷 Turk"],
    ["🇹🇯 Tojik", "🇰🇿 Qozoq"],
]

SCIENCE_FIELDS = [
    "Pedagogika",
    "Psixologiya",
    "Filologiya",
    "Tarix",
    "Iqtisodiyot",
    "Huquq",
    "Fizika-matematika",
    "Texnika",
    "Kimyo",
    "Biologiya",
    "Tibbiyot",
    "Qishloq xo'jaligi",
]

# Состояния заявки в БД
ST_RECEIPT = "receipt"          # ждём чек
ST_REVIEW = "payment_review"    # чек у администратора
ST_ARTICLE = "article"          # устаревшее: раньше ждали файл статьи
ST_DONE = "done"
ST_CANCELLED = "cancelled"

STATE_LABELS = {
    ST_RECEIPT: "💳 To'lov cheki kutilmoqda",
    ST_REVIEW: "⏳ Chek tekshirilmoqda",
    ST_ARTICLE: "📄 Maqola fayli kutilmoqda",
    ST_DONE: "✅ To'lov tasdiqlangan",
    ST_CANCELLED: "❌ Bekor qilingan",
}

BTN_NEW = "📝 Maqola topshirish"
BTN_CANCEL = "❌ Bekor qilish"
BTN_SUPPORT = "🆘 Yordam"

router = Router()


# ---------- Анкета (FSM) ----------

class Form(StatesGroup):
    full_name = State()
    phone = State()
    author_status = State()
    workplace = State()
    topic = State()
    field = State()
    field_custom = State()
    language = State()
    confirm = State()


FORM_ORDER = ["full_name", "phone", "author_status", "workplace", "topic", "field", "language"]


# ---------- База данных ----------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db_init() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id     INTEGER NOT NULL,
                username        TEXT,
                full_name       TEXT NOT NULL,
                author_status   TEXT NOT NULL,
                workplace       TEXT NOT NULL,
                topic           TEXT NOT NULL,
                field           TEXT NOT NULL,
                language        TEXT NOT NULL,
                state           TEXT NOT NULL,
                payment_status  TEXT NOT NULL DEFAULT 'kutilmoqda',
                article_status  TEXT NOT NULL DEFAULT 'kutilmoqda',
                receipt_file_id TEXT,
                receipt_type    TEXT,
                article_file_id TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
        if "phone" not in columns:
            conn.execute("ALTER TABLE applications ADD COLUMN phone TEXT NOT NULL DEFAULT ''")


def db_create_application(user_id: int, username: str | None, data: dict) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO applications (telegram_id, username, full_name, phone, author_status, workplace, "
            "topic, field, language, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id, username or "",
                data["full_name"], data["phone"], data["author_status"], data["workplace"],
                data["topic"], data["field"], data["language"],
                ST_RECEIPT, now_str(), now_str(),
            ),
        )
        return cur.lastrowid


def db_get(app_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()


def db_latest(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE telegram_id = ? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()


def db_update(app_id: int, **fields) -> None:
    fields["updated_at"] = now_str()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE applications SET {cols} WHERE id = ?", [*fields.values(), app_id])


def db_last(limit: int = 10) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM applications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---------- Клавиатуры ----------

def reply_kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True
    )


def main_kb() -> ReplyKeyboardMarkup:
    return reply_kb([[BTN_NEW], [BTN_SUPPORT]])


def cancel_kb() -> ReplyKeyboardMarkup:
    return reply_kb([[BTN_CANCEL]])


def phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
    )


def fields_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=SCIENCE_FIELDS[i + j], callback_data=f"field:{i + j}")
         for j in range(2) if i + j < len(SCIENCE_FIELDS)]
        for i in range(0, len(SCIENCE_FIELDS), 2)
    ]
    rows.append([InlineKeyboardButton(text="✏️ Boshqa soha", callback_data="field:other")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash va to'lovga o'tish", callback_data="confirm:send")],
        [
            InlineKeyboardButton(text="✏️ F.I.Sh.", callback_data="edit:full_name"),
            InlineKeyboardButton(text="✏️ Telefon", callback_data="edit:phone"),
        ],
        [InlineKeyboardButton(text="✏️ Maqom", callback_data="edit:author_status")],
        [
            InlineKeyboardButton(text="✏️ Ish joyi", callback_data="edit:workplace"),
            InlineKeyboardButton(text="✏️ Mavzu", callback_data="edit:topic"),
        ],
        [
            InlineKeyboardButton(text="✏️ Soha", callback_data="edit:field"),
            InlineKeyboardButton(text="✏️ Til", callback_data="edit:language"),
        ],
        [InlineKeyboardButton(text="❌ Bekor qilish", callback_data="confirm:cancel")],
    ])


def payment_admin_kb(app_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ TASDIQLASH", callback_data=f"pay_ok:{app_id}"),
        InlineKeyboardButton(text="❌ RAD ETISH", callback_data=f"pay_no:{app_id}"),
    ]])


# ---------- Тексты ----------

FIO_RE = re.compile(r"^[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳʼʻ'’\-]+(\s+[A-Za-zА-Яа-яЁёЎўҚқҒғҲҳʼʻ'’\-\.]+){1,3}$")

e = html.escape


def form_summary(data: dict) -> str:
    return (
        f"👤 <b>F.I.Sh.:</b> {e(data['full_name'])}\n"
        f"📱 <b>Telefon:</b> {e(data.get('phone') or '-')}\n"
        f"🎓 <b>Maqomi:</b> {e(data['author_status'])}\n"
        f"🏢 <b>Ish/o'qish joyi:</b> {e(data['workplace'])}\n"
        f"📝 <b>Maqola mavzusi:</b> {e(data['topic'])}\n"
        f"🔬 <b>Ilmiy soha:</b> {e(data['field'])}\n"
        f"🌐 <b>Til:</b> {e(data['language'])}"
    )


def app_summary(app: sqlite3.Row) -> str:
    return (
        f"📌 <b>Ariza №{app['id']}</b>\n\n"
        f"{form_summary(dict(app))}"
    )


def user_contact(app: sqlite3.Row) -> str:
    if app["username"]:
        return f"@{e(app['username'])}"
    return f'<a href="tg://user?id={app["telegram_id"]}">profil</a>'


def payment_text(app_id: int) -> str:
    return (
        f"💳 <b>To'lov ma'lumotlari (ariza №{app_id})</b>\n\n"
        f"Jurnal: <b>{e(JOURNAL_NAME)}</b>\n"
        f"To'lov: <b>{e(PAYMENT_AMOUNT)}</b>\n"
        f"Karta: <code>{e(CARD_NUMBER)}</code>\n"
        f"Karta egasi: <b>{e(CARD_HOLDER)}</b>\n\n"
        "To'lovni amalga oshirgach, chekni <b>rasm yoki PDF</b> formatida shu yerga yuboring.\n\n"
        "⚠️ Faqat haqiqiy to'lov chekini yuboring."
    )


def is_editor(chat_id: int, user_id: int) -> bool:
    return chat_id in EDITOR_CHAT_IDS or user_id in EDITOR_CHAT_IDS


async def notify_editors(bot: Bot, text: str, file_id: str | None = None, file_type: str | None = None,
                         caption: str = "", reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Отправляет редакторам текст заявки и (опционально) файл с кнопками под ним."""
    for chat_id in EDITOR_CHAT_IDS:
        try:
            await bot.send_message(chat_id, text, reply_markup=None if file_id else reply_markup)
            if file_type == "photo":
                await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=reply_markup)
            elif file_type == "document":
                await bot.send_document(chat_id, file_id, caption=caption, reply_markup=reply_markup)
        except Exception:
            logging.exception("Редактору %s не удалось отправить сообщение", chat_id)


# ---------- Шаги анкеты ----------

async def ask(message: Message, state: FSMContext, step: str) -> None:
    n = FORM_ORDER.index(step) + 1
    head = f"<b>{n}/{len(FORM_ORDER)}.</b> "
    if step == "full_name":
        await state.set_state(Form.full_name)
        await message.answer(
            head + "Muallifning familiyasi, ismi va otasining ismini to'liq kiriting.\n\n"
            "<i>Nayimov Azizbek Alisherovich</i>",
            reply_markup=cancel_kb(),
        )
    elif step == "phone":
        await state.set_state(Form.phone)
        await message.answer(
            head + "Telefon raqamingizni yuboring — tahririyat siz bilan shu raqam orqali bog'lanadi.\n\n"
            "Pastdagi «📱 Telefon raqamni yuborish» tugmasini bosing yoki raqamni yozing.\n"
            "<i>Masalan: +998 90 123 45 67</i>",
            reply_markup=phone_kb(),
        )
    elif step == "author_status":
        await state.set_state(Form.author_status)
        await message.answer(head + "Maqomingizni tanlang:", reply_markup=reply_kb(AUTHOR_STATUSES + [[BTN_CANCEL]]))
    elif step == "workplace":
        await state.set_state(Form.workplace)
        await message.answer(
            head + "Ish yoki o'qish joyingizni to'liq kiriting.\n\n<i>Masalan:\n"
            "Buxoro davlat pedagogika instituti, dotsent\nyoki\n"
            "Buxoro davlat universiteti, 2-kurs magistranti</i>",
            reply_markup=cancel_kb(),
        )
    elif step == "topic":
        await state.set_state(Form.topic)
        await message.answer(head + "Maqolangiz mavzusini (nomini) to'liq kiriting:", reply_markup=cancel_kb())
    elif step == "field":
        await state.set_state(Form.field)
        await message.answer(head + "Ilmiy sohani tanlang:", reply_markup=fields_kb())
    elif step == "language":
        await state.set_state(Form.language)
        await message.answer(head + "Maqola qaysi tilda yozilgan?", reply_markup=reply_kb(LANGUAGES + [[BTN_CANCEL]]))


async def show_confirm(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.confirm)
    data = await state.get_data()
    await message.answer("Ma'lumotlarni tekshiring:", reply_markup=ReplyKeyboardRemove())
    await message.answer(form_summary(data), reply_markup=confirm_kb())


async def save_and_next(message: Message, state: FSMContext, **values) -> None:
    """Сохраняет поле и переходит к следующему незаполненному (или к подтверждению при редактировании)."""
    await state.update_data(**values)
    data = await state.get_data()
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    for step in FORM_ORDER:
        if step not in data:
            await ask(message, state, step)
            return
    await show_confirm(message, state)


def clean(text: str) -> str:
    return " ".join(text.split())


# ---------- Общие команды ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"Assalomu alaykum!\n\n<b>{e(JOURNAL_NAME)}</b> rasmiy Telegram botiga xush kelibsiz.\n\n"
        "Bot orqali ilmiy maqolangizni jurnalga topshirishingiz mumkin.",
        reply_markup=main_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Botdan foydalanish:\n\n"
        "/start — bosh menyu\n"
        "/new — yangi maqola topshirish\n"
        "/cancel — joriy jarayonni bekor qilish\n"
        f"/support — administrator bilan bog'lanish (@{e(SUPPORT_USERNAME)})\n\n"
        "Savollarga ketma-ket javob bering. To'lov chekini rasm yoki PDF ko'rinishida yuboring."
    )


@router.message(Command("support"))
@router.message(F.text == BTN_SUPPORT)
async def cmd_support(message: Message) -> None:
    await message.answer(
        f"Savollar bo'yicha administratorga yozing: @{e(SUPPORT_USERNAME)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✉️ Administratorga yozish", url=f"https://t.me/{SUPPORT_USERNAME}")
        ]]),
    )


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    in_form = await state.get_state() is not None
    await state.clear()
    app = db_latest(message.from_user.id)
    if not in_form and app and app["state"] == ST_RECEIPT:
        db_update(app["id"], state=ST_CANCELLED)
    await message.answer("Jarayon bekor qilindi.", reply_markup=main_kb())


@router.message(Command("new"))
@router.message(F.text == BTN_NEW)
async def cmd_new(message: Message, state: FSMContext) -> None:
    await state.clear()
    await ask(message, state, "full_name")


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>")


@router.message(Command("orders"))
async def cmd_orders(message: Message) -> None:
    if not is_editor(message.chat.id, message.from_user.id):
        return
    rows = db_last()
    if not rows:
        await message.answer("Arizalar hali yo'q.")
        return
    text = "\n\n".join(
        f"<b>№{r['id']}</b> · {r['created_at']}\n{e(r['full_name'])} — {e(r['topic'])}\n"
        f"{STATE_LABELS.get(r['state'], r['state'])}"
        for r in rows
    )
    await message.answer(f"Oxirgi arizalar:\n\n{text}")


# ---------- Анкета ----------

@router.message(Form.full_name, F.text)
async def got_full_name(message: Message, state: FSMContext) -> None:
    value = clean(message.text)
    if not FIO_RE.match(value) or len(value) > 150:
        await message.answer(
            "F.I.Sh.ni harflar bilan to'liq kiriting (kamida familiya va ism).\n"
            "<i>Nayimov Azizbek Alisherovich</i>"
        )
        return
    await save_and_next(message, state, full_name=value)


@router.message(Form.phone, F.contact)
async def got_phone_contact(message: Message, state: FSMContext) -> None:
    contact = message.contact
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer("Iltimos, o'zingizning raqamingizni tugma orqali yuboring.", reply_markup=phone_kb())
        return
    phone = contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone
    await save_and_next(message, state, phone=phone)


@router.message(Form.phone, F.text)
async def got_phone_text(message: Message, state: FSMContext) -> None:
    digits = re.sub(r"\D", "", message.text)
    if not 9 <= len(digits) <= 15:
        await message.answer(
            "Telefon raqami noto'g'ri. Tugma orqali yuboring yoki to'liq yozing.\n"
            "<i>Masalan: +998 90 123 45 67</i>",
            reply_markup=phone_kb(),
        )
        return
    if len(digits) == 9:  # местный номер без кода страны
        digits = "998" + digits
    await save_and_next(message, state, phone="+" + digits)


@router.message(Form.author_status, F.text)
async def got_author_status(message: Message, state: FSMContext) -> None:
    value = clean(message.text)
    if not 2 <= len(value) <= 60:
        await message.answer("Maqomni tugma orqali tanlang yoki qisqa yozing.")
        return
    await save_and_next(message, state, author_status=value)


@router.message(Form.workplace, F.text)
async def got_workplace(message: Message, state: FSMContext) -> None:
    value = clean(message.text)
    if not 3 <= len(value) <= 300:
        await message.answer("Ish/o'qish joyi 3 dan 300 belgigacha bo'lishi kerak. Qayta kiriting:")
        return
    await save_and_next(message, state, workplace=value)


@router.message(Form.topic, F.text)
async def got_topic(message: Message, state: FSMContext) -> None:
    value = clean(message.text)
    if not 5 <= len(value) <= 500:
        await message.answer("Mavzu 5 dan 500 belgigacha bo'lishi kerak. Qayta kiriting:")
        return
    await save_and_next(message, state, topic=value)


@router.callback_query(Form.field, F.data.startswith("field:"))
async def got_field(call: CallbackQuery, state: FSMContext) -> None:
    value = call.data.split(":", 1)[1]
    await call.answer()
    if value == "other":
        await state.set_state(Form.field_custom)
        await call.message.edit_text("Ilmiy sohangizni yozing:")
        return
    field = SCIENCE_FIELDS[int(value)]
    await call.message.edit_text(f"Ilmiy soha: <b>{field}</b>")
    await save_and_next(call.message, state, field=field)


@router.message(Form.field_custom, F.text)
async def got_field_custom(message: Message, state: FSMContext) -> None:
    value = clean(message.text)
    if not 2 <= len(value) <= 100:
        await message.answer("Soha nomi 2 dan 100 belgigacha bo'lishi kerak. Qayta kiriting:")
        return
    await save_and_next(message, state, field=value)


@router.message(Form.field)
async def field_use_buttons(message: Message) -> None:
    await message.answer("Sohani yuqoridagi tugmalar orqali tanlang yoki «Boshqa soha»ni bosing.")


@router.message(Form.language, F.text)
async def got_language(message: Message, state: FSMContext) -> None:
    value = clean(message.text)
    if not 2 <= len(value) <= 30:
        await message.answer("Tilni tugma orqali tanlang.")
        return
    await save_and_next(message, state, language=value)


@router.callback_query(Form.confirm, F.data.startswith("edit:"))
async def edit_field(call: CallbackQuery, state: FSMContext) -> None:
    step = call.data.split(":", 1)[1]
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await state.update_data(editing=True)
    await ask(call.message, state, step)


@router.callback_query(Form.confirm, F.data == "confirm:cancel")
async def confirm_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer("Ariza bekor qilindi.", reply_markup=main_kb())


@router.callback_query(Form.confirm, F.data == "confirm:send")
async def confirm_send(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    app_id = db_create_application(call.from_user.id, call.from_user.username, data)
    await state.clear()
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"📝 Ariza №{app_id} yaratildi.")
    await call.message.answer(payment_text(app_id), reply_markup=main_kb())


@router.message(Form.confirm)
async def confirm_use_buttons(message: Message) -> None:
    await message.answer("Ariza ostidagi tugmalardan foydalaning: tasdiqlash, o'zgartirish yoki bekor qilish.")


@router.message(StateFilter(Form.full_name, Form.phone, Form.author_status, Form.workplace,
                            Form.topic, Form.field_custom, Form.language))
async def form_not_text(message: Message) -> None:
    await message.answer("Iltimos, javobni matn ko'rinishida yuboring.")


# ---------- Чек (состояние в БД) ----------

@router.message(F.photo | F.document)
async def got_receipt(message: Message, bot: Bot) -> None:
    app = db_latest(message.from_user.id)
    if not app or app["state"] != ST_RECEIPT:
        await message.answer(
            f"Bu bosqichda fayl yuborish talab qilinmaydi. Yangi ariza uchun «{BTN_NEW}» tugmasini bosing.",
            reply_markup=main_kb(),
        )
        return
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    else:
        mime = message.document.mime_type or ""
        if not (mime.startswith("image/") or mime == "application/pdf"):
            await message.answer("Chekni rasm yoki PDF formatida yuboring.")
            return
        file_id, file_type = message.document.file_id, "document"

    db_update(app["id"], state=ST_REVIEW, payment_status="tekshirilmoqda",
              receipt_file_id=file_id, receipt_type=file_type)
    app = db_get(app["id"])
    await message.answer(
        f"✅ Ariza №{app['id']}: chekingiz qabul qilindi va administrator tekshiruviga yuborildi.\n\n"
        "Natija tasdiqlangach, sizga bot orqali xabar keladi.",
        reply_markup=main_kb(),
    )
    await notify_editors(
        bot,
        f"💳 <b>YANGI TO'LOV CHEKI</b>\n\n{app_summary(app)}\n\n👤 Telegram: {user_contact(app)}",
        file_id, file_type,
        caption=f"Ariza №{app['id']} — to'lov cheki",
        reply_markup=payment_admin_kb(app["id"]),
    )


# ---------- Решение администратора по оплате ----------

@router.callback_query(F.data.regexp(r"^pay_(ok|no):\d+$"))
async def admin_payment(call: CallbackQuery, bot: Bot) -> None:
    if not is_editor(call.message.chat.id, call.from_user.id):
        await call.answer("Sizda bu amal uchun ruxsat yo'q.", show_alert=True)
        return
    action, app_id = call.data.split(":")
    app = db_get(int(app_id))
    await call.message.edit_reply_markup(reply_markup=None)
    if not app or app["state"] != ST_REVIEW:
        await call.answer("Bu ariza allaqachon ko'rib chiqilgan.", show_alert=True)
        return
    await call.answer()

    if action == "pay_ok":
        db_update(app["id"], state=ST_DONE, payment_status="✅ tasdiqlangan")
        user_text = (
            f"✅ <b>Chek qabul qilindi, to'lov tasdiqlandi.</b>\n\n"
            f"Hurmatli {e(app['full_name'])}, ariza №{app['id']} bo'yicha maqolangizni kuting."
        )
        admin_text = f"✅ Ariza №{app['id']}: to'lov tasdiqlandi."
    else:
        db_update(app["id"], state=ST_RECEIPT, payment_status="❌ rad etilgan")
        user_text = (
            f"❌ <b>TO'LOV TASDIQLANMADI</b> (ariza №{app['id']})\n\n"
            "Yuborilgan chek bo'yicha to'lov aniqlanmadi yoki chek ma'lumotlari mos kelmadi.\n\n"
            "Iltimos, to'lovni tekshirib, haqiqiy chekni qayta yuboring."
        )
        admin_text = f"❌ Ariza №{app['id']}: to'lov rad etildi."

    try:
        await bot.send_message(app["telegram_id"], user_text)
    except Exception:
        logging.exception("Muallifga xabar yuborilmadi (ariza %s)", app["id"])
        admin_text += "\n⚠️ Muallifga xabar yetkazilmadi (botni bloklagan bo'lishi mumkin)."
    await call.message.answer(admin_text)


# ---------- Прочий текст вне анкеты ----------

@router.message(StateFilter(None))
async def fallback(message: Message) -> None:
    app = db_latest(message.from_user.id)
    hints = {
        ST_RECEIPT: "Ariza №{id}: iltimos, to'lov chekini rasm yoki PDF fayl ko'rinishida yuboring.",
        ST_REVIEW: "⏳ Ariza №{id}: chekingiz administrator tekshiruvida. Iltimos, natijani kuting.",
    }
    text = hints[app["state"]].format(id=app["id"]) if app and app["state"] in hints else None
    await message.answer(text or f"Maqola topshirish uchun «{BTN_NEW}» tugmasini bosing.", reply_markup=main_kb())


# ---------- Запуск ----------

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")
    if not CARD_NUMBER:
        logging.warning("CARD_NUMBER не задан — авторы не увидят реквизиты оплаты")
    if not EDITOR_CHAT_IDS:
        logging.warning("EDITOR_CHAT_IDS не задан — некому подтверждать оплату")
    db_init()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
