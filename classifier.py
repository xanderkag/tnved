"""
Двухстадийный пайплайн классификации ТН ВЭД.

Stage 1 (triage): по описанию определяет вероятную группу + что не хватает.
Stage 2 (classify): из кандидатов внутри группы выбирает финальный код + альтернативы.

LLM возвращает строгий JSON (response_format={"type": "json_object"}).
Бэкенд — любой OpenAI-совместимый endpoint (OpenAI, vLLM, Ollama в режиме
/v1, внутренний шлюз и т.п.). Конфигурится через env: OPENAI_API_KEY,
OPENAI_BASE_URL.
"""

from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

from gri import GRI_HINT_FOR_PROMPT, gri_text
from tnved_data import TNVEDStore

_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """Ленивый singleton AsyncOpenAI. Конфиг — из env."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=os.environ.get("OPENAI_API_KEY", "EMPTY"),
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
        )
    return _client

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


async def _llm_json(client: AsyncOpenAI, model: str, system: str, user: str) -> dict:
    """Вызов OpenAI-совместимого chat API с JSON-режимом и парсинг в dict."""
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # последняя попытка — найти JSON в тексте
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start:end + 1])
        raise


# ─── stage 1 — triage ────────────────────────────────────────────────────────

async def triage(
    store: TNVEDStore,
    description: str,
    model: str,
) -> dict:
    """Определяет группу и набор уточняющих вопросов."""
    client = get_client()
    user = (
        f"ОПИСАНИЕ ТОВАРА:\n{description}\n\n"
        f"ДОСТУПНЫЕ ГРУППЫ ТН ВЭД:\n{store.groups_list_for_prompt()}"
    )
    result = await _llm_json(client, model, TRIAGE_SYSTEM, user)

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
    model: str,
    top_k: int = 12,
) -> dict:
    """Финальная классификация: ищем кандидатов в группе, LLM выбирает код."""
    candidates = store.search(description, top_k=top_k, group_code=group_code)

    # Если в группе ничего не нашлось — fallback на поиск без фильтра
    if not candidates:
        candidates = store.search(description, top_k=top_k)

    candidates_text = "\n".join(
        f"  {i+1}. [{c['code']}] {c.get('full_path') or c['description']}"
        for i, c in enumerate(candidates)
    )

    group_info = store.group_info(group_code)
    group_line = f"{group_info['code']} {group_info['description']}" if group_info else group_code

    client = get_client()
    user = (
        f"ОПИСАНИЕ ТОВАРА:\n{description}\n\n"
        f"ОПРЕДЕЛЁННАЯ ГРУППА: {group_line}\n\n"
        f"КАНДИДАТЫ:\n{candidates_text}\n\n"
        f"{GRI_HINT_FOR_PROMPT}"
    )
    result = await _llm_json(client, model, CLASSIFY_SYSTEM, user)

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
