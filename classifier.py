"""
Двухстадийный пайплайн классификации ТН ВЭД.

Stage 1 (triage): по описанию определяет вероятную группу, до 3 позиций + что не хватает.
Stage 2 (classify): кандидаты — лучшие коды группы и названных позиций (позиции могут быть
из других групп); модель выбирает финальный код + альтернативы.

LLM — только наша модель на нашем железе: OpenAI-совместимый адрес (vLLM)
из env сервера, ответ — строгий JSON (response_format=json_object). Модель
задаётся только на сервере: ни значений по умолчанию, ни выбора из запроса.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel

from gri import GRI_HINT_FOR_PROMPT, gri_text
from netcheck import require_internal_url
from tnved_data import TNVEDStore


# ─── конфиг LLM ───────────────────────────────────────────────────────────────

class LLMConfig(BaseModel):
    """Наша модель: OpenAI-совместимый адрес во внутренней сети и имя модели."""
    base_url: str
    model: str
    api_key: str = ""


# По этим переменным раньше ходили в OpenAI / Anthropic. Если они заданы,
# человек думает, что они работают, — поэтому не молчим, а не стартуем.
_RETIRED_ENV = ("LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY")


def llm_config_from_env() -> LLMConfig:
    """Модель из env сервера. Нет адреса во внутренней сети или имени модели — RuntimeError."""
    retired = [name for name in _RETIRED_ENV if os.environ.get(name, "").strip()]
    if retired:
        raise RuntimeError(
            f"Переменные {', '.join(retired)} больше не читаются: внешние провайдеры отключены. "
            "Уберите их; модель задаётся через LLM_BASE_URL, LLM_MODEL и LLM_API_KEY."
        )
    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not base_url or not model:
        raise RuntimeError(
            "Модель не задана: нужны LLM_BASE_URL (адрес нашей модели, например "
            "http://10.10.33.10:8100/v1) и LLM_MODEL. Значения по умолчанию нет намеренно: "
            "без нашей модели сервис не стартует."
        )
    return LLMConfig(
        base_url=require_internal_url(base_url, "LLM_BASE_URL"),
        model=model,
        api_key=os.environ.get("LLM_API_KEY", "").strip(),
    )


class LLMUnavailable(RuntimeError):
    """Наша модель не ответила. На другую модель не переключаемся — это отказ."""


# ─── вызов модели ─────────────────────────────────────────────────────────────

LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))
# Потолок ответа. Без него зациклившаяся модель пишет до конца контекста (32k токенов) —
# минуты вместо секунд. Обрезанный ответ — отказ, а не «что успело».
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))


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


async def llm_json(cfg: LLMConfig, system: str, user: str) -> dict:
    """Запрос к нашей модели с response_format=json_object.

    Любой непригодный ответ — отказ (LLMUnavailable → 503), а не пустой результат:
    таймаут, ошибка сервера, ответ обрезан по LLM_MAX_TOKENS, не JSON-объект.
    """
    client = AsyncOpenAI(
        api_key=cfg.api_key or "EMPTY",
        base_url=cfg.base_url,
        timeout=LLM_TIMEOUT,
        # Без скрытых повторов: они втрое растягивают таймаут и добавляют нагрузку
        # на и без того занятую модель. Отказ виден сразу.
        max_retries=0,
    )
    t0 = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=cfg.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=LLM_MAX_TOKENS,
            # Qwen3.x — без «размышлений», ответ сразу JSON. Иначе при reasoning-парсере
            # на сервере модель думает до JSON тысячи токенов и не укладывается в таймаут.
            # Шаблоны других моделей лишний ключ игнорируют.
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    except APIError as exc:
        raise LLMUnavailable(f"Модель {cfg.model} не ответила: {exc}") from exc

    choice = resp.choices[0]
    usage = resp.usage
    print(f"[llm] {cfg.model}: {time.perf_counter() - t0:.1f}s, "
          f"prompt={usage.prompt_tokens if usage else '?'} "
          f"compl={usage.completion_tokens if usage else '?'}, finish={choice.finish_reason}")
    if choice.finish_reason == "length":
        raise LLMUnavailable(f"Модель {cfg.model}: ответ обрезан на {LLM_MAX_TOKENS} токенах (LLM_MAX_TOKENS)")
    try:
        result = _parse_json_loose(choice.message.content or "")
    except json.JSONDecodeError as exc:
        raise LLMUnavailable(f"Модель {cfg.model} вернула не JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise LLMUnavailable(f"Модель {cfg.model} вернула не JSON-объект, а {type(result).__name__}")
    return result

# ─── промпты ──────────────────────────────────────────────────────────────────

TRIAGE_SYSTEM = """Ты эксперт-классификатор ТН ВЭД ЕАЭС с многолетним опытом.

Задача: по описанию товара
1) определить наиболее вероятную ГРУППУ (2 знака) ТН ВЭД,
2) назвать до 3 наиболее вероятных ТОВАРНЫХ ПОЗИЦИЙ (4 знака), самую вероятную первой.
   Позиции могут быть из разных групп. Для части машины групп 84 или 85 назови обе: позицию
   по её собственной функции, если она сама товар групп 84 или 85 (вентилятор — 8414, блок
   питания — 8504, кабель — 8544, печатная схема — 8534), и позицию частей той машины, для
   которой она предназначена: 8409 — двигателей 8407, 8408; 8431 — машин 8425–8430; 8448 —
   текстильных машин 8444–8447; 8466 — станков 8456–8465; 8473 — вычислительных и конторских
   машин 8470–8472, в том числе серверов; 8503 — электродвигателей и генераторов 8501, 8502;
   8522 — аппаратуры 8519–8521; 8529 — аппаратуры 8524–8528; 8538 — аппаратуры 8535–8537;
   прочие части — 8487, 8548,
3) оценить полноту информации для классификации до 10 знаков,
4) если данных недостаточно — сформулировать конкретные уточняющие вопросы.

Важно:
- Группу выбирай ТОЛЬКО из предоставленного списка.
- Позиция — 4 цифры кода ТН ВЭД (например, "8471"). Не уверен — назови меньше.
- Вопросы должны касаться характеристик, реально влияющих на классификацию в этой группе:
  состав/материал, способ обработки, форма, назначение, технические параметры.
- Не задавай очевидных вопросов и не повторяйся.
- Максимум 4 вопроса. Если описания достаточно — ставь completeness="high" и questions=[].

Отвечай СТРОГО в формате JSON:
{
  "group_code": "XX",
  "group_name": "...",
  "headings": ["XXXX"],
  "completeness": "high" | "medium" | "low",
  "missing_aspects": ["..."],
  "questions": [
    { "id": "q1", "question": "...", "hint": "почему это важно для классификации" }
  ]
}"""


CLASSIFY_SYSTEM = """Ты эксперт-классификатор ТН ВЭД ЕАЭС.

Тебе дано:
- полное описание товара со всеми ответами на уточнения,
- предварительно определённая группа ТН ВЭД и вероятные товарные позиции (они могут быть из других групп),
- список кандидатных кодов с полной иерархией (раздел → группа → позиция → субпозиция → подсубпозиция):
  коды этой группы и коды названных позиций,
- Основные правила интерпретации (ОПИ).

Задача:
1) выбрать ОДИН наиболее точный код из кандидатов (10 знаков, в формате как в кандидатах).
   Код бери только из списка кандидатов и не составляй сам: такого кода может не быть в тарифе.
   Группа предварительная: если по ОПИ точнее код другой группы из списка — выбирай его,
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

# Примечание 2 к разделу XVI — в промпт classify, когда среди кандидатов есть коды групп 84
# или 85. Без него модель относила вентилятор к двигателям (8501), корпус сервера — к
# устройствам 8471 80, объединительную плату и райзер — к печатным схемам 8534 (пилот-10, Д3).
SECTION_XVI_NOTE = """ЧАСТИ МАШИН ГРУПП 84 И 85 (примечание 2 к разделу XVI) — применять по порядку:
а) часть, которая сама является товаром какой-либо позиции групп 84 или 85, включается в эту позицию, даже если сделана для конкретной машины: вентилятор, в том числе со своим двигателем, — 8414, а не двигатель 8501 (встраиваемый — 8414 59: в 8414 51 только настольные, напольные, настенные, оконные, потолочные и крышные); блок питания — 8504; кабель — 8544; печатная схема — 8534. Позиции частей (8409, 8431, 8448, 8466, 8473, 8487, 8503, 8522, 8529, 8538, 8548) здесь не в счёт. Часть, подходящая под п. а), по п. б) не классифицируется: вентилятор, блок питания, кабель для сервера — в 8414, 8504, 8544, а не в 8473;
б) прочие части, пригодные только или в основном для машин одной позиции, — в позицию частей этих машин: части вычислительных машин 8471 (серверов, компьютеров) — 8473 30. Корпус (шасси) без установленных блоков — такая часть, а не устройство вычислительной машины 8471;
в) остальные части — 8487 или 8548.
Печатная схема 8534 — плата только с проводниками, контактами и пассивными элементами, полученными печатью; разъёмы допускаются. Плата с установленными микросхемами или другими элементами — не 8534, а электронный модуль, часть машины по п. б): для 8471 — 8473 30 20. Механические части (корпус, лоток, радиатор, панель) — не электронные модули: для 8471 — 8473 30 80. Если из описания не видно, голая ли плата, — выбирай по п. б), 8534 дай альтернативой и впиши проверку в checks_required."""


def _notes_for_prompt(candidates: list[dict]) -> str:
    """Примечания к разделам, нужные для этих кандидатов; пока одно — к разделу XVI."""
    if any(c["code"][:2] in ("84", "85") for c in candidates):
        return SECTION_XVI_NOTE + "\n\n"
    return ""


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


# ─── сверка ответа со справочником ────────────────────────────────────────────

_CODE_PROBLEM = {
    "unknown": "такого кода нет в справочнике",
    "retired": "код снят — есть только в дереве 2017, в действующем тарифе его нет",
    "not_leaf": "это не 10-значный код, а уровень пути",
}


def _code_digits(raw: object) -> str:
    """«9401 39 000 0», «9401.39.000.0» → «9401390000»: модель пишет код и с разделителями."""
    return re.sub(r"\D", "", str(raw or ""))


def _check_codes(store: TNVEDStore, result: dict, candidate_codes: set[str]) -> None:
    """Наружу — только действующий 10-значный код. Модель выдумывала коды (8544300000)
    и выбирала снятые (9401300001) — такой ответ не выдаём, а показываем причину.

    primary с плохим кодом: code="" (отказ), confidence="low", rejected=причина,
    model_code=что ответила модель. Плохие альтернативы убираются в rejected_alternatives.
    Действующий код не из кандидатов принимаем, но уверенность — не выше средней.
    """
    primary = result["primary"]
    raw = str(primary.get("code") or "").strip()
    code = _code_digits(raw)
    status = store.code_status(code)[0] if code else "empty"
    if status == "current":
        primary["code"] = code
        primary["in_candidates"] = code in candidate_codes
        if not primary["in_candidates"]:
            if primary.get("confidence") == "high":
                primary["confidence"] = "medium"
            result["checks_required"].insert(
                0, f"Код {code} модель выбрала не из найденных кандидатов — проверить по тарифу.")
    else:
        reason = "модель не назвала код" if status == "empty" else f"{raw}: {_CODE_PROBLEM[status]}"
        primary.update(code="", model_code=raw, confidence="low", rejected=reason)
        result["checks_required"].insert(0, f"Код не выдан — {reason}. Нужна ручная классификация.")

    kept, rejected = [], []
    for alt in result["alternatives"]:
        raw_alt = str(alt.get("code") or "").strip()
        alt_code = _code_digits(raw_alt)
        alt_status = store.code_status(alt_code)[0] if alt_code else "empty"
        if alt_status != "current":
            reason = "нет кода" if alt_status == "empty" else _CODE_PROBLEM[alt_status]
            rejected.append({"code": raw_alt, "reason": reason})
        elif alt_code == primary["code"] or any(a["code"] == alt_code for a in kept):
            rejected.append({"code": raw_alt, "reason": "повтор"})
        else:
            alt["code"] = alt_code
            kept.append(alt)
    result["alternatives"] = kept
    result["rejected_alternatives"] = rejected


# ─── stage 1 — triage ────────────────────────────────────────────────────────

# Позиции от triage: сколько берём и сколько кодов каждой добавляем к кандидатам — не меньше
# HEADING_TOP_K и по коду на каждую подпозицию, но не больше HEADING_MAX.
MAX_HEADINGS = 3
HEADING_TOP_K = 8
HEADING_MAX = 16


def _valid_headings(store: TNVEDStore, raw: object) -> list[str]:
    """Позиции из ответа triage → до 3 четырёхзначных, у которых есть действующие коды.

    «8473 30», «84.73» → 8473: позиция — первые 4 цифры. Группа («84»), позиция без
    действующих кодов и повтор отбрасываются — в поиск идёт только то, что есть в тарифе.
    """
    if isinstance(raw, str):
        raw = re.split(r"[,;]", raw)
    if not isinstance(raw, list):
        return []
    headings: list[str] = []
    for item in raw:
        digits = _code_digits(item)
        if len(digits) >= 4 and digits[:4] in store.current_headings and digits[:4] not in headings:
            headings.append(digits[:4])
    return headings[:MAX_HEADINGS]


def _heading_candidates(found: list[dict]) -> list[dict]:
    """Коды позиции по убыванию близости → лучший код каждой подпозиции (6 знаков), затем
    ближайшие, пока не наберётся HEADING_TOP_K; всего не больше HEADING_MAX, порядок — поиска.

    Восьми ближайших мало: у вентилятора в 8414 не было ни одного кода 8414 59, у
    объединительной платы в 8473 — ни одного 8473 30, и модель не могла их выбрать (Д3).
    """
    reps: list[str] = []
    subheadings: set[str] = set()
    for c in found:
        if c["code"][:6] not in subheadings:
            subheadings.add(c["code"][:6])
            reps.append(c["code"])
    limit = min(max(HEADING_TOP_K, len(reps)), HEADING_MAX)
    chosen = set(reps[:limit])
    for c in found:
        if len(chosen) >= limit:
            break
        chosen.add(c["code"])
    return [c for c in found if c["code"] in chosen]


async def triage(
    store: TNVEDStore,
    description: str,
    cfg: LLMConfig,
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
    # Название — из базы: модель переписывала его из списка с опечатками («АППАТУРА»).
    group = store.group_info(group_code) if group_code else None
    result["group_name"] = group["description"] if group else ""
    result["headings"] = _valid_headings(store, result.get("headings"))
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

PATH_SEP = " → "
PROMPT_LEVEL_LIMIT = 150


def _path_levels(item: dict) -> list[str]:
    return [s.strip() for s in (item.get("full_path") or "").split(PATH_SEP) if s.strip()]


def _cut_level(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _path_for_prompt(item: dict, group_code: str) -> str:
    """Путь кандидата для модели: «позиция → … → с шестигранной головкой → из
    коррозионностойкой стали → прочие». Различает кандидатов хвост пути, поэтому
    он идёт целиком; группа уже названа строкой «ОПРЕДЕЛЁННАЯ ГРУППА», а длинные
    уровни (наименования позиций — до 250 знаков) подрезаем.
    """
    segs = _path_levels(item)
    if len(segs) > 1 and item["code"][:2] == group_code:
        segs = segs[1:]
    return PATH_SEP.join(_cut_level(s, PROMPT_LEVEL_LIMIT) for s in segs) or item["description"]


# ─── хвост кода внутри субпозиции ─────────────────────────────────────────────

# Кандидаты classify — ближайшие коды группы и по коду на каждую подпозицию названных позиций,
# поэтому соседей выбранного кода модель может не видеть: у корпуса сервера из 8473 30 был виден
# только 8473302008 (электронные модули), а 8473308000 (прочие части) — нет, у вентилятора из
# 8414 59 — только центробежные, у блока питания из 8504 40 — только инверторы (пилот-10, Д4).
# Тогда код уточняется вторым вызовом по всем кодам подпозиции: в 99 % подпозиций их до 18,
# в самой большой — 72, берём ближайшие TAIL_MAX. Тексты кодов здесь почти не режем: у
# 8473302002 ключ «звуковая карта» — в конце текста из 371 знака.
TAIL_MAX = 30
TAIL_LEVEL_LIMIT = 400

TAIL_SYSTEM = """Ты эксперт-классификатор ТН ВЭД ЕАЭС.

Товар уже отнесён к субпозиции (первые 6 знаков кода). Выбери в ней ОДИН 10-значный код.
Сравни описание товара с текстом каждого кода субпозиции и возьми тот, под который товар подходит;
«прочие» — только если товар не подходит ни под один более конкретный код. Примечания, если даны, применяй.
Код бери только из списка.

Отвечай СТРОГО в формате JSON:
{"code": "XXXXXXXXXX", "reasoning": "1–2 предложения: почему этот код, а не соседние"}"""


def _tail_prompt(description: str, code: str, siblings: list[dict]) -> str:
    """Общая часть пути — строкой субпозиции (без группы), у кодов — только то, чем они различаются."""
    paths = [_path_levels(c) for c in siblings]
    common = 0
    for level in zip(*paths):
        if any(s != level[0] for s in level):
            break
        common += 1
    common = max(0, min(common, min(len(p) for p in paths) - 1))  # у каждого кода остаётся хоть уровень
    head = PATH_SEP.join(_cut_level(s, PROMPT_LEVEL_LIMIT) for s in paths[0][1:common])
    lines = []
    for i, (c, p) in enumerate(zip(siblings, paths)):
        tail = PATH_SEP.join(_cut_level(s, TAIL_LEVEL_LIMIT) for s in p[common:]) or c["description"]
        lines.append(f"  {i+1}. [{c['code']}] {tail}")
    return (
        f"ОПИСАНИЕ ТОВАРА:\n{description}\n\n"
        f"СУБПОЗИЦИЯ {code[:4]} {code[4:6]}" + (f": {head}" if head else "") + "\n\n"
        "КОДЫ СУБПОЗИЦИИ:\n" + "\n".join(lines) + "\n\n"
        f"{_notes_for_prompt(siblings)}"
    ).rstrip() + "\n"


async def _refine_tail(
    store: TNVEDStore,
    description: str,
    result: dict,
    candidate_codes: set[str],
    cfg: LLMConfig,
) -> None:
    """Код выбран, а из его подпозиции модель видела не все коды, — уточнить по полному списку.

    Новый код — только из этой подпозиции; прежний становится первой альтернативой. Модель
    не ответила — остаётся первый выбор и проверка в checks_required; назвала код не из
    списка — остаётся первый выбор. Что было — в primary["tail"]; in_candidates и уверенность —
    по первому выбору.
    """
    primary = result["primary"]
    code = primary.get("code") or ""
    if not code:
        return
    found = await asyncio.to_thread(
        store.search, description, top_k=len(store.meta), group_code=code[:6]
    )
    siblings = found[:TAIL_MAX]
    if code not in {c["code"] for c in siblings}:  # в индексе может не быть кода — строка из базы
        siblings = siblings[:TAIL_MAX - 1] + [store.code_to_meta.get(code) or store.code_status(code)[1]]
    codes = {c["code"] for c in siblings}
    if len(codes) < 2 or codes <= candidate_codes:
        return
    siblings.sort(key=lambda c: c["code"])

    try:
        answer = await llm_json(cfg, TAIL_SYSTEM, _tail_prompt(description, code, siblings))
    except LLMUnavailable as exc:
        primary["tail"] = {"from": code, "error": str(exc)}
        result["checks_required"].append(
            f"Код внутри субпозиции {code[:4]} {code[4:6]} не уточнён ({exc}) — "
            "проверить последние знаки по тарифу.")
        return
    new = _code_digits(answer.get("code"))
    why = str(answer.get("reasoning") or "").strip()
    primary["tail"] = {"from": code, "to": new if new in codes else None,
                       "codes": sorted(codes), "reasoning": why}
    if new not in codes or new == code:
        return
    primary["code"] = new
    primary["reasoning"] = f"{primary['reasoning']} Код внутри субпозиции уточнён: {why}".strip()
    result["alternatives"] = [a for a in result["alternatives"] if a.get("code") != new]
    result["alternatives"].insert(0, {
        "code": code,
        "why_close": "выбран сначала, когда из субпозиции были видны не все коды",
        "why_rejected": why,
    })


async def classify(
    store: TNVEDStore,
    description: str,
    group_code: str,
    cfg: LLMConfig,
    top_k: int = 12,
    headings: list[str] | None = None,
) -> dict:
    """Финальная классификация: кандидаты из группы и из позиций triage, LLM выбирает код."""
    # В lite-режиме store игнорирует top_k и отдаёт LITE_TOP_K листьев группы;
    # в полном режиме это векторный поиск (CPU-bound, поэтому в to_thread).
    candidates = await asyncio.to_thread(
        store.search, description, top_k=top_k, group_code=group_code
    )

    # Если в группе ничего не нашлось — fallback на поиск без фильтра
    if not candidates:
        candidates = await asyncio.to_thread(store.search, description, top_k=top_k)

    # Лучшие коды позиций, названных triage, — в том числе из других групп. По английскому
    # описанию поиск в группе приносит шум (у кабеля Mini-SAS 12 из 12 — носители 8523 29),
    # а нужной подпозиции в нём нет, и модель дописывала хвост кода сама (пилот-10).
    have = {c["code"] for c in candidates}
    for heading in headings or []:
        found = await asyncio.to_thread(  # вся позиция по близости: в самой большой 298 кодов
            store.search, description, top_k=len(store.meta), group_code=heading
        )
        for c in _heading_candidates(found):
            if c["code"] not in have:
                have.add(c["code"])
                candidates.append(c)

    candidates_text = "\n".join(
        f"  {i+1}. [{c['code']}] {_path_for_prompt(c, group_code)}"
        for i, c in enumerate(candidates)
    )

    group_info = store.group_info(group_code)
    group_line = f"{group_info['code']} {group_info['description']}" if group_info else group_code
    headings_line = f"ВЕРОЯТНЫЕ ПОЗИЦИИ: {', '.join(headings)}\n\n" if headings else ""

    user = (
        f"ОПИСАНИЕ ТОВАРА:\n{description}\n\n"
        f"ОПРЕДЕЛЁННАЯ ГРУППА: {group_line}\n\n"
        f"{headings_line}"
        f"КАНДИДАТЫ:\n{candidates_text}\n\n"
        f"{_notes_for_prompt(candidates)}"
        f"{GRI_HINT_FOR_PROMPT}"
    )
    result = await llm_json(cfg, CLASSIFY_SYSTEM, user)

    # Валидация: вместо объекта или списка модель может вернуть null или строку с кодом
    primary = result.get("primary")
    if not isinstance(primary, dict):
        primary = {"code": primary or ""}
    primary.setdefault("code", "")
    primary.setdefault("reasoning", "")
    primary.setdefault("confidence", "medium")
    result["primary"] = primary

    for key in ("alternatives", "gri_applied", "checks_required"):
        if not isinstance(result.get(key), list):
            result[key] = []
    result["alternatives"] = [a if isinstance(a, dict) else {"code": a} for a in result["alternatives"]]

    candidate_codes = {c["code"] for c in candidates}
    _check_codes(store, result, candidate_codes)
    await _refine_tail(store, description, result, candidate_codes, cfg)

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
    # Группа ответа — группа выданного кода: он мог прийти из позиции другой группы.
    # Код не выдан — группа triage. Название — из базы.
    result["group_code"] = code[:2] if code else group_code
    group = store.group_info(result["group_code"]) if result["group_code"] else None
    result["group_name"] = group["description"] if group else ""
    return result
