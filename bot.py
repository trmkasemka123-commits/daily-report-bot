# -*- coding: utf-8 -*-
"""
Бот итогов дня — Фаза 1 (бета)
Стек: Python + aiogram 3.x, хостинг Render (polling + aiohttp keep-alive).
Бета: кнопки-меню вместо команд, имя сотрудника и дата в итоге.
Без AI и без WEEEK (следующие фазы).
"""

import asyncio
import logging
import os
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiohttp import web

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# ТЕКСТ КНОПОК
# ============================================================

BTN_REPORT = "Создать итог дня"
BTN_HELP = "Помощь"

# ============================================================
# ВОПРОСЫ
# ============================================================

QUESTIONS = [
    ("projects", "Над какими проектами сегодня работал?", "Проекты"),
    ("done", "Что конкретно делал и что сделал по каждому проекту?", "Что делал и сделал"),
    ("problems", "Были трудности или нерешённые проблемы? (если нет — поставь прочерк)", "Трудности и замечания"),
    ("learned", "Узнал или попробовал что-то новое сегодня? (если нет — поставь прочерк)", "Что нового узнал"),
    ("misc", "Координация, встречи, прочее? (если нет — поставь прочерк)", "Координация и прочее"),
]

# ============================================================
# СОСТОЯНИЯ FSM
# ============================================================

class ReportStates(StatesGroup):
    answering = State()

# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REPORT)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )

# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

dp = Dispatcher(storage=MemoryStorage())

# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================

def get_user_name(message: Message) -> str:
    u = message.from_user
    if u.full_name:
        return u.full_name
    if u.username:
        return u.username
    return "Сотрудник"


async def start_report(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ReportStates.answering)
    await state.update_data(answers={}, q_index=0)
    first_q = QUESTIONS[0]
    await message.answer(f"Вопрос 1 из {len(QUESTIONS)}\n\n{first_q[1]}")


HELP_TEXT = (
    "Я помогаю быстро собрать итог дня.\n\n"
    "Нажми «Создать итог дня» — я задам 5 коротких вопросов "
    "и соберу из ответов аккуратный структурированный отчёт, "
    "готовый к отправке.\n\n"
    "В следующих версиях итог будет автоматически улучшаться "
    "и попадать прямо в WEEEK.\n\n"
    "Команды: /report — начать, /cancel — прервать."
)

# ============================================================
# ХЕНДЛЕРЫ
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Привет, {get_user_name(message)}!\n\n"
        "Я помогу собрать твой итог дня. Нажми кнопку ниже, чтобы начать.",
        reply_markup=main_menu(),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Сейчас нечего отменять.", reply_markup=main_menu())
        return
    await state.clear()
    await message.answer("Отменил. Нажми кнопку, чтобы начать заново.", reply_markup=main_menu())


@dp.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    await start_report(message, state)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@dp.message(F.text == BTN_REPORT)
async def btn_report(message: Message, state: FSMContext):
    await start_report(message, state)


@dp.message(F.text == BTN_HELP)
async def btn_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@dp.message(ReportStates.answering, F.text)
async def process_answer(message: Message, state: FSMContext):
    if message.text in (BTN_REPORT, BTN_HELP):
        if message.text == BTN_REPORT:
            await start_report(message, state)
        else:
            await message.answer(HELP_TEXT)
        return

    data = await state.get_data()
    answers = data.get("answers", {})
    q_index = data.get("q_index", 0)

    key = QUESTIONS[q_index][0]
    answers[key] = message.text.strip()
    q_index += 1

    if q_index < len(QUESTIONS):
        await state.update_data(answers=answers, q_index=q_index)
        next_q = QUESTIONS[q_index]
        await message.answer(f"Вопрос {q_index + 1} из {len(QUESTIONS)}\n\n{next_q[1]}")
    else:
        report = build_report(answers, get_user_name(message))
        await state.clear()
        await message.answer(report, reply_markup=main_menu())
        await message.answer("Готово! Это твой итог дня. Можешь скопировать его целиком.")


@dp.message(ReportStates.answering)
async def process_non_text(message: Message):
    await message.answer("Пожалуйста, ответь текстом.")


@dp.message()
async def fallback(message: Message):
    await message.answer("Выбери действие на кнопках ниже.", reply_markup=main_menu())

# ============================================================
# ФОРМИРОВАНИЕ ИТОГА
# ============================================================

def escape_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_report(answers: dict, user_name: str) -> str:
    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"Итог дня — {today}", f"Сотрудник: {user_name}", ""]
    for key, _question, heading in QUESTIONS:
        answer = answers.get(key, "").strip() or "—"
        lines.append(f"{heading}:")
        lines.append(answer)
        lines.append("")
    body = "\n".join(lines).strip()
    return f"<pre>{escape_html(body)}</pre>"

# ============================================================
# KEEP-ALIVE ВЕБ-СЕРВЕР (для Render)
# ============================================================

async def handle_root(request):
    return web.Response(text="Bot is alive")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Keep-alive web server started on port {PORT}")

# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Укажи его в переменных окружения.")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await start_web_server()
    logger.info("Бот запущен (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
