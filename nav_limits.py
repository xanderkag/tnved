"""
Лимиты навигатора для открытой страницы (D-10): считаем товары, а не клики. Новый товар —
запрос без path; ответы кнопками на вопрос по тому же товару бесплатны, иначе посетитель
упрётся в лимит посреди подбора.

NAV_FREE_PER_DAY — товаров в сутки без почты (0 — лимита на посетителя нет: внутренний UI,
Taipit); NAV_EMAIL_PER_DAY — с почтой; NAV_DAILY_CAP — новых товаров в сутки на весь сервис
(0 — без потолка). Потолок держим ниже бюджета шлюза на Jev: при наплыве отказ получают новые
товары, а не весь канал до 00:00 UTC. Сутки — UTC, как у квоты шлюза. Счётчики в памяти:
перезапуск их обнуляет, это допустимо.

Кто посетитель и оставил ли почту, говорит сайт: X-Visitor-Id и X-Visitor-Email: 1 (почта
подтверждена) — только вместе с X-Site-Token, равным NAV_SITE_TOKEN. Без токена посетитель —
адрес клиента и лимит без почты: заголовкам из браузера не верим. Почту сайт нам не передаёт.
Сервис слушает только 127.0.0.1 за прокси, и адрес клиента для него — адрес прокси: с
NAV_TRUST_PROXY=1 берём последний X-Forwarded-For — его дописывает наш прокси.
"""

from __future__ import annotations

import hmac
import os
import time

FREE_PER_DAY = int(os.environ.get("NAV_FREE_PER_DAY", "0"))
EMAIL_PER_DAY = int(os.environ.get("NAV_EMAIL_PER_DAY", "10"))
DAILY_CAP = int(os.environ.get("NAV_DAILY_CAP", "0"))
SITE_TOKEN = os.environ.get("NAV_SITE_TOKEN", "")
TRUST_PROXY = os.environ.get("NAV_TRUST_PROXY", "") == "1"
RESET_TEXT = "обновится в 03:00 МСК"


class LimitExceeded(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _goods(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} товар"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} товара"
    return f"{n} товаров"


class Limits:
    def __init__(self, free: int = FREE_PER_DAY, email: int = EMAIL_PER_DAY, cap: int = DAILY_CAP,
                 site_token: str = SITE_TOKEN, trust_proxy: bool = TRUST_PROXY):
        self.free, self.email, self.cap, self.site_token = free, email, cap, site_token
        self.trust_proxy = trust_proxy
        self.day = ""
        self.used: dict[str, int] = {}
        self.total = 0

    @property
    def enabled(self) -> bool:
        return bool(self.free or self.cap)

    def _roll(self) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if day != self.day:
            self.day, self.used, self.total = day, {}, 0

    def visitor(self, headers, host: str) -> tuple[str, bool]:
        """Ключ посетителя и есть ли у него почта."""
        token = headers.get("x-site-token", "")
        if self.site_token and token and hmac.compare_digest(token, self.site_token):
            vid = headers.get("x-visitor-id", "").strip()[:128]
            if vid:
                return f"v:{vid}", headers.get("x-visitor-email", "").strip() == "1"
        if self.trust_proxy:
            host = headers.get("x-forwarded-for", "").split(",")[-1].strip() or host
        return f"ip:{host}", False

    def take(self, key: str, email: bool) -> tuple[int, int]:
        """Засчитать новый товар до похода в модель — иначе параллельные запросы одного
        посетителя пройдут проверку все разом. Вернёт (подобрано сегодня, лимит; 0 — без лимита).
        Модель отказала — refund: отказ шлюза посетителю не в счёт."""
        self._roll()
        if self.cap and self.total >= self.cap:
            raise LimitExceeded(503, f"Подбор кодов на сегодня исчерпан, {RESET_TEXT}")
        limit = (self.email if email else self.free) if self.free else 0
        if limit and self.used.get(key, 0) >= limit:
            if email:
                raise LimitExceeded(429, f"Лимит — {_goods(limit)} в сутки, {RESET_TEXT}")
            raise LimitExceeded(429, f"Бесплатно — {_goods(limit)} в сутки. "
                                     f"Оставьте почту — будет {self.email}.")
        self.used[key] = self.used.get(key, 0) + 1
        self.total += 1
        return self.used[key], limit

    def refund(self, key: str) -> None:
        if self.used.get(key):  # после смены суток счётчик уже пуст — возвращать нечего
            self.used[key] -= 1
            self.total -= 1
