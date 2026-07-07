# -*- coding: utf-8 -*-
"""
Хранилище привязок "Telegram-пользователь -> сотрудник (колонка WEEEK)".
Данные хранятся в JSON-файле в GitHub-репозитории через GitHub API,
чтобы переживать перезапуски Render.

Формат файла registrations.json:
{
    "123456789": {"name": "Селиванов Артемий", "column_id": 4},
    ...
}
"""

import base64
import json
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

# Настройки GitHub (задаются в переменных окружения Render)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")        # вид: "username/repo-name"
GITHUB_FILE = os.getenv("GITHUB_FILE", "registrations.json")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

GITHUB_API = "https://api.github.com"


async def _get_file(filename=None):
    """Возвращает (содержимое_dict, sha) файла из GitHub. Если нет — ({}, None)."""
    fname = filename or GITHUB_FILE
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning("GitHub не настроен — храню только в памяти")
        return {}, None

    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{fname}?ref={GITHUB_BRANCH}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 404:
                    return {}, None
                if resp.status != 200:
                    logger.error(f"GitHub get файла статус {resp.status}")
                    return {}, None
                data = await resp.json()
                content_b64 = data.get("content", "")
                sha = data.get("sha")
                raw = base64.b64decode(content_b64).decode("utf-8")
                return json.loads(raw) if raw.strip() else {}, sha
    except Exception as e:
        logger.error(f"Ошибка чтения GitHub: {e}")
        return {}, None


async def _save_file(content_dict, sha, filename=None):
    """Записывает файл в GitHub. При конфликте SHA (409) перечитывает
    актуальный SHA и повторяет запись один раз."""
    fname = filename or GITHUB_FILE
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{fname}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    raw = json.dumps(content_dict, ensure_ascii=False, indent=2)
    body = {
        "message": f"Обновление {fname}",
        "content": base64.b64encode(raw.encode("utf-8")).decode("utf-8"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=body) as resp:
                if resp.status in (200, 201):
                    return True
                if resp.status == 409:
                    # SHA устарел (файл менял кто-то ещё) — перечитываем и повторяем
                    logger.warning("GitHub 409: SHA устарел, перечитываю и повторяю запись")
                    _, fresh_sha = await _get_file(fname)
                    if fresh_sha:
                        body["sha"] = fresh_sha
                    else:
                        body.pop("sha", None)
                    async with session.put(url, headers=headers, json=body) as resp2:
                        if resp2.status in (200, 201):
                            return True
                        logger.error(f"GitHub save повтор статус {resp2.status}: {await resp2.text()}")
                        return False
                logger.error(f"GitHub save статус {resp.status}: {await resp.text()}")
                return False
    except Exception as e:
        logger.error(f"Ошибка записи GitHub: {e}")
        return False


# Кэш в памяти, чтобы не дёргать GitHub на каждое сообщение
_cache = None
_sha = None


async def load_all():
    """Загружает все регистрации (с кэшированием)."""
    global _cache, _sha
    if _cache is None:
        _cache, _sha = await _get_file()
    return _cache


async def get_user(tg_id: int):
    """Возвращает {'name':..., 'column_id':...} или None."""
    data = await load_all()
    return data.get(str(tg_id))


async def set_user(tg_id: int, name: str, column_id: int):
    """Сохраняет привязку пользователя. Сохраняет дату сдачи и флаг доступа, если были."""
    global _cache, _sha
    data = await load_all()
    key = str(tg_id)
    existing = data.get(key, {})
    record = {"name": name, "column_id": column_id}
    # не теряем отметку о последней сдаче итога при смене профиля
    if "last_report_date" in existing:
        record["last_report_date"] = existing["last_report_date"]
    # не теряем флаг доступа по паролю
    if "access_granted" in existing:
        record["access_granted"] = existing["access_granted"]
    data[key] = record
    _cache = data
    ok = await _save_file(data, _sha)
    if ok:
        _, _sha = await _get_file()
    return ok


async def mark_report_sent(tg_id: int, date_str: str):
    """Отмечает, что пользователь сдал итог в указанную дату (формат ДД.ММ.ГГГГ)."""
    global _cache, _sha
    data = await load_all()
    key = str(tg_id)
    if key not in data:
        return False  # незарегистрированный — нечего отмечать
    data[key]["last_report_date"] = date_str
    _cache = data
    ok = await _save_file(data, _sha)
    if ok:
        _, _sha = await _get_file()
    return ok


async def has_access(tg_id: int) -> bool:
    """Проверяет, вводил ли пользователь верный пароль ранее."""
    data = await load_all()
    rec = data.get(str(tg_id))
    return bool(rec and rec.get("access_granted"))


async def grant_access(tg_id: int) -> bool:
    """Отмечает, что пользователь ввёл верный пароль (доступ навсегда)."""
    global _cache, _sha
    data = await load_all()
    key = str(tg_id)
    existing = data.get(key, {})
    existing["access_granted"] = True
    data[key] = existing
    _cache = data
    ok = await _save_file(data, _sha)
    if ok:
        _, _sha = await _get_file()
    return ok


async def notifications_enabled(tg_id: int) -> bool:
    """Включены ли напоминания у пользователя. По умолчанию True."""
    data = await load_all()
    rec = data.get(str(tg_id))
    if not rec:
        return True
    # хранится флаг отключения; если его нет — напоминания включены
    return not rec.get("notifications_off", False)


async def toggle_notifications(tg_id: int) -> bool:
    """Переключает напоминания. Возвращает новое состояние (True=включены)."""
    global _cache, _sha
    data = await load_all()
    key = str(tg_id)
    existing = data.get(key, {})
    currently_off = existing.get("notifications_off", False)
    existing["notifications_off"] = not currently_off  # инвертируем
    data[key] = existing
    _cache = data
    ok = await _save_file(data, _sha)
    if ok:
        _, _sha = await _get_file()
    # новое состояние "включены" = НЕ выключены
    return not existing["notifications_off"]


# ============================================================
# ЗАМЕТКИ (notes.json) — накопление в течение дня
# ============================================================
# Структура notes.json:
# { "<tg_id>": {"date": "ДД.ММ.ГГГГ", "notes": ["текст1", "текст2", ...]} }

NOTES_FILE = os.getenv("GITHUB_NOTES_FILE", "notes.json")

_notes_cache = None
_notes_sha = None


async def _load_notes():
    """Загружает все заметки (с кэшем)."""
    global _notes_cache, _notes_sha
    if _notes_cache is None:
        _notes_cache, _notes_sha = await _get_file(NOTES_FILE)
    return _notes_cache


async def _save_notes(data):
    """Сохраняет заметки в GitHub, обновляет кэш и sha."""
    global _notes_cache, _notes_sha
    _notes_cache = data
    ok = await _save_file(data, _notes_sha, NOTES_FILE)
    if ok:
        _, _notes_sha = await _get_file(NOTES_FILE)
    return ok


async def add_note(tg_id: int, text: str, today: str):
    """Добавляет заметку пользователю за сегодня.
    Если в хранилище заметки за другую дату — заменяет их на новый день."""
    data = await _load_notes()
    key = str(tg_id)
    rec = data.get(key)
    if not rec or rec.get("date") != today:
        # новый день или первая заметка — начинаем список заново
        rec = {"date": today, "notes": []}
    rec["notes"].append(text.strip())
    data[key] = rec
    ok = await _save_notes(data)
    return ok, len(rec["notes"])


async def get_notes(tg_id: int, today: str):
    """Возвращает список заметок пользователя за сегодня (или [] если нет/другая дата)."""
    data = await _load_notes()
    rec = data.get(str(tg_id))
    if not rec or rec.get("date") != today:
        return []
    return rec.get("notes", [])


async def edit_note(tg_id: int, index: int, new_text: str, today: str):
    """Заменяет заметку с указанным индексом на новый текст.
    Возвращает True при успехе, False если заметки/индекса нет или другая дата."""
    data = await _load_notes()
    key = str(tg_id)
    rec = data.get(key)
    if not rec or rec.get("date") != today:
        return False
    notes = rec.get("notes", [])
    if index < 0 or index >= len(notes):
        return False
    notes[index] = new_text.strip()
    rec["notes"] = notes
    data[key] = rec
    return await _save_notes(data)


async def clear_all_notes():
    """Полностью очищает все заметки (вызывается в 8:00). Возвращает True при успехе."""
    global _notes_cache, _notes_sha
    _notes_cache = {}
    ok = await _save_file({}, _notes_sha, NOTES_FILE)
    if ok:
        _, _notes_sha = await _get_file(NOTES_FILE)
    return ok


async def get_all_notes_users():
    """Возвращает dict всех записей заметок {tg_id: {date, notes}} — для автопоказа в 9:00."""
    return await _load_notes()
