# -*- coding: utf-8 -*-
"""
Клиент для WEEEK API — создание задачи с итогом дня в колонке сотрудника.
"""

import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

WEEEK_API_KEY = os.getenv("WEEEK_API_KEY", "")
WEEEK_BASE = "https://api.weeek.net/public/v1"

# Фиксированные ID из тестовой доски
PROJECT_ID = int(os.getenv("WEEEK_PROJECT_ID", "1"))
BOARD_ID = int(os.getenv("WEEEK_BOARD_ID", "1"))


def _to_html(text: str) -> str:
    """Преобразует текст в HTML, понятный редактору WEEEK.
    Капс-заголовки -> <h3>, пункты с bullet -> элементы списка <ul><li>.
    Между разделами добавляется отступ непустым абзацем."""

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    lines = [l.strip() for l in text.split("\n")]
    html = []
    in_list = False
    first_section = True

    def close_list():
        nonlocal in_list
        if in_list:
            html.append("</ul>")
            in_list = False

    for s in lines:
        if s == "":
            continue
        letters = [c for c in s if c.isalpha()]
        is_caps_header = (
            len(letters) >= 2
            and all(c.isupper() for c in letters)
            and not s.startswith("•")
        )
        if is_caps_header:
            close_list()
            # отступ перед разделом (кроме первого) непустым абзацем
            if not first_section:
                html.append("<p>&nbsp;</p>")
            first_section = False
            html.append(f"<h3>{esc(s)}</h3>")
        elif s.startswith("•"):
            item = esc(s.lstrip("•").strip())
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{item}</li>")
        else:
            close_list()
            html.append(f"<p>{esc(s)}</p>")

    close_list()
    return "".join(html)


async def create_task(title: str, description: str, column_id: int):
    """
    Создаёт задачу в указанной колонке доски.
    Возвращает (ok: bool, message: str).
    """
    if not WEEEK_API_KEY:
        return False, "WEEEK_API_KEY не задан"

    url = f"{WEEEK_BASE}/tm/tasks"
    headers = {
        "Authorization": f"Bearer {WEEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    # WEEEK через API схлопывает обычные переносы \n.
    # Конвертируем в HTML: каждая строка в <p>, пустые строки дают отступ.
    html_description = _to_html(description)

    payload = {
        "title": title,
        "description": html_description,
        "type": "action",
        "locations": [
            {
                "projectId": PROJECT_ID,
                "boardId": BOARD_ID,
                "boardColumnId": column_id,
            }
        ],
    }

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                text = await resp.text()
                if resp.status in (200, 201):
                    return True, "Задача создана в WEEEK"
                logger.error(f"WEEEK create_task статус {resp.status}: {text[:300]}")
                return False, f"WEEEK вернул ошибку {resp.status}"
    except Exception as e:
        logger.error(f"Ошибка WEEEK create_task: {e}")
        return False, f"Ошибка соединения с WEEEK: {e}"
