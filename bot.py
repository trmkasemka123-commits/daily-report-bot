# -*- coding: utf-8 -*-
"""
Бот итогов дня — Фаза 2 (AI через DeepSeek)
Стек: Python + aiogram 3.x, хостинг Render (polling + aiohttp keep-alive).

Фаза 2: собирает ответы сотрудника на 5 вопросов и отправляет их в DeepSeek,
который формирует красивый структурированный итог дня в стиле эталонных примеров.
AI СТРОГО не выдумывает — только переформулирует и структурирует написанное.
Если DeepSeek недоступен — откат на простой формат (Фаза 1).

Без WEEEK (это Фаза 3).
"""

import asyncio
import logging
import os
from datetime import datetime

import aiohttp
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
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
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
# ПРОМТ ДЛЯ DEEPSEEK
# ============================================================

SYSTEM_PROMPT = """Ты помощник, который оформляет рабочие итоги дня сотрудника BIM-компании.

ЗАДАЧА: на основе ответов сотрудника на вопросы составить аккуратный структурированный итог дня.

КРИТИЧЕСКИ ВАЖНОЕ ПРАВИЛО: НИЧЕГО НЕ ВЫДУМЫВАЙ. Используй ТОЛЬКО факты, которые написал сотрудник. Запрещено добавлять любые детали, действия, достижения или выводы, которых нет в его ответах. Если по какому-то блоку информации нет — не выдумывай, просто не включай блок или ставь прочерк. Нельзя приукрашивать или додумывать.

ЧТО МОЖНО: переформулировать корявые формулировки в грамотный деловой язык, сгруппировать по проектам, структурировать, убрать повторы, исправить орфографию.

СТРУКТУРА ИТОГА (используй те блоки, по которым есть информация):
- Группировка по проектам (если проектов несколько — раздели по ним)
- По каждому проекту: что делал и что сделал
- Трудности и замечания (если были)
- Что нового узнал или попробовал (если было)
- Координация, встречи, прочее (если было)

СТИЛЬ: деловой, по пунктам, как в рабочем отчёте. Без эмодзи. Без вводных фраз вроде "вот итог". Сразу содержание.

ВАЖНО ПРО ЛИЦО ПОВЕСТВОВАНИЯ: пиши строго от ПЕРВОГО лица единственного числа, как пишет сам сотрудник о себе ("проверил", "сделал", "разобрался", "потратил время", "нашёл решение"). НЕ пиши от третьего лица ("сотрудник проверил", "он сделал") и не пиши отстранённо. Это отчёт сотрудника о своей работе своими словами.

Вот примеры хороших итогов дня (для понимания стиля и структуры, НЕ копируй их содержание):

ПРИМЕР 1:
Проект: Ленинский
- Проверил собранный плагин, подключив его к новому проекту — работает корректно.
- При сборке возникла проблема с материалами: при миграции ассетов терялись связи. Переназначил все связи вручную.
- Создал второй плагин, загрузил всю корневую директорию с ассетами.
- Изучал разницу между виртуальными и обычными текстурами.
Трудности / замечания
- Основная сложность — потеря связей между материалами при миграции ассетов.
Что нового узнал
- Разобрался, чем виртуальные текстуры отличаются от обычных.

ПРИМЕР 2:
Проект: Резонит
- Продолжил подготовку проекта к выдаче и проверкам.
- Выполнил настройку видов для выгрузки по шести секциям.
- Сформировал XML-файл с ТЭП. Значения совпали с буклетом.
Рабочая координация
- Созвонился с Дмитрием, обсудили статус проекта и дальнейшие шаги.
Итог
- Подготовлены виды по шести секциям.
- Сформирован XML ТЭП, подтверждено соответствие буклету.

Теперь составь итог дня сотрудника на основе его ответов ниже."""


def build_user_prompt(answers: dict) -> str:
    """Формирует текст с ответами сотрудника для отправки в DeepSeek."""
    parts = []
    for key, question, _heading in QUESTIONS:
        ans = answers.get(key, "").strip() or "—"
        parts.append(f"Вопрос: {question}\nОтвет: {ans}")
    return "\n\n".join(parts)


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
    "Нажми «Создать итог дня» — я задам 5 коротких вопросов, "
    "а затем с помощью ИИ оформлю из твоих ответов аккуратный "
    "структурированный отчёт. ИИ ничего не выдумывает — только "
    "приводит твои ответы в порядок.\n\n"
    "В следующей версии итог будет автоматически попадать в WEEEK.\n\n"
    "Команды: /report — начать, /cancel — прервать."
)


# ============================================================
# ВЫЗОВ DEEPSEEK
# ============================================================

async def generate_ai_report(answers: dict) -> str:
    """
    Отправляет ответы в DeepSeek и возвращает готовый итог.
    При любой ошибке возвращает None (вызвавший код сделает откат на простой формат).
    """
    if not DEEPSEEK_API_KEY:
        logger.warning("DEEPSEEK_API_KEY не задан — откат на простой формат")
        return None

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(answers)},
        ],
        "temperature": 0.3,  # низкая — чтобы не фантазировал
        "stream": False,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(DEEPSEEK_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"DeepSeek вернул статус {resp.status}: {text[:300]}")
                    return None
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                return content
    except Exception as e:
        logger.error(f"Ошибка вызова DeepSeek: {e}")
        return None


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
        await state.clear()
        # Сообщаем что идёт обработка ИИ (может занять несколько секунд)
        wait_msg = await message.answer("Формирую итог дня, секунду...")

        ai_report = await generate_ai_report(answers)
        user_name = get_user_name(message)
        today = datetime.now().strftime("%d.%m.%Y")

        if ai_report:
            header = f"Итог дня — {today}\nСотрудник: {user_name}"
            full = f"{header}\n\n{ai_report}"
            body = f"<pre>{escape_html(full)}</pre>"
            note = "Готово! Итог оформлен ИИ. Проверь и при необходимости поправь."
        else:
            # Откат на простой формат если ИИ недоступен
            body = build_simple_report(answers, user_name)
            note = ("Готово! (ИИ временно недоступен — собрал простой формат.) "
                    "Можешь скопировать целиком.")

        # Удаляем "Формирую итог..." и шлём результат
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await message.answer(body, reply_markup=main_menu())
        await message.answer(note)


@dp.message(ReportStates.answering)
async def process_non_text(message: Message):
    await message.answer("Пожалуйста, ответь текстом.")


@dp.message()
async def fallback(message: Message):
    await message.answer("Выбери действие на кнопках ниже.", reply_markup=main_menu())


# ============================================================
# ФОРМАТИРОВАНИЕ
# ============================================================

def escape_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_simple_report(answers: dict, user_name: str) -> str:
    """Запасной простой формат (Фаза 1) — если ИИ недоступен."""
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
