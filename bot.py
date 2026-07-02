# -*- coding: utf-8 -*-
"""
Бот итогов дня — Фаза 3 (регистрация + AI + WEEEK)
Стек: Python + aiogram 3.x, хранилище GitHub-backed JSON, хостинг Render.

Флоу:
1. Первый вход -> регистрация: выбор своей фамилии кнопкой. Запоминается в GitHub.
2. "Создать итог дня" -> 5 вопросов -> OpenAI (gpt-4o-mini) оформляет итог (от 1 лица, без выдумок).
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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
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

# Пароль доступа к боту (вводится один раз после регистрации)
ACCESS_PASSWORD = "BIM_ENVELOP"

# Ссылка на доску итогов в WEEEK (пока тестовая, потом заменить на реальную)
WEEEK_BOARD_URL = "https://app.weeek.net/ws/1013470/project/1/board/1"

# ============================================================
# КНОПКИ МЕНЮ
# ============================================================

BTN_REPORT = "✏️ Создать итог дня"
BTN_CHANGE_USER = "👤 Сменить пользователя"
BTN_HELP = "Помощь"

# ============================================================
# ВОПРОСЫ
# ============================================================

QUESTIONS = [
    ("projects", "Над какими проектами сегодня работал(а)?\n\nПеречисли все проекты через запятую или списком.", "Проекты"),
    ("done", "Расскажи подробно, что конкретно делал(а) по каждому проекту?\n\nОпиши конкретные задачи и что удалось сделать — чем детальнее, тем полезнее итог.", "Что делал и сделал"),
    ("problems", "Были ли трудности или проблемы? Как их удалось решить?\n\nОпиши с чем столкнулся(лась) и что предпринял(а). (если трудностей не было — поставь прочерк)", "Трудности и замечания"),
    ("learned", "Узнал(а) или попробовал(а) что-то новое сегодня?\n\nНовый инструмент, приём, подход, что-то из обучения. (если нет — поставь прочерк)", "Что нового узнал"),
    ("misc", "Координация, встречи, другие взаимодействия с командой?\n\nСозвоны, помощь коллегам, обсуждения, планёрки. (если нет — поставь прочерк)", "Координация и прочее"),
]

# вопросы, для которых требуется развёрнутый ответ (доп-вопрос при коротком)
DETAIL_REQUIRED = {"done"}
MIN_ANSWER_LENGTH = 100  # порог символов
FOLLOWUP_TEXT = "Может, добавишь ещё пару деталей? Уточни, что именно делал по каждому проекту, каким инструментом, какой результат?"

# ============================================================
# ПРОМТ ДЛЯ OPENAI
# ============================================================

SYSTEM_PROMPT = """Ты составляешь итог рабочего дня сотрудника BIM-компании на основе его ответов на вопросы. Итог должен читаться как связный и осмысленный рабочий отчёт, а не как короткий пересказ. 

СТРУКТУРА ПО СМЫСЛУ:
- Для каждого проекта, над которым работал сотрудник, создавай отдельный раздел. Заголовок проектного раздела всегда в формате "Работа над проектом [Название]", название в именительном падеже, не склоняется.
- Помимо проектных, создавай осмысленные разделы там, где информация не привязана к одному проекту (например, проверка моделей по нескольким проектам, взаимодействие с командой, автоматизация, польза команде). Придумывай наиболее подходящие названия разделов под каждый итог ндивидуально
- Раскладывай каждый факт в подходящий по смыслу раздел, независимо от того, на какой вопрос он был дан.
- Создавай раздел только если для него есть содержательная рабочая информация. Не делай разделов ради пустоты, прочерков или проходных бытовых реплик.

ЧТО ВКЛЮЧАТЬ:
- Включай всю переданную сотрудником информацию: что делал по проектам, встречи, проверки, взаимодействие, трудности, новое в работе.
- Проходные, шуточные или бытовые реплики, сказанные вскользь, можно опускать, если они не несут хоть какой-то пользы для отчёта. Если такая реплика уместна — можно упомянуть её лёгкой короткой фразой, но не выделяй в отдельный раздел и не раздувай этот текст.
- Осмысляй ситуацию и излагай связно. Разговорные формулировки переводи в нейтральный или деловой тон, сохраняя смысл. 
- Не искажай и не выдумывай факты: пиши только то, что реально сообщил сотрудник. НЕ добавляй оценочную "воду" и выводы о пользе, которых сотрудник не говорил — например фразы вроде "что позволило повысить качество", "способствовало выявлению ошибок", "что облегчило работу", "потребовало концентрации и дисциплины".

ОБЪЁМ: делай итог свободном формате и содержательным, но не перебарщивай с водой

ЛИЦО И РОД: пиши от первого лица единственного числа. Пиши текст в прошедшем времени, даже ели сотрудник пишет, например, что провел аботу над чем-то, ты должен отформатировать это как "проведена работа над чем-то". Род глаголов, если таковые используются -  определяй СТРОГО по имени и фамилии сотрудника (указаны в начале запроса), а не по тому, в каком роде написаны ответы. Для женского имени — женский род всегда, для мужского — мужской.

НАЗВАНИЯ И АББРЕВИАТУРЫ:
- Названия проектов пиши с большой буквы в каждом значимом слове, В ТОМ ЧИСЛЕ В ЗАГОЛОВКАХ. Правильно: "Работа над проектом ПИК Саларьево", "Работа над проектом Производственный Кластер", "Работа над проектом Школа Бухвостова", "Работа над проектом ЖК Нагатинский". Каждое значимое слово названия — с большой буквы, даже если это обычные слова (Производственный Кластер, а не производственный кластер).
- Различай проекты и инструменты. Инструменты и программы (ЦИМ Чек, IFC-checker, Строим Просто, Revit, AutoCAD, Blender и т.п.) — не проекты, по ним не создавай проектных разделов. Проекты — это объекты строительства. Проекты перечисляет пользователь отвечая на 1-й вопрос.
- Аббревиатуры и названия ПО приводи к общепринятому правильному написанию независимо от того, как их набрал сотрудник (например: ЦИМ, IFC, BIM, DWG, 1by1, ТРЦ, ЖК, Revit, AutoCAD, Строим Просто). Технические аббревиатуры — заглавными, софт и бренды — со своей капитализацией.

ФОРМАТ:
- Не используй markdown: без звёздочек, решёток, подчёркиваний.
- Если сотрудник не укзал в ответе для чего сделал определенное действие и к чему оно привело, например "проверил модель" - не нужно придумывать за него - для чего он сделал это и что это позволило улучшить.
- Заголовок раздела — отдельной строкой, без символа • и без номеров. Первое слово заголовка с ЗАГЛАВНОЙ буквы (например "Работа над проектом...", "Проверка моделей", "Взаимодействие").
- Пункты — с новой строки, начинаются с "• ".
- Между разделами — одна пустая строка.
- Каждый пункт понятен сам по себе, без отсылок на другие пункты.

Стиль без эмодзи и вводных фраз. Составь итог на основе ответов сотрудника ниже."""


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
    entering_password = State()  # вводит пароль после регистрации
    answering = State()     # отвечает на вопросы
    confirming = State()    # подтверждает отправку в WEEEK
    editing = State()       # присылает исправленный текст итога


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


def board_link_keyboard() -> InlineKeyboardMarkup:
    """Inline-кнопка перехода на доску итогов WEEEK."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Ссылка", url=WEEEK_BOARD_URL)],
    ])


def confirm_keyboard() -> InlineKeyboardMarkup:
    """Кнопки подтверждения отправки в WEEEK."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить в WEEEK", callback_data="send_weeek")],
        [InlineKeyboardButton(text="Редактировать", callback_data="edit_report")],
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
    if not OPENAI_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(answers, user_name)},
        ],
        "temperature": 0.2,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(OPENAI_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"OpenAI статус {resp.status}: {(await resp.text())[:300]}")
                    return None
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                return clean_markdown(content)
    except Exception as e:
        logger.error(f"OpenAI ошибка: {e}")
        return None


def strip_report_header(text: str) -> str:
    """Убирает служебные строки шапки (дата, ФИО) из текста итога.
    Нужно при редактировании: пользователь копирует итог вместе с шапкой,
    а в WEEEK шапка не нужна (там своя дата в названии и своя колонка)."""
    import re
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        s = line.strip()
        # строка вида "Итог дня — ДД.ММ.ГГГГ" (с любым тире)
        if re.match(r"^Итог дня\s*[—\-–]", s):
            continue
        # строка вида "Сотрудник: ..."
        if s.startswith("Сотрудник:"):
            continue
        cleaned.append(line)
    # убираем ведущие пустые строки, которые могли остаться после удаления шапки
    result = "\n".join(cleaned).strip()
    return result


REFORMAT_PROMPT = """Ты форматируешь текст рабочего итога дня, который сотрудник отредактировал вручную.

КРИТИЧЕСКИ ВАЖНО: НЕ МЕНЯЙ содержание, слова и формулировки сотрудника. Твоя задача — ТОЛЬКО привести текст к правильной структуре форматирования. Запрещено переписывать, сокращать, дополнять или менять смысл. Сохрани все факты и формулировки как есть.

ЧТО НУЖНО СДЕЛАТЬ — привести к структуре:
- Заголовки разделов (например "Работа над [Проект]", "Трудности", "Что нового узнал", "Взаимодействие") — на отдельной строке, обычным регистром, БЕЗ символа • в начале.
- Пункты под заголовком — каждый с новой строки, начинается с "• " (символ кружка и пробел).
- Между разделами — одна пустая строка.

ЗАПРЕЩЕНО ИСПОЛЬЗОВАТЬ MARKDOWN: не используй звёздочки (*), решётки (#), подчёркивания (_). Только чистый текст и символ • для пунктов.

УБЕРИ СЛУЖЕБНЫЕ СТРОКИ: если в присланном тексте есть строки с датой (например "Итог дня — 01.07.2026") и с именем сотрудника (например "Сотрудник: Селиванов Артемий") — УДАЛИ их полностью. Они не нужны в результате. Начинай сразу с первого раздела (заголовка проекта или раздела).

ПРАВИЛА РЕГИСТРА (соблюдай):
- Заголовки не капсом, обычным регистром.
- Аббревиатуры (ЖК, ГК, ТРЦ, ТГК, БЦ, IFC, BIM, XML, ТЭП) — заглавными.
- Названия проектов с большой буквы.

Просто верни тот же текст с правильной структурой (заголовки отдельно, пункты с •). Ничего не добавляй от себя, только переформатируй."""


async def reformat_report(text: str):
    """Переразмечает отредактированный пользователем текст в правильную структуру,
    НЕ меняя содержания. Возвращает переформатированный текст или None при ошибке."""
    if not OPENAI_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": REFORMAT_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(OPENAI_URL, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    logger.error(f"OpenAI reformat статус {resp.status}: {(await resp.text())[:300]}")
                    return None
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                return clean_markdown(content)
    except Exception as e:
        logger.error(f"OpenAI reformat ошибка: {e}")
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
    # удаляем сообщение "Выбери свою фамилию и имя из списка" целиком
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer()

    if not ok:
        await state.clear()
        await call.message.answer(
            f"Выбран: {name}. (Внимание: не удалось сохранить в постоянное хранилище — "
            "регистрация может слететь после перезапуска. Проверь настройки GitHub.)",
            reply_markup=main_menu(),
        )
        return

    # проверяем доступ по паролю
    if await storage.has_access(call.from_user.id):
        await state.clear()
        await call.message.answer(
            f"Готово, {name}! Ты зарегистрирован(а).\n\nТеперь можешь создавать итоги дня.",
            reply_markup=main_menu(),
        )
    else:
        # доступа ещё нет — просим пароль
        await state.set_state(Flow.entering_password)
        await call.message.answer(
            f"Готово, {name}! Ты зарегистрирован(а).\n\n"
            "Для доступа к боту введи пароль:"
        )


@dp.message(Flow.entering_password, F.text)
async def on_password(message: Message, state: FSMContext):
    entered = message.text.strip()
    # если нажали кнопку меню во время ввода пароля — обрабатываем корректно
    if entered == BTN_REPORT:
        # доступ ещё не открыт — напоминаем про пароль, не считая это попыткой ввода
        await message.answer("Сначала введи пароль для доступа к боту:")
        return
    if entered == BTN_CHANGE_USER:
        await state.clear()
        await ask_registration(message, state)
        return
    if entered == BTN_HELP:
        await message.answer(HELP_TEXT)
        return
    if entered == ACCESS_PASSWORD:
        await storage.grant_access(message.from_user.id)
        await state.clear()
        await message.answer(
            "Пароль верный, доступ открыт!\n\nТеперь можешь создавать итоги дня.",
            reply_markup=main_menu(),
        )
    else:
        await message.answer("Неверный пароль. Попробуй ещё раз:")


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
    # проверка доступа по паролю
    if not await storage.has_access(message.from_user.id):
        await state.set_state(Flow.entering_password)
        await message.answer("Для доступа к боту сначала введи пароль:")
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
    awaiting_followup = data.get("awaiting_followup", False)

    current_key = QUESTIONS[q_index][0]

    if awaiting_followup:
        # это ответ на уточняющий вопрос — дописываем к уже данному ответу
        prev = answers.get(current_key, "")
        answers[current_key] = (prev + " " + message.text.strip()).strip()
        await state.update_data(answers=answers, awaiting_followup=False)
        q_index += 1
    else:
        # обычный ответ на основной вопрос
        answer_text = message.text.strip()
        answers[current_key] = answer_text
        # если вопрос требует развёрнутости и ответ короткий — задаём доп-вопрос
        if current_key in DETAIL_REQUIRED and len(answer_text) < MIN_ANSWER_LENGTH:
            await state.update_data(answers=answers, awaiting_followup=True)
            await message.answer(FOLLOWUP_TEXT)
            return
        q_index += 1

    if q_index < len(QUESTIONS):
        await state.update_data(answers=answers, q_index=q_index, awaiting_followup=False)
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

    # удаляем сообщение "Проверь итог. Отправить?" целиком
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.answer("Отправляю...")

    if not user:
        await call.message.answer("Не нашёл твою регистрацию. Зарегистрируйся заново.")
        await state.clear()
        return

    ok, msg = await weeek_client.create_task(title, report, user["column_id"])
    await state.clear()
    if ok:
        # отмечаем что сотрудник сдал итог сегодня — чтобы не слать ему напоминание
        today_str = datetime.now().strftime("%d.%m.%Y")
        try:
            await storage.mark_report_sent(call.from_user.id, today_str)
        except Exception as e:
            logger.warning(f"Не удалось отметить дату сдачи: {e}")
        await call.message.answer(
            f"Готово! Итог отправлен в WEEEK в колонку «{user['name']}».",
            reply_markup=main_menu(),
        )
        await call.message.answer(
            "Держи ссылку для перехода в WEEEK",
            reply_markup=board_link_keyboard(),
        )
    else:
        await call.message.answer(
            f"Не удалось отправить в WEEEK: {msg}\n\nИтог выше можешь скопировать вручную.",
            reply_markup=main_menu(),
        )


@dp.callback_query(F.data == "cancel_send", Flow.confirming)
async def on_cancel_send(call: CallbackQuery, state: FSMContext):
    try:
        await call.message.delete()
    except Exception:
        pass
    await state.clear()
    await call.answer()
    await call.message.answer(
        "Не отправил. Итог выше можешь скопировать вручную.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "edit_report", Flow.confirming)
async def on_edit_report(call: CallbackQuery, state: FSMContext):
    """Пользователь хочет отредактировать итог вручную."""
    try:
        await call.message.delete()
    except Exception:
        pass
    await state.set_state(Flow.editing)
    await call.answer()
    edit_prompt_msg = await call.message.answer(
        "Скопируй итог выше, поправь текст как нужно и пришли мне обратно "
        "одним сообщением.\n\n"
        "Не переживай о форматировании — я сам приведу заголовки и пункты "
        "в порядок. Просто меняй содержание."
    )
    # запоминаем id этого сообщения, чтобы удалить после присланного текста
    await state.update_data(edit_prompt_msg_id=edit_prompt_msg.message_id)


async def _finalize_and_send(message: Message, state: FSMContext, report_text: str):
    """Общий финал: отправка готового текста итога в WEEEK."""
    user = await storage.get_user(message.from_user.id)
    if not user:
        await message.answer("Не нашёл твою регистрацию. Зарегистрируйся заново.",
                             reply_markup=main_menu())
        await state.clear()
        return
    today = datetime.now().strftime("%d.%m.%Y")
    title = f"Итоги дня {today}"
    ok, msg = await weeek_client.create_task(title, report_text, user["column_id"])
    await state.clear()
    if ok:
        try:
            await storage.mark_report_sent(message.from_user.id, today)
        except Exception as e:
            logger.warning(f"Не удалось отметить дату сдачи: {e}")
        await message.answer(
            f"Готово! Итог отправлен в WEEEK в колонку «{user['name']}».",
            reply_markup=main_menu(),
        )
        await message.answer(
            "Держи ссылку для перехода в WEEEK",
            reply_markup=board_link_keyboard(),
        )
    else:
        await message.answer(
            f"Не удалось отправить в WEEEK: {msg}\n\nИтог можешь скопировать вручную.",
            reply_markup=main_menu(),
        )


@dp.message(Flow.editing, F.text)
async def on_edited_text(message: Message, state: FSMContext):
    """Принимает отредактированный текст, переразмечает через OpenAI и шлёт в WEEEK."""
    # если нажали кнопку меню во время редактирования
    if message.text in (BTN_REPORT, BTN_CHANGE_USER, BTN_HELP):
        await state.clear()
        if message.text == BTN_CHANGE_USER:
            await ask_registration(message, state)
        elif message.text == BTN_HELP:
            await message.answer(HELP_TEXT, reply_markup=main_menu())
        else:
            await cmd_report(message, state)
        return

    edited = message.text.strip()

    # удаляем сообщение-инструкцию "Скопируй итог, поправь..."
    data = await state.get_data()
    edit_prompt_id = data.get("edit_prompt_msg_id")
    if edit_prompt_id:
        try:
            await message.bot.delete_message(message.chat.id, edit_prompt_id)
        except Exception:
            pass

    # убираем шапку (дата, ФИО) до переразметки
    edited = strip_report_header(edited)
    wait_msg = await message.answer("Форматирую и отправляю, секунду...")

    # переразметка через OpenAI (не меняя содержания)
    formatted = await reformat_report(edited)
    if not formatted:
        # если переразметка не удалась — берём текст как есть (clean_markdown уберёт мусор)
        formatted = clean_markdown(edited)

    # подстраховка: если модель всё же вернула шапку — вырезаем ещё раз
    formatted = strip_report_header(formatted)

    try:
        await wait_msg.delete()
    except Exception:
        pass

    await _finalize_and_send(message, state, formatted)


@dp.message(Flow.editing)
async def editing_non_text(message: Message):
    await message.answer("Пришли исправленный текст сообщением.")


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
# НАПОМИНАНИЕ В 18:30 МСК
# ============================================================

REMINDER_TEXT = (
    "Напоминание: не забудь оформить итог дня!\n\n"
    "Нажми «Создать итог дня», это займёт пару минут."
)

# глобальная ссылка на бота для рассылки из планировщика
_bot_for_reminders = None


async def send_daily_reminders():
    """Рассылает напоминание сотрудникам, которые ещё НЕ сдали итог сегодня."""
    global _bot_for_reminders
    if _bot_for_reminders is None:
        return
    try:
        users = await storage.load_all()
    except Exception as e:
        logger.error(f"Не удалось загрузить пользователей для напоминания: {e}")
        return

    today_str = datetime.now().strftime("%d.%m.%Y")
    sent = 0
    skipped = 0
    for tg_id, info in users.items():
        # пропускаем тех, кто уже сдал итог сегодня
        if isinstance(info, dict) and info.get("last_report_date") == today_str:
            skipped += 1
            continue
        try:
            await _bot_for_reminders.send_message(
                int(tg_id), REMINDER_TEXT, reply_markup=main_menu()
            )
            sent += 1
        except Exception as e:
            # пользователь мог заблокировать бота — пропускаем
            logger.warning(f"Не удалось отправить напоминание {tg_id}: {e}")
    logger.info(
        f"Напоминания: отправлено {sent}, пропущено (уже сдали) {skipped}, всего {len(users)}"
    )


def setup_scheduler(bot):
    """Запускает планировщик напоминаний на 18:30 по будням (МСК)."""
    global _bot_for_reminders
    _bot_for_reminders = bot
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    # по будням (пн-пт) в 18:30 МСК
    scheduler.add_job(
        send_daily_reminders,
        CronTrigger(day_of_week="mon-fri", hour=18, minute=30, timezone="Europe/Moscow"),
        id="daily_reminder",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Планировщик напоминаний запущен (18:30 МСК, пн-пт)")


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
    setup_scheduler(bot)
    logger.info("Бот запущен (polling)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
