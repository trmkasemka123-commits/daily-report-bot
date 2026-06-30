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
    payload = {
        "title": title,
        "description": description,
        "type": "board",
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
