"""
Проверка, что адрес модели — во внутренней сети.

Описание товара уходит в две модели: в LLM (выбор кода) и в векторизацию
запроса (поиск кандидатов). Решение от 25.09.2026 — только наши модели на нашем
железе, во внешние сервисы описания не уходят никогда. Поэтому адрес из конфига
проверяется при старте: хост должен резолвиться только во внутренние адреса
(10/8, 172.16/12, 192.168/16, loopback).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def require_internal_url(url: str, what: str) -> str:
    """Возвращает url, если он ведёт во внутреннюю сеть; иначе RuntimeError с причиной."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RuntimeError(f"{what}: «{url}» — не http(s)-адрес")
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror as exc:
        raise RuntimeError(f"{what}: хост {parts.hostname} не резолвится ({exc})") from exc
    addrs = {info[4][0].split("%")[0] for info in infos}
    external = sorted(a for a in addrs if not ipaddress.ip_address(a).is_private)
    if external:
        raise RuntimeError(
            f"{what}: {parts.hostname} → {', '.join(external)} — это не внутренняя сеть. "
            "Описания товаров во внешние сервисы не отправляем."
        )
    return url
