# -*- coding: utf-8 -*-
"""
Бот итогов дня — Фаза 1
Собирает ответы сотрудника на вопросы и формирует структурированный итог дня.
Стек: Python + aiogram 3.x, хостинг Render (polling + aiohttp keep-alive).

Фаза 1: без AI и без WEEEK. Бот задаёт вопросы по очереди,
собирает ответы и выдаёт аккуратно структурированный итог.
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
from aiogram.types import Message
from aiohttp import web

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # токен задаётся в переменных окружения Render
PORT = int(os.getenv("PORT", "10000"))  # Render передаёт порт через переменную PORT

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# ВОПРОСЫ
# ============================================================
# Каждый вопрос — кортеж (ключ, текст вопроса, заголовок в итоге)

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
    answering = State()  # пользователь отвечает на вопросы


# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

dp = Dispatcher(storage=MemoryStorage())


# ============================================================
# ХЕНДЛЕРЫ
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я помогу собрать твой итог дня.\n\n"
        "Я задам несколько коротких вопросов, а в конце соберу из ответов "
        "аккуратный структурированный отчёт.\n\n"
        "Чтобы начать — напиши /report\n"
        "Прервать в любой момент — /cancel"
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Сейчас нечего отменять. Напиши /report чтобы начать.")
        return
    await state.clear()
    await message.answer("Отменил. Напиши /report чтобы начать заново.")


@dp.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ReportStates.answering)
    await state.update_data(answers={}, q_index=0)
    # Задаём первый вопрос
    first_q = QUESTIONS[0]
    await message.answer(
        f"Вопрос 1 из {len(QUESTIONS)}\n\n{first_q[1]}"
    )


@dp.message(ReportStates.answering, F.text)
async def process_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    answers = data.get("answers", {})
    q_index = data.get("q_index", 0)

    # Сохраняем ответ на текущий вопрос
    key = QUESTIONS[q_index][0]
    answers[key] = message.text.strip()

    q_index += 1

    # Есть ли ещё вопросы
    if q_index < len(QUESTIONS):
        await state.update_data(answers=answers, q_index=q_index)
        next_q = QUESTIONS[q_index]
        await message.answer(
            f"Вопрос {q_index + 1} из {len(QUESTIONS)}\n\n{next_q[1]}"
        )
    else:
        # Все вопросы заданы — формируем итог
        report = build_report(answers)
        await state.clear()
        await message.answer(report)
        await message.answer(
            "Готово! Это твой итог дня.\n\n"
            "Чтобы собрать новый — напиши /report"
        )


@dp.message(ReportStates.answering)
async def process_non_text(message: Message):
    # Если прислали не текст во время опроса
    await message.answer("Пожалуйста, ответь текстом.")


@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Напиши /report чтобы собрать итог дня, или /start для справки."
    )


# ============================================================
# ФОРМИРОВАНИЕ ИТОГА
# ============================================================

def build_report(answers: dict) -> str:
    """Собирает структурированный итог из ответов."""
    today = datetime.now().strftime("%d.%m.%Y")
    lines = [f"Итог дня — {today}", ""]

    for key, _question, heading in QUESTIONS:
        answer = answers.get(key, "").strip()
        if not answer:
            answer = "—"
        lines.append(f"{heading}:")
        lines.append(answer)
        lines.append("")  # пустая строка между блоками

    return "\n".join(lines).strip()


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

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Запускаем keep-alive сервер параллельно с ботом
    await start_web_server()

    logger.info("Бот запущен (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
