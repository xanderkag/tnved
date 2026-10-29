"""
Канал решений шлюза Ванги (POST /v1/decisions): модель не пишет текст, а выбирает
один из заданных вариантов и отдаёт вероятности по всем (манифест parsdocs «Jev»).

Навигатор зовёт её на каждом узле дерева: ветки узла — варианты, описание товара —
состояние. Вероятность честная (замер 29.10 на решениях ЕЭК: на узлах с уверенностью
от 0,9 — 91% верных, ниже 0,4 — около половины), поэтому по ней решаем, спускаться
самим или спросить человека.

Только для инстанса asha. Jev считает у поставщика (TypeSafe через OpenRouter), то
есть описание товара уходит наружу; в контуре Taipit канал не включать. Включается
DECISIONS_MODEL (псевдоним из GET /v1/decisions/models, сейчас «jev»); адрес и ключ —
те же, что у LLM (LLM_BASE_URL, LLM_API_KEY / LLM_API_KEY_FILE).
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx2 as httpx  # пакет сервиса (constraints.txt); openai ходит через него же
from pydantic import BaseModel

from classifier import LLMConfig

DECISIONS_TIMEOUT = float(os.environ.get("DECISIONS_TIMEOUT", "30"))
# Шлюз перезапускают при выкатке на 1–2 минуты (parsdocs, 29.09): 502, отказ соединения.
# В живом запросе ждём недолго, прогоны замера передают свой бюджет.
DECISIONS_RETRY_BUDGET = float(os.environ.get("DECISIONS_RETRY_BUDGET", "20"))
_RETRY_STATUS = {429, 502, 503, 504}


class DecisionsConfig(BaseModel):
    base_url: str
    api_key: str
    model: str


class DecisionsUnavailable(RuntimeError):
    """Канал решений не ответил или отказал. quota — исчерпан суточный бюджет потребителя:
    до 00:00 UTC повторять бессмысленно."""

    def __init__(self, message: str, quota: bool = False):
        super().__init__(message)
        self.quota = quota


def decisions_config_from_env(llm: LLMConfig) -> DecisionsConfig | None:
    """None — канал выключен (DECISIONS_MODEL пуст)."""
    model = os.environ.get("DECISIONS_MODEL", "").strip()
    if not model:
        return None
    return DecisionsConfig(base_url=llm.base_url.rstrip("/"), api_key=llm.api_key, model=model)


def _quota_exceeded(resp: httpx.Response) -> bool:
    """429 бывает двух видов: частота (ждём) и суточный бюджет потребителя (ждать до
    00:00 UTC бессмысленно — отдаём ошибку сразу)."""
    if resp.status_code != 429:
        return False
    try:
        return (resp.json().get("error") or {}).get("code") == "quota_exceeded"
    except ValueError:
        return False


class Choice(BaseModel):
    choice: str
    confidence: float
    probabilities: dict[str, float]
    cost_usd: float = 0.0


async def choose(cfg: DecisionsConfig, state: str, instructions: str, criteria: dict[str, str],
                 retry_budget: float | None = None) -> Choice:
    """Один вопрос типа choice: какой из criteria подходит state."""
    body = {"model": cfg.model, "state": state, "questions": {"q": {
        "type": "choice", "instructions": instructions, "criteria": criteria}}}
    budget = DECISIONS_RETRY_BUDGET if retry_budget is None else retry_budget
    t0, delay = time.monotonic(), 1.0
    async with httpx.AsyncClient(timeout=DECISIONS_TIMEOUT) as client:
        while True:
            try:
                resp = await client.post(f"{cfg.base_url}/decisions", json=body,
                                         headers={"Authorization": f"Bearer {cfg.api_key}"})
                if resp.status_code not in _RETRY_STATUS or _quota_exceeded(resp):
                    break
                problem = f"HTTP {resp.status_code}"
            except httpx.TransportError as exc:
                problem = f"{type(exc).__name__}"
            if time.monotonic() - t0 + delay > budget:
                raise DecisionsUnavailable(f"Канал решений {cfg.model} не ответил: {problem}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    if _quota_exceeded(resp):
        raise DecisionsUnavailable("Дневной лимит модели исчерпан, сброс в 00:00 UTC (03:00 МСК)",
                                   quota=True)
    if resp.status_code != 200:
        raise DecisionsUnavailable(f"Канал решений {cfg.model}: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    answer = data["answers"]["q"]
    if answer.get("choice") not in criteria:
        raise DecisionsUnavailable(f"Канал решений {cfg.model} вернул вариант вне списка")
    return Choice(choice=answer["choice"], confidence=float(answer.get("confidence") or 0.0),
                  probabilities={k: float(v) for k, v in (answer.get("probabilities") or {}).items()},
                  cost_usd=float((data.get("usage") or {}).get("cost") or 0.0))
