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


async def _get_file():
    """Возвращает (содержимое_dict, sha) файла из GitHub. Если нет — ({}, None)."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.warning("GitHub не настроен — храню только в памяти")
        return {}, None

    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}?ref={GITHUB_BRANCH}"
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


async def _save_file(content_dict, sha):
    """Записывает файл в GitHub. При конфликте SHA (409) перечитывает
    актуальный SHA и повторяет запись один раз."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    raw = json.dumps(content_dict, ensure_ascii=False, indent=2)
    body = {
        "message": "Обновление регистраций бота",
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
                    _, fresh_sha = await _get_file()
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
