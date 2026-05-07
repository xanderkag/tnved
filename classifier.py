"""
Двухстадийный пайплайн классификации ТН ВЭД.

Stage 1 (triage): по описанию определяет вероятную группу + что не хватает.
Stage 2 (classify): из кандидатов внутри группы выбирает финальный код + альтернативы.

LLM возвращает строгий JSON (OpenAI: response_format=json_object;
Anthropic: эмулируется через системный промпт + parse). Конфиг приходит
per-request из заголовков X-LLM-* (см. api.py), либо подтягивается из env
как fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Literal, Optional

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from gri import GRI_HINT_FOR_PROMPT, gri_text
from tnved_data import TNVEDStore


# ─── конфиг LLM ───────────────────────────────────────────────────────────────

class LLMConfig(BaseModel):
    """Конфигурация LLM-провайдера. Приходит из UI (заголовки X-LLM-*) или env."""
    provider: Literal["openai", "anthropic"] = "openai"
    api_key: str = ""
    base_url: Optional[str] = None
    model: str = ""


_DEFAULT_MODEL_BY_PROVIDER = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}


def resolve_config(cfg: Optional[LLMConfig]) -> LLMConfig:
    """Заполняет пустые поля cfg из env. Используем как fallback в любом эндпоинте."""
    if cfg is None:
        cfg = LLMConfig()

    provider = cfg.provider or "openai"
    api_key = cfg.api_key or os.environ.get(
        "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY",
        "",
    )
    base_url = cfg.base_url or (os.environ.get("OPENAI_BASE_URL") if provider == "openai" else None)
    model = cfg.model or os.environ.get("LLM_MODEL", _DEFAULT_MODEL_BY_PROVIDER[provider])

    return LLMConfig(provider=provider, api_key=api_key, base_url=base_url or None, model=model)


# ─── вызовы провайдеров ───────────────────────────────────────────────────────

LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))


async def _call_openai_json(cfg: LLMConfig, system: str, user: str) -> dict:
    """OpenAI / OpenAI-совместимый: используем нативный response_format=json_object."""
    client = AsyncOpenAI(
        api_key=cfg.api_key or "EMPTY",
        base_url=cfg.base_url,
        timeout=LLM_TIMEOUT,
    )
    resp = await client.chat.completions.create(
        model=cfg.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    raw = resp.choices[0].message.content or "{}"
    return _parse_json_loose(raw)


async def _call_anthropic_json(cfg: LLMConfig, system: str, user: str) -> dict:
    """Anthropic: нет нативного JSON-mode → жёстко требуем формат в system."""
    client = AsyncAnthropic(api_key=cfg.api_key, timeout=LLM_TIMEOUT)
    sys = system + "\n\nВАЖНО: верни ТОЛЬКО валидный JSON-объект, без markdown-обрамления, без преамбулы и пояснений."
    resp = await client.messages.create(
        model=cfg.model,
        max_tokens=2048,
        system=sys,
        messages=[{"role": "user", "content": user}],
        temperature=0.1,
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return _parse_json_loose(raw)


def _parse_json_loose(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


async def llm_json(cfg: Optional[LLMConfig], system: str, user: str) -> dict:
    cfg = resolve_config(cfg)
    if not cfg.api_key:
        raise ValueError(
            "LLM API-ключ не задан. Открой шестерёнку справа сверху и введи ключ "
            "(OpenAI или Anthropic), либо задай env OPENAI_API_KEY / ANTHROPIC_API_KEY."
        )
    if cfg.provider == "anthropic":
        return await _call_anthropic_json(cfg, system, user)
    return await _call_openai_json(cfg, system, user)

# ─── промпты ──────────────────────────────────────────────────────────────────

TRIAGE_SYSTEM = """Ты эксперт-классификатор ТН ВЭД ЕАЭС с многолетним опытом.

Задача: по описанию товара
1) определить наиболее вероятную ГРУППУ (2 знака) ТН ВЭД,
2) оценить полноту информации для классификации до 10 знаков,
3) если данных недостаточно — сформулировать конкретные уточняющие вопросы.

Важно:
- Группу выбирай ТОЛЬКО из предоставленного списка.
- Вопросы должны касаться характеристик, реально влияющих на классификацию в этой группе:
  состав/материал, способ обработки, форма, назначение, технические параметры.
- Не задавай очевидных вопросов и не повторяйся.
- Максимум 4 вопроса. Если описания достаточно — ставь completeness="high" и questions=[].

Отвечай СТРОГО в формате JSON:
{
  "group_code": "XX",
  "group_name": "...",
  "completeness": "high" | "medium" | "low",
  "missing_aspects": ["..."],
  "questions": [
    { "id": "q1", "question": "...", "hint": "почему это важно для классификации" }
  ]
}"""


CLASSIFY_SYSTEM = """Ты эксперт-классификатор ТН ВЭД ЕАЭС.

Тебе дано:
- полное описание товара со всеми ответами на уточнения,
- определённая группа ТН ВЭД,
- список кандидатных кодов с полной иерархией (раздел → группа → позиция → субпозиция → подсубпозиция),
- Основные правила интерпретации (ОПИ).

Задача:
1) выбрать ОДИН наиболее точный код из кандидатов (10 знаков, в формате как в кандидатах),
2) указать 1–2 близкие альтернативы и пояснить, почему они отвергнуты,
3) указать применённые ОПИ,
4) если есть моменты, которые декларант должен проверить вручную — перечислить их,
5) оценить уверенность.

Отвечай СТРОГО в формате JSON:
{
  "primary": {
    "code": "XXXXXXXXXX",
    "reasoning": "обоснование 2-4 предложения",
    "confidence": "high" | "medium" | "low"
  },
  "alternatives": [
    {
      "code": "XXXXXXXXXX",
      "why_close": "почему этот код был рассмотрен",
      "why_rejected": "почему отвергнут в пользу основного"
    }
  ],
  "gri_applied": ["1", "3a"],
  "checks_required": ["..."]
}"""


# ─── вспомогательные ──────────────────────────────────────────────────────────

def normalize_input(simple_text: str | None, fields: dict | None) -> str:
    """Сводит ввод (либо текст, либо поля формы) к единому строковому описанию."""
    if simple_text and simple_text.strip():
        return simple_text.strip()
    if not fields:
        return ""
    parts = []
    for label, value in fields.items():
        if value and str(value).strip():
            parts.append(f"{label}: {value}")
    return "\n".join(parts)


def merge_qa(description: str, answers: list[dict]) -> str:
    """Добавляет к описанию ответы на уточняющие вопросы."""
    if not answers:
        return description
    lines = [description, "", "Уточнения:"]
    for a in answers:
        lines.append(f"- {a['question']}\n  Ответ: {a['answer']}")
    return "\n".join(lines)


# ─── stage 1 — triage ────────────────────────────────────────────────────────

async def triage(
    store: TNVEDStore,
    description: str,
    cfg: Optional[LLMConfig] = None,
) -> dict:
    """Определяет группу и набор уточняющих вопросов."""
    user = (
        f"ОПИСАНИЕ ТОВАРА:\n{description}\n\n"
        f"ДОСТУПНЫЕ ГРУППЫ ТН ВЭД:\n{store.groups_list_for_prompt()}"
    )
    result = await llm_json(cfg, TRIAGE_SYSTEM, user)

    # Валидация и обогащение
    group_code = str(result.get("group_code", "")).strip()
    if len(group_code) == 1:
        group_code = group_code.zfill(2)
    result["group_code"] = group_code
    result.setdefault("group_name", "")
    result.setdefault("completeness", "low")
    result.setdefault("missing_aspects", [])
    result.setdefault("questions", [])

    # Ограничим количество вопросов
    result["questions"] = result["questions"][:4]
    for i, q in enumerate(result["questions"]):
        q.setdefault("id", f"q{i+1}")
        q.setdefault("hint", "")

    return result


# ─── stage 2 — final classification ──────────────────────────────────────────

async def classify(
    store: TNVEDStore,
    description: str,
    group_code: str,
    cfg: Optional[LLMConfig] = None,
    top_k: int = 12,
) -> dict:
    """Финальная классификация: ищем кандидатов в группе, LLM выбирает код."""
    # В lite-режиме store игнорирует top_k и отдаёт LITE_TOP_K листьев группы;
    # в полном режиме это векторный поиск (CPU-bound, поэтому в to_thread).
    candidates = await asyncio.to_thread(
        store.search, description, top_k=top_k, group_code=group_code
    )

    # Если в группе ничего не нашлось — fallback на поиск без фильтра
    if not candidates:
        candidates = await asyncio.to_thread(store.search, description, top_k=top_k)

    candidates_text = "\n".join(
        f"  {i+1}. [{c['code']}] {c.get('full_path') or c['description']}"
        for i, c in enumerate(candidates)
    )

    group_info = store.group_info(group_code)
    group_line = f"{group_info['code']} {group_info['description']}" if group_info else group_code

    user = (
        f"ОПИСАНИЕ ТОВАРА:\n{description}\n\n"
        f"ОПРЕДЕЛЁННАЯ ГРУППА: {group_line}\n\n"
        f"КАНДИДАТЫ:\n{candidates_text}\n\n"
        f"{GRI_HINT_FOR_PROMPT}"
    )
    result = await llm_json(cfg, CLASSIFY_SYSTEM, user)

    # Валидация
    primary = result.get("primary") or {}
    primary.setdefault("code", "")
    primary.setdefault("reasoning", "")
    primary.setdefault("confidence", "medium")
    result["primary"] = primary

    result.setdefault("alternatives", [])
    result.setdefault("gri_applied", [])
    result.setdefault("checks_required", [])

    # Обогащаем коды иерархией, текстами ОПИ и ставкой пошлины
    code = primary["code"]
    primary["hierarchy"] = store.hierarchy(code)
    meta = store.code_to_meta.get(code, {})
    primary["full_path"] = meta.get("full_path")
    primary["duty_rate"] = meta.get("duty_rate")

    for alt in result["alternatives"]:
        alt_code = alt.get("code", "")
        alt["hierarchy"] = store.hierarchy(alt_code)
        alt_meta = store.code_to_meta.get(alt_code, {})
        alt["full_path"] = alt_meta.get("full_path")
        alt["duty_rate"] = alt_meta.get("duty_rate")

    result["gri_explained"] = [
        {"code": code, "text": gri_text(code)}
        for code in result["gri_applied"]
        if gri_text(code)
    ]

    result["candidates"] = candidates
    return result
