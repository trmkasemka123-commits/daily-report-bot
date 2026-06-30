# -*- coding: utf-8 -*-
"""
Бот итогов дня — Фаза 3 (регистрация + AI + WEEEK)
Стек: Python + aiogram 3.x, хранилище GitHub-backed JSON, хостинг Render.

Флоу:
1. Первый вход -> регистрация: выбор своей фамилии кнопкой. Запоминается в GitHub.
2. "Создать итог дня" -> 5 вопросов -> DeepSeek оформляет итог (от 1 лица, без выдумок).
3. Бот показывает итог + кнопка "Отправить в WEEEK".
4. По кнопке -> создаётся задача "Итоги дня ДД.ММ.ГГГГ" в колонке сотрудника.
5. "Сменить пользователя" -> повторная регистрация.
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
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiohttp import web

import storage
import weeek_client

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
# СОТРУДНИКИ (фамилия -> column_id колонки в WEEEK)
# ============================================================

EMPLOYEES = {
    "Селиванов Артемий": 4,
    "Курочкина Дарья": 5,
    "Редькин Пётр": 6,
}

# ============================================================
# КНОПКИ МЕНЮ
# ============================================================

BTN_REPORT = "Создать итог дня"
BTN_CHANGE_USER = "Сменить пользователя"
BTN_HELP = "Помощь"

# ============================================================
# ВОПРОСЫ
# ============================================================

QUESTIONS = [
    ("projects", "Над какими проектами сегодня работал(а)? Перечисли через запятую или списком", "Проекты"),
    ("done", "Расскажи что конкретно делал(а) по каждому проекту...", "Что делал и сделал"),
    ("problems", "Были ли трудности или какие либо проблемы? Как их удалось решить? (если нет — поставь прочерк)", "Трудности и замечания"),
    ("learned", "Узнал или попробовал ли что-то новое сегодня? (если нет — поставь прочерк)", "Что нового узнал"),
    ("misc", "Координация, встречи, другие взаимодействия с командой? (если нет — поставь прочерк)", "Координация и прочее"),
]

# ============================================================
# ПРОМТ DEEPSEEK
# ============================================================

SYSTEM_PROMPT = """Ты помощник, который оформляет рабочие итоги дня сотрудника BIM-компании.

ЗАДАЧА: на основе ответов сотрудника составить аккуратный структурированный итог дня, разбитый на РАЗДЕЛЫ.

КРИТИЧЕСКИ ВАЖНО: НИЧЕГО НЕ ВЫДУМЫВАЙ. Используй ТОЛЬКО факты, которые написал сотрудник. Запрещено добавлять детали, действия, достижения или выводы, которых нет в ответах. Нельзя приукрашивать или додумывать.

ЧТО МОЖНО: переформулировать корявые предложения в грамотный деловой язык, убрать повторы, исправить орфографию.

ЛИЦО ПОВЕСТВОВАНИЯ: пиши строго от ПЕРВОГО лица единственного числа ("проверил", "сделал", "разобрался"). НЕ пиши от третьего лица. Отглагольное существительное переформулируй в первое лицо или оставь как у сотрудника — смотря как правильнее в контексте.

РОД ГЛАГОЛОВ ПО ПОЛУ СОТРУДНИКА: в начале запроса будет указано имя и фамилия сотрудника. Определи пол по имени и фамилии и склоняй глаголы прошедшего времени в правильном роде. Для мужчины: "сделал", "проверил", "разобрался". Для женщины: "сделала", "проверила", "разобралась". Например, для "Курочкина Дарья" — женский род ("выполнила", "завершила"), для "Селиванов Артемий" — мужской ("выполнил", "завершил").

ЗАПРЕЩЕНО ИСПОЛЬЗОВАТЬ MARKDOWN. НЕ используй звёздочки (*), решётки (#), подчёркивания (_), обратные кавычки. Только чистый текст и символ • для пунктов.

ПРАВИЛА РЕГИСТРА (ОЧЕНЬ ВАЖНО, соблюдай везде — и в заголовках, и в пунктах):
- НЕ пиши заголовки капсом (заглавными целиком). Пиши обычным регистром: первое слово с большой буквы, остальное строчными. Например "Работа над проектом", "Трудности", "Взаимодействие".
- Аббревиатуры всегда заглавными буквами: ЖК, ГК, ТРЦ, ТГК, БЦ, ЖД, IFC, BIM, XML, ТЭП и подобные. Никогда не пиши их строчными.
- Названия проектов пиши с большой буквы: "ЖК Нагатинский", "ТРЦ Джалиля", "ТГК Солнцево", "Школа ВОГ". Аббревиатура в названии заглавными, само название с большой буквы.

ГЛАВНОЕ ПРАВИЛО СТРУКТУРЫ — РАЗБИВАЙ НА ОТДЕЛЬНЫЕ РАЗДЕЛЫ:

1. Для КАЖДОГО проекта — ОТДЕЛЬНЫЙ раздел. Заголовок раздела: "Работа над [Название проекта]" (обычный регистр, аббревиатуры в названии заглавными). Под ним пункты с • по этому проекту. НЕ объединяй разные проекты в один раздел.

2. Если сотрудник написал про трудности — отдельный раздел с заголовком: "Трудности".

3. Если написал про что-то новое — отдельный раздел: "Что нового узнал".

4. Если написал про встречи, созвоны, помощь коллегам — отдельный раздел: "Взаимодействие".

Включай ТОЛЬКО те разделы, по которым сотрудник дал информацию.

ФОРМАТ (соблюдай переносы строк ТОЧНО так):
- Заголовок раздела на отдельной строке.
- Далее каждый пункт с новой строки, начинается с "• ".
- После последнего пункта раздела — ПУСТАЯ СТРОКА, затем следующий раздел.

Пример правильного вывода:

Работа над ЖК Нагатинский
• Выполнил зоны и ТЭП.
• Проверил расхождения площадей.

Работа над Школа ВОГ
• Завершил работу и передал файлы заказчику.

Трудности
• Возникла трудность с переустановкой Revit.

Взаимодействие
• Участвовал во встрече в телемосте в 10:00 с Дмитрием.

СТИЛЬ: деловой, по пунктам. Без эмодзи. Без вводных фраз. Сразу содержание.

ДЛИНА: объём итога прямо пропорционален объёму ответов сотрудника. Чем подробнее ответы — тем длиннее итог. Не раздувай искусственно."""


def build_user_prompt(answers: dict, user_name: str = "") -> str:
    parts = []
    if user_name:
        parts.append(f"Имя и фамилия сотрудника (для определения пола и рода глаголов): {user_name}")
    for key, question, _heading in QUESTIONS:
        ans = answers.get(key, "").strip() or "—"
        parts.append(f"Вопрос: {question}\nОтвет: {ans}")
    return "\n\n".join(parts)


# ============================================================
# СОСТОЯНИЯ FSM
# ============================================================

class Flow(StatesGroup):
    registering = State()   # выбирает фамилию
    answering = State()     # отвечает на вопросы
    confirming = State()    # подтверждает отправку в WEEEK


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_REPORT)],
            [KeyboardButton(text=BTN_CHANGE_USER)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выбери действие",
    )


def employees_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора фамилии при регистрации."""
    rows = []
    for name in EMPLOYEES.keys():
        rows.append([InlineKeyboardButton(text=name, callback_data=f"reg:{name}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Кнопка подтверждения отправки в WEEEK."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить в WEEEK", callback_data="send_weeek")],
        [InlineKeyboardButton(text="Не отправлять", callback_data="cancel_send")],
    ])


# ============================================================
# ИНИЦИАЛИЗАЦИЯ
# ============================================================

dp = Dispatcher(storage=MemoryStorage())


# ============================================================
# ВСПОМОГАТЕЛЬНОЕ
# ============================================================

def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_markdown(text: str) -> str:
    """Убирает markdown и нормализует переносы.
    Заголовок раздела = строка без ведущего •; пункт = строка с •.
    Перед каждым заголовком (кроме первого) ставит пустую строку."""
    import re
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = text.replace("`", "")
    # markdown-маркеры списков (* или - в начале строки) -> bullet
    text = re.sub(r"^\s*[\*\-]\s+", "• ", text, flags=re.MULTILINE)

    lines = [l.rstrip() for l in text.split("\n")]
    fixed = []
    seen_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == "":
            continue
        is_header = not stripped.startswith("•")
        if is_header:
            if seen_section and fixed and fixed[-1].strip() != "":
                fixed.append("")  # пустая строка перед новым заголовком
            seen_section = True
        fixed.append(stripped)
    text = "\n".join(fixed)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def generate_ai_report(answers: dict, user_name: str = ""):
    if not DEEPSEEK_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(answers, user_name)},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(DEEPSEEK_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"DeepSeek статус {resp.status}: {(await resp.text())[:300]}")
                    return None
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                return clean_markdown(content)
    except Exception as e:
        logger.error(f"DeepSeek ошибка: {e}")
        return None


def build_simple_report(answers: dict) -> str:
    lines = []
    for key, _q, heading in QUESTIONS:
        ans = answers.get(key, "").strip() or "—"
        lines.append(f"{heading}:")
        lines.append(ans)
        lines.append("")
    return "\n".join(lines).strip()


HELP_TEXT = (
    "Я помогаю собрать итог дня и отправить его в WEEEK.\n\n"
    "«Создать итог дня» — задам 5 коротких вопросов о твоей работе за день, "
    "оформлю ответы с помощью ИИ в аккуратный структурированный отчёт "
    "(ничего не выдумываю — только привожу твои ответы в порядок), "
    "покажу итог и спрошу, отправить ли его в WEEEK в твою колонку.\n\n"
    "«Сменить пользователя» — заново выбрать свою фамилию.\n\n"
    "Команды: /report — начать, /cancel — прервать."
)


# ============================================================
# РЕГИСТРАЦИЯ
# ============================================================

async def ask_registration(message: Message, state: FSMContext):
    await state.set_state(Flow.registering)
    await message.answer(
        "Выбери свою фамилию и имя из списка:",
        reply_markup=employees_keyboard(),
    )


@dp.callback_query(F.data.startswith("reg:"))
async def on_register(call: CallbackQuery, state: FSMContext):
    name = call.data.split("reg:", 1)[1]
    column_id = EMPLOYEES.get(name)
    if column_id is None:
        await call.answer("Не нашёл такого сотрудника", show_alert=True)
        return
    ok = await storage.set_user(call.from_user.id, name, column_id)
    await call.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    if ok:
        await call.message.answer(
            f"Готово, {name}! Ты зарегистрирован.\n\nТеперь можешь создавать итоги дня.",
            reply_markup=main_menu(),
        )
    else:
        await call.message.answer(
            f"Выбрал: {name}. (Внимание: не удалось сохранить в постоянное хранилище — "
            "регистрация может слететь после перезапуска. Проверь настройки GitHub.)",
            reply_markup=main_menu(),
        )
    await call.answer()


# ============================================================
# КОМАНДЫ И МЕНЮ
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await storage.get_user(message.from_user.id)
    if user:
        await message.answer(
            f"С возвращением, {user['name']}!\nВыбери действие.",
            reply_markup=main_menu(),
        )
    else:
        await message.answer("Привет! Сначала зарегистрируйся.")
        await ask_registration(message, state)


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменил.", reply_markup=main_menu())


@dp.message(F.text == BTN_CHANGE_USER)
async def btn_change_user(message: Message, state: FSMContext):
    await state.clear()
    await ask_registration(message, state)


@dp.message(F.text == BTN_HELP)
async def btn_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=main_menu())


@dp.message(Command("report"))
@dp.message(F.text == BTN_REPORT)
async def cmd_report(message: Message, state: FSMContext):
    user = await storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся — выбери свою фамилию.")
        await ask_registration(message, state)
        return
    await state.clear()
    await state.set_state(Flow.answering)
    await state.update_data(answers={}, q_index=0)
    await message.answer(f"Вопрос 1 из {len(QUESTIONS)}\n\n{QUESTIONS[0][1]}")


# ============================================================
# СБОР ОТВЕТОВ
# ============================================================

@dp.message(Flow.answering, F.text)
async def process_answer(message: Message, state: FSMContext):
    if message.text in (BTN_REPORT, BTN_CHANGE_USER, BTN_HELP):
        # нажали меню во время опроса — выходим из опроса
        await state.clear()
        if message.text == BTN_CHANGE_USER:
            await ask_registration(message, state)
        elif message.text == BTN_HELP:
            await message.answer(HELP_TEXT, reply_markup=main_menu())
        else:
            await cmd_report(message, state)
        return

    data = await state.get_data()
    answers = data.get("answers", {})
    q_index = data.get("q_index", 0)

    answers[QUESTIONS[q_index][0]] = message.text.strip()
    q_index += 1

    if q_index < len(QUESTIONS):
        await state.update_data(answers=answers, q_index=q_index)
        await message.answer(f"Вопрос {q_index + 1} из {len(QUESTIONS)}\n\n{QUESTIONS[q_index][1]}")
        return

    # все вопросы пройдены
    user = await storage.get_user(message.from_user.id)
    user_name = user["name"] if user else ""

    wait_msg = await message.answer("Формирую итог дня, секунду...")
    ai_report = await generate_ai_report(answers, user_name)
    if not ai_report:
        ai_report = build_simple_report(answers)

    try:
        await wait_msg.delete()
    except Exception:
        pass

    today = datetime.now().strftime("%d.%m.%Y")
    header = f"Итог дня — {today}\nСотрудник: {user['name']}"
    full = f"{header}\n\n{ai_report}"

    # сохраняем готовый итог в state для последующей отправки
    await state.update_data(final_report=ai_report, report_title=f"Итоги дня {today}")
    await state.set_state(Flow.confirming)

    await message.answer(f"<pre>{escape_html(full)}</pre>")
    await message.answer(
        "Проверь итог. Отправить его в WEEEK в твою колонку?",
        reply_markup=confirm_keyboard(),
    )


@dp.message(Flow.answering)
async def answering_non_text(message: Message):
    await message.answer("Пожалуйста, ответь текстом.")


# ============================================================
# ПОДТВЕРЖДЕНИЕ ОТПРАВКИ В WEEEK
# ============================================================

@dp.callback_query(F.data == "send_weeek", Flow.confirming)
async def on_send_weeek(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    report = data.get("final_report", "")
    title = data.get("report_title", "Итоги дня")
    user = await storage.get_user(call.from_user.id)

    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Отправляю...")

    if not user:
        await call.message.answer("Не нашёл твою регистрацию. Зарегистрируйся заново.")
        await state.clear()
        return

    ok, msg = await weeek_client.create_task(title, report, user["column_id"])
    await state.clear()
    if ok:
        await call.message.answer(
            f"Готово! Итог отправлен в WEEEK в колонку «{user['name']}».",
            reply_markup=main_menu(),
        )
    else:
        await call.message.answer(
            f"Не удалось отправить в WEEEK: {msg}\n\nИтог выше можешь скопировать вручную.",
            reply_markup=main_menu(),
        )


@dp.callback_query(F.data == "cancel_send", Flow.confirming)
async def on_cancel_send(call: CallbackQuery, state: FSMContext):
    await call.message.edit_reply_markup(reply_markup=None)
    await state.clear()
    await call.answer()
    await call.message.answer(
        "Не отправил. Итог выше можешь скопировать вручную.",
        reply_markup=main_menu(),
    )


# ============================================================
# FALLBACK
# ============================================================

@dp.message()
async def fallback(message: Message, state: FSMContext):
    user = await storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала зарегистрируйся.")
        await ask_registration(message, state)
        return
    await message.answer("Выбери действие на кнопках.", reply_markup=main_menu())


# ============================================================
# KEEP-ALIVE (Render)
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
        raise RuntimeError("BOT_TOKEN не задан.")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await start_web_server()
    logger.info("Бот запущен (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
