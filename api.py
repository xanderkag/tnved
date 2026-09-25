"""
FastAPI-бэкенд: двухстадийный пайплайн классификации ТН ВЭД.

Запуск: venv/bin/uvicorn api:app --host 127.0.0.1 --port 8765 --reload
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook, load_workbook
from pydantic import BaseModel, Field

from classifier import (
    LLMConfig,
    LLMUnavailable,
    classify,
    llm_config_from_env,
    merge_qa,
    normalize_input,
    triage,
)
from embedder import EmbeddingsUnavailable
from tnved_data import TNVEDStore

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
BATCH_CONCURRENCY = int(os.environ.get("BATCH_CONCURRENCY", "5"))
BATCH_MAX_ROWS = int(os.environ.get("BATCH_MAX_ROWS", "500"))
MAX_DESCRIPTION_LEN = int(os.environ.get("MAX_DESCRIPTION_LEN", "5000"))
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL_HOURS", "24")) * 3600
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "600"))


def _compute_static_version() -> str:
    """Хеш содержимого app.css + app.js для cache-busting. Считается при импорте."""
    h = hashlib.sha1()
    for fname in ("app.css", "app.js", "index.html"):
        try:
            h.update((STATIC_DIR / fname).read_bytes())
        except FileNotFoundError:
            continue
    return h.hexdigest()[:8]


STATIC_VER = _compute_static_version()

# ─── глобальные ресурсы ───────────────────────────────────────────────────────

class App:
    llm: LLMConfig | None = None
    store: TNVEDStore | None = None
    sessions: dict[str, dict] = {}
    batch_jobs: dict[str, dict] = {}
    chats: dict[str, dict] = {}


state = App()


async def _cleanup_loop() -> None:
    """Периодически чистит протухшие in-memory сущности (B2 в TECH_DEBT)."""
    while True:
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            cutoff_iso = (datetime.utcnow() - timedelta(seconds=SESSION_TTL_SECONDS)).isoformat()

            stale = [k for k, v in state.sessions.items() if (v.get("created_at") or "") < cutoff_iso]
            for k in stale:
                state.sessions.pop(k, None)

            # batch — чистим только завершённые, бегущие не трогаем
            stale = [
                k for k, v in state.batch_jobs.items()
                if v.get("status") in ("done", "failed")
                and (v.get("finished_at") or v.get("started_at") or "") < cutoff_iso
            ]
            for k in stale:
                state.batch_jobs.pop(k, None)

            stale = [k for k, v in state.chats.items() if (v.get("created_at") or "") < cutoff_iso]
            for k in stale:
                state.chats.pop(k, None)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001 — фоновый цикл не должен умирать
            print(f"[cleanup] error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Без нашей модели не стартуем: исключение отсюда останавливает uvicorn.
    state.llm = llm_config_from_env()
    print(f"Модель: {state.llm.model} @ {state.llm.base_url}")
    print("Загружаем ресурсы ТН ВЭД...")
    try:
        state.store = TNVEDStore()
        mode = "LITE (SQLite-only)" if state.store.lite else "FULL (FAISS)"
        print(f"Готово [{mode}]. Кодов в индексе: {state.store.index_ntotal:,}, групп: {len(state.store.groups)}")
    except Exception as e:  # noqa: BLE001 — переходим в degraded-режим вместо краха
        print(f"[lifespan] FATAL: не смог загрузить TNVEDStore: {e}")
        print("[lifespan] сервер поднят в degraded-режиме: classify-эндпоинты будут отдавать 503")
        state.store = None

    cleanup_task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan, title="ТН ВЭД Ассистент")


@app.middleware("http")
async def refuse_llm_headers(request: Request, call_next):
    """Раньше заголовки X-LLM-* меняли адрес, ключ и модель на лету — так описание
    могло уйти во внешний сервис, а серверный ключ — на чужой адрес. Модель теперь
    задаётся только на сервере, и такой запрос — отказ, а не тихое игнорирование."""
    sent = sorted(k for k, v in request.headers.items() if k.startswith("x-llm-") and v.strip())
    if sent:
        return JSONResponse(
            status_code=400,
            content={"detail": f"Заголовки {', '.join(sent)} не принимаются: "
                               "модель задаётся только на сервере."},
        )
    return await call_next(request)


@app.exception_handler(LLMUnavailable)
async def llm_unavailable(request: Request, exc: LLMUnavailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(EmbeddingsUnavailable)
async def embeddings_unavailable(request: Request, exc: EmbeddingsUnavailable):
    return JSONResponse(status_code=503, content={"detail": f"Векторный поиск недоступен: {exc}"})


def _require_store() -> TNVEDStore:
    """Гард для эндпоинтов, которым нужен загруженный store."""
    if state.store is None:
        raise HTTPException(503, "Сервис временно недоступен: справочник ТН ВЭД не загружен")
    return state.store


# ─── схемы ────────────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    mode: str = Field("simple", pattern="^(simple|detailed)$")
    description: Optional[str] = None
    fields: Optional[dict] = None


class AnswerItem(BaseModel):
    id: str
    question: str
    answer: str


class FinalizeRequest(BaseModel):
    session_id: str
    answers: list[AnswerItem] = []


def _llm() -> LLMConfig:
    """Наша модель — из env сервера, проверена при старте (llm_config_from_env)."""
    assert state.llm is not None, "lifespan не даёт стартовать без модели"
    return state.llm


# ─── endpoints ────────────────────────────────────────────────────────────────

@app.post("/api/classify/start")
async def classify_start(
    req: StartRequest,
):
    store = _require_store()
    cfg = _llm()
    description = normalize_input(req.description, req.fields)
    if not description:
        raise HTTPException(400, "Пустое описание товара")
    if len(description) > MAX_DESCRIPTION_LEN:
        raise HTTPException(
            400,
            f"Описание слишком длинное ({len(description)} > {MAX_DESCRIPTION_LEN} символов)",
        )

    triage_result = await triage(store, description, cfg)

    session_id = uuid.uuid4().hex[:12]
    state.sessions[session_id] = {
        "id": session_id,
        "created_at": datetime.utcnow().isoformat(),
        "mode": req.mode,
        "description": description,
        "llm_model": cfg.model,
        "triage": triage_result,
    }

    return {
        "session_id": session_id,
        "description": description,
        "group": {
            "code": triage_result.get("group_code", ""),
            "name": triage_result.get("group_name", ""),
        },
        "completeness": triage_result.get("completeness", "low"),
        "missing_aspects": triage_result.get("missing_aspects", []),
        "questions": triage_result.get("questions", []),
    }


@app.post("/api/classify/finalize")
async def classify_finalize(
    req: FinalizeRequest,
):
    store = _require_store()
    cfg = _llm()
    session = state.sessions.get(req.session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")

    description = merge_qa(
        session["description"],
        [a.model_dump() for a in req.answers],
    )
    group_code = session["triage"].get("group_code", "")

    result = await classify(
        store,
        description=description,
        group_code=group_code,
        cfg=cfg,
        headings=session["triage"].get("headings"),
    )

    session["answers"] = [a.model_dump() for a in req.answers]
    session["full_description"] = description
    session["result"] = result
    session["finalized_at"] = datetime.utcnow().isoformat()

    return {
        "session_id": req.session_id,
        "description": description,
        "group": {"code": result["group_code"], "name": result["group_name"]},
        "result": result,
    }


@app.get("/api/classify/{session_id}")
async def classify_get(session_id: str):
    session = state.sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")
    return session


@app.get("/api/models")
async def models():
    """Какая модель отвечает. Выбора нет: модель задаётся только на сервере."""
    return {"model": _llm().model}


@app.get("/health")
async def health():
    """Лёгкий healthcheck. 200 если store загружен, 503 в degraded-режиме."""
    if state.store is None:
        raise HTTPException(503, "store not loaded")
    return {
        "status": "ok",
        "mode": "lite" if state.store.lite else "full",
        "llm_model": _llm().model,
        "vectors": state.store.index_ntotal,
        "groups": len(state.store.groups),
        "static_version": STATIC_VER,
    }


# ─── справочник ───────────────────────────────────────────────────────────────

CODE_RE = re.compile(r"\d{2}|\d{4}|\d{6}|\d{8}|\d{10}")


@app.get("/api/codes")
async def codes_version():
    """Версия справочника: дата сборки базы и тарифа, число кодов — чтобы потребитель видел, с чем сверяется."""
    return _require_store().version()


EXPORT_DIR = BASE / "exports"
_export_cache: dict[tuple, dict] = {}


def _codes_export() -> dict:
    """Последняя выгрузка export_codes.py, сверенная с базой: {path, sha256, tariff_as_of, rows}.

    Путь в выгрузке — цепочка самого тарифа (data/raw), из базы её не восстановить, поэтому
    отдаём готовый файл. Файл от другой сборки базы или другого тарифа не отдаём — 503.
    """
    files = sorted(EXPORT_DIR.glob("tnved10_paths_*.csv"))
    if not files:
        raise HTTPException(503, "Выгрузки справочника нет: python export_codes.py и положить exports/ в образ")
    path = files[-1]
    key = (path, path.stat().st_mtime_ns)
    if key not in _export_cache:
        data = path.read_bytes()
        reader = csv.DictReader(io.StringIO(data.decode("utf-8"), newline=""), delimiter=";")
        rows = list(reader)
        _export_cache.clear()
        _export_cache[key] = {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "rows": len(rows),
            "tariff_as_of": {r["tariff_as_of"] for r in rows},
            "db_built_at": {r["db_built_at"] for r in rows},
        }
    info = _export_cache[key]
    version = _require_store().version()
    problems = []
    if info["tariff_as_of"] != {version["tariff_as_of"]}:
        problems.append(f"тариф в файле {sorted(info['tariff_as_of'])}, в базе {version['tariff_as_of']}")
    if info["db_built_at"] != {(version["db_built_at"] or "")[:10]}:
        problems.append(f"сборка базы в файле {sorted(info['db_built_at'])}, в базе {version['db_built_at']}")
    if info["rows"] != version["codes10"]:
        problems.append(f"кодов в файле {info['rows']}, в базе {version['codes10']}")
    if problems:
        raise HTTPException(503, f"Выгрузка {path.name} не от этой базы: {'; '.join(problems)}. "
                                 "Пересоберите: python export_codes.py")
    return {**info, "tariff_as_of": version["tariff_as_of"]}


@app.get("/api/codes/export")
async def codes_export():
    """Весь справочник 10-значных кодов с путём — файл export_codes.py (UTF-8, «;», CRLF).

    Версия — в имени файла (дата тарифа) и в заголовках X-Tariff-As-Of, X-Content-SHA256.
    """
    info = await asyncio.to_thread(_codes_export)
    return FileResponse(
        info["path"], media_type="text/csv; charset=utf-8", filename=info["path"].name,
        headers={"X-Tariff-As-Of": info["tariff_as_of"], "X-Content-SHA256": info["sha256"],
                 "X-Codes": str(info["rows"])},
    )


@app.get("/api/codes/{code}")
async def code_card(code: str):
    """Карточка кода: статус (current | retired | not_leaf), путь по дереву, пошлина.

    Кода нет — 404, а не ближайший похожий: подставленный код хуже отказа.
    """
    store = _require_store()
    code = re.sub(r"[\s.]", "", code)
    if not CODE_RE.fullmatch(code):
        raise HTTPException(400, "Код ТН ВЭД — 2, 4, 6, 8 или 10 цифр")
    card = store.code_card(code)
    if card is None:
        raise HTTPException(404, f"Кода {code} нет в справочнике")
    return card


# ─── chat (свободный диалог) ──────────────────────────────────────────────────

class ChatStartRequest(BaseModel):
    initial_description: str = ""


class ChatMessageRequest(BaseModel):
    text: str


def _chat_format_finalization(triage_res: dict, classify_res: dict) -> str:
    primary = classify_res.get("primary") or {}
    code = primary.get("code", "—")
    duty = primary.get("duty_rate") or "—"
    conf = primary.get("confidence", "—")
    reasoning = primary.get("reasoning", "") or ""
    full_path = primary.get("full_path") or ""

    if primary.get("rejected"):
        lines = [f"**Код не выдан:** {primary['rejected']}. Нужна ручная классификация."]
    else:
        lines = [f"**Код:** `{code}`"]
    if full_path:
        lines.append(f"_{full_path}_")
    lines.append(f"**Пошлина:** {duty}  **Уверенность:** {conf}")
    if reasoning:
        lines.append("")
        lines.append(reasoning)

    alts = classify_res.get("alternatives") or []
    if alts:
        lines.append("")
        lines.append("**Альтернативы:**")
        for a in alts[:2]:
            ac = a.get("code", "—")
            ad = a.get("duty_rate") or "—"
            why = a.get("why_close") or ""
            lines.append(f"- `{ac}` ({ad}) — {why}")

    checks = classify_res.get("checks_required") or []
    if checks:
        lines.append("")
        lines.append("**Стоит проверить вручную:**")
        for c in checks[:3]:
            lines.append(f"- {c}")
    return "\n".join(lines)


def _chat_format_questions(triage_res: dict) -> str:
    group_code = triage_res.get("group_code", "")
    group_name = triage_res.get("group_name", "")
    questions = triage_res.get("questions", [])

    lines = []
    if group_code:
        head = f"Похоже на группу **{group_code}**"
        if group_name:
            head += f" — {group_name}"
        lines.append(head + ".")

    if questions:
        lines.append("Уточню:")
        for q in questions[:3]:
            lines.append(f"- {q.get('question', '')}")
        lines.append("Ответьте одним сообщением — после него подберу код.")
    else:
        lines.append("Пришлите больше деталей — состав, назначение, форму, технические параметры.")
    return "\n".join(lines)


async def _chat_handle_user(chat_id: str, text: str, cfg: LLMConfig) -> dict:
    """Ход пользователя в чате. Как «один товар»: triage — только на первой реплике,
    вторая — ответ на вопросы, и сразу classify с группой и позициями первого triage.
    Не больше одного triage и одного classify на чат (B3: раньше triage шёл на каждом ходе
    по всему накопленному описанию — токены росли квадратично).
    """
    store = _require_store()
    chat = state.chats[chat_id]
    chat["messages"].append({"role": "user", "content": text})

    # Модель не ответила (503) — реплика остаётся в чате; повтор дописывается к ней, а не заменяет
    if chat["triage"] is None:
        chat["description"] = f"{chat['description']}\n{text}".strip()
        triage_res = await triage(store, chat["description"], cfg)
        chat["triage"] = triage_res
        if triage_res.get("completeness", "low") != "high" and triage_res.get("questions"):
            chat["messages"].append({
                "role": "assistant",
                "content": _chat_format_questions(triage_res),
            })
            return _chat_snapshot(chat)
        description = chat["description"]
    else:
        triage_res = chat["triage"]
        asked = [q.get("question", "") for q in (triage_res.get("questions") or [])[:3]]
        chat["answer"] = f"{chat.get('answer', '')}\n{text}".strip()
        description = merge_qa(chat["description"], [{"question": "; ".join(q for q in asked if q), "answer": chat["answer"]}])

    result = await classify(
        store,
        description=description,
        group_code=triage_res.get("group_code", ""),
        cfg=cfg,
        headings=triage_res.get("headings"),
    )
    chat["full_description"] = description
    chat["result"] = result
    chat["phase"] = "finalized"
    chat["messages"].append({
        "role": "assistant",
        "content": _chat_format_finalization(triage_res, result),
    })
    return _chat_snapshot(chat)


def _chat_snapshot(chat: dict) -> dict:
    return {
        "chat_id": chat["id"],
        "phase": chat["phase"],
        "messages": chat["messages"],
        "result": chat.get("result"),
    }


@app.post("/api/chat/start")
async def chat_start(
    req: ChatStartRequest,
):
    cfg = _llm()
    chat_id = uuid.uuid4().hex[:12]
    state.chats[chat_id] = {
        "id": chat_id,
        "created_at": datetime.utcnow().isoformat(),
        "messages": [],
        "description": "",
        "phase": "gathering",
        "llm_model": cfg.model,
        "triage": None,
        "result": None,
    }

    initial = req.initial_description.strip()
    if initial:
        return await _chat_handle_user(chat_id, initial, cfg)

    state.chats[chat_id]["messages"].append({
        "role": "assistant",
        "content": "Опишите товар — состав, назначение, форму, технические параметры. Я задам уточняющие вопросы и подберу код ТН ВЭД.",
    })
    return {
        "chat_id": chat_id,
        "phase": "gathering",
        "messages": state.chats[chat_id]["messages"],
        "result": None,
    }


@app.post("/api/chat/{chat_id}/message")
async def chat_message(
    chat_id: str,
    req: ChatMessageRequest,
):
    cfg = _llm()
    chat = state.chats.get(chat_id)
    if not chat:
        raise HTTPException(404, "Чат не найден")
    if chat["phase"] == "finalized":
        raise HTTPException(409, "Чат уже завершён — начните новый")
    if len(req.text) > MAX_DESCRIPTION_LEN:
        raise HTTPException(
            400,
            f"Сообщение слишком длинное ({len(req.text)} > {MAX_DESCRIPTION_LEN} символов)",
        )
    return await _chat_handle_user(chat_id, req.text, cfg)


@app.get("/api/chat/{chat_id}")
async def chat_get(chat_id: str):
    chat = state.chats.get(chat_id)
    if not chat:
        raise HTTPException(404, "Чат не найден")
    return chat


# ─── batch (xlsx in → xlsx out) ───────────────────────────────────────────────

# Роль колонки — по заголовку: правила по порядку, решает первое совпадение.
# Поэтому «Страна производства» — страна, «Наименование производителя» —
# производитель, «Part Number» и «Код товара» — артикул, «Стоимость товара» —
# прочее, а не описание. Слова ищем с начала слова: «origin» не найдётся
# в «Original description», «вес» — в «Весы».
# Колонку с кодом ТН ВЭД, тарифом или пошлиной не читаем: если ответ уже есть
# в файле, модели его показывать нельзя. «Commodity code» и «код вида товара»
# из счёта-фактуры — это тоже код ТН ВЭД.
BATCH_COLUMN_RULES = tuple((role, re.compile(pattern)) for role, pattern in (
    ("skip", r"\bтн[ -]?вэд|\btn[ -]?ved|\bht?s\b|\bh\.s\.|\bhs-?code|\bcommodity code|\bcustoms code"
             r"|\bтаможенн|\bкод вида товара|\bтариф|\btariff|\bпошлин|\bdut(?:y|ies)\b"),
    ("country", r"\bстран|\bcountry|\borigin(?!al)|\bпроисхожд|\bmade in\b|^coo$"),
    ("manufacturer", r"\bпроизводител|\bизготовител|\bбренд|\bтоварн\w* знак|\bмарка\b|\bbrand|\btrademark"
                     r"|\bmanufacturer|\bvendor|\bmaker\b|\bproducer|^mf[rg]$"),
    ("article", r"\bартикул|\bкаталожн|\bномер детали|\bкод (?:товара|изделия)|\bмодель|\bmodel"
                r"|\bpart ?(?:no\b|num|#)|\bp/n\b|\bsku\b|\barticle|\bitem ?(?:no\b|num|code|#)|\bproduct code"
                r"|^(?:pn|mpn|арт)$"),
    # номер по порядку, количество, деньги, вес, стороны сделки — модели не нужны
    ("other", r"^(?:№|#|no|nr|n|п/п|№ ?п/п|поз|pos|line)$|\bкол-?во\b|\bколич|\bцен[аы]\b|\bстоимост"
              r"|\bсумм|\bвес\b|\bмасс[аы]\b|\bнетто\b|\bбрутто\b|\bед\.? ?изм|\bединиц|\bвалют|\bитог|\bдата\b"
              r"|\bприм|\bсерийн|\bпоставщик|\bпродав|\bпокупател|\bзаказчик|\bqty\b|\bquantity|\bprice"
              r"|\bamount|\btotal|\bweight|\bvalue|\bcurrency|\buom\b|\bunits?\b|\bcosts?\b|\bdate\b|\bnotes?\b"
              r"|\bremarks?\b|\bcomments?\b|\bserial|\bsupplier|\bseller|\bshipper|\bbuyer|\bconsignee|\bcustomer"),
    ("description", r"\bописан|\bнаим|\bназван|\bтовар|\bпродукци|\bdescr|\bname|\bproduct|\bgoods|\bitem"
                    r"|\bcommodity"),
))
BATCH_FIELDS = ("description", "article", "manufacturer", "country")
BATCH_FIELD_LABELS = {"article": "Артикул", "manufacturer": "Производитель", "country": "Страна происхождения"}
BATCH_HEADER_SCAN_ROWS = 10
BATCH_HEADER_MAX_LEN = 80  # длиннее — это уже описание товара, а не заголовок


def _column_role(header: object) -> str | None:
    h = " ".join(str(header or "").lower().replace("ё", "е").split()).strip(" .:")
    if not h or len(h) > BATCH_HEADER_MAX_LEN:
        return None
    return next((role for role, rx in BATCH_COLUMN_RULES if rx.search(h)), None)


def _cell_text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # артикул 123456 Excel хранит как 123456.0
    return " ".join(str(value).split()) if value is not None else ""


def _read_batch_items(rows: list[tuple]) -> tuple[list[dict], dict]:
    """Строки xlsx → позиции для классификации и отчёт, какие колонки прочитаны.

    Заголовок — та из первых 10 строк, где узнаётся больше всего колонок, при
    равенстве — более широкая (строка с названием инвойса над таблицей его не
    перебьёт). Одна узнанная колонка — заголовок, только если это описание и
    строка шире всех над ней: иначе это товар, в описании которого попалось
    «товар», «модель» или «цена». Из одного «прочего» заголовок не собирается:
    «Валюта: USD | Итого: 5000» над таблицей — не он.

    Заголовка нет — описание из первой колонки, как раньше. Заголовок есть,
    а колонки описания нет — отказ: по одному артикулу и стране не классифицируем.

    Номер строки — как в Excel, чтобы результат сводился с исходным файлом.
    Пустые строки пропускаем, строку без описания не выбрасываем, а помечаем.
    """
    header_idx, best, widest = None, (0, 0), 0
    for i, row in enumerate(rows[:BATCH_HEADER_SCAN_ROWS]):
        filled = [c for c in row or () if _cell_text(c)]
        roles = [r for r in map(_column_role, filled) if r]
        if roles == ["description"]:
            ok = len(filled) > widest
        else:
            ok = len(roles) >= 2 and any(r != "other" for r in roles)
        if ok and (len(roles), len(filled)) > best:
            best, header_idx = (len(roles), len(filled)), i
        widest = max(widest, len(filled))

    columns: dict = {"header_row": None, "unused": [], **{f: [] for f in BATCH_FIELDS}}
    if header_idx is None:
        cols = {0: "description"}
        data_start = 0
    else:
        header = rows[header_idx]
        cols = {}
        for i, cell in enumerate(header):
            name, role = _cell_text(cell), _column_role(cell)
            if role in BATCH_FIELDS:
                cols[i] = role
                columns[role].append(name)
            elif name:
                columns["unused"].append(name)
        columns["header_row"] = header_idx + 1
        if not columns["description"]:
            raise ValueError(
                f"В строке {header_idx + 1} заголовок ({', '.join(filter(None, map(_cell_text, header)))}), "
                f"но нет колонки с описанием товара — назовите её «Описание», «Наименование» или «Description»"
            )
        data_start = header_idx + 1

    items = []
    for n, row in enumerate(rows[data_start:], start=data_start + 1):
        row = row or ()
        if not any(_cell_text(c) for c in row):
            continue
        item: dict = {"row": n, **{f: [] for f in BATCH_FIELDS}}
        for i, role in cols.items():
            value = _cell_text(row[i]) if i < len(row) else ""
            if value and value not in item[role]:
                item[role].append(value)
        item.update({f: "; ".join(item[f]) for f in BATCH_FIELDS})
        item["text"] = "\n".join(
            [item["description"]]
            + [f"{label}: {item[f]}" for f, label in BATCH_FIELD_LABELS.items() if item[f]]
        )
        if not item["description"]:
            item["error"] = "нет описания товара"
        elif len(item["text"]) > MAX_DESCRIPTION_LEN:
            item["error"] = f"описание длиннее {MAX_DESCRIPTION_LEN} символов"
        items.append(item)
    return items, columns


async def _classify_one_for_batch(description: str, cfg: LLMConfig) -> dict:
    """В батче пропускаем уточнения: триаж → классификация на исходном описании."""
    store = _require_store()
    triage_result = await triage(store, description, cfg)
    result = await classify(store, description, triage_result.get("group_code", ""), cfg=cfg,
                            headings=triage_result.get("headings"))
    return {
        "group_code": result["group_code"],
        "group_name": result["group_name"],
        "primary": result.get("primary", {}),
        "alternatives": result.get("alternatives", [])[:2],
    }


async def _run_batch(job_id: str, items: list[dict], cfg: LLMConfig) -> None:
    job = state.batch_jobs[job_id]
    sem = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def worker(idx: int, item: dict) -> None:
        if item.get("error"):  # строка без описания или слишком длинная — к модели не идёт
            job["results"][idx] = {"error": item["error"]}
            job["errors"].append({"row": item["row"], "error": item["error"]})
            job["processed"] += 1
            return
        async with sem:
            try:
                job["results"][idx] = await _classify_one_for_batch(item["text"], cfg)
            except Exception as e:
                job["results"][idx] = {"error": str(e)}
                job["errors"].append({"row": item["row"], "error": str(e)})
            finally:
                job["processed"] += 1

    try:
        await asyncio.gather(*(worker(i, item) for i, item in enumerate(items)))
        job["status"] = "done"
    except Exception as e:
        job["status"] = "failed"
        job["errors"].append({"row": 0, "error": f"job-level: {e}"})
    finally:
        job["finished_at"] = datetime.utcnow().isoformat()


@app.post("/api/classify/batch")
async def classify_batch_start(
    file: UploadFile = File(...),
):
    _require_store()
    cfg = _llm()
    fname = (file.filename or "").lower()
    if not fname.endswith(".xlsx"):
        raise HTTPException(400, "Ожидается .xlsx (Excel)")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 10 МБ — слишком много")
    try:
        wb = load_workbook(filename=BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
        ws.reset_dimensions()  # размер листа в файле бывает записан неверно — тогда читались бы не все колонки
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        raise HTTPException(400, f"Не смог прочитать xlsx: {e}")

    try:
        items, columns = _read_batch_items(rows)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not any("error" not in item for item in items):
        raise HTTPException(400, "В xlsx не нашёл строк с описаниями товаров")
    if len(items) > BATCH_MAX_ROWS:
        raise HTTPException(400, f"Слишком много строк: {len(items)} > лимит {BATCH_MAX_ROWS}")

    job_id = uuid.uuid4().hex[:12]
    state.batch_jobs[job_id] = {
        "id": job_id,
        "filename": file.filename,
        "total": len(items),
        "processed": 0,
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "llm_model": cfg.model,
        "columns": columns,
        "items": items,
        "results": [None] * len(items),
        "errors": [],
    }

    asyncio.create_task(_run_batch(job_id, items, cfg))

    return {"job_id": job_id, "total": len(items), "status": "running", "columns": columns}


@app.get("/api/classify/batch/{job_id}")
async def classify_batch_status(job_id: str):
    job = state.batch_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job не найден")
    return {
        "job_id": job["id"],
        "filename": job["filename"],
        "total": job["total"],
        "processed": job["processed"],
        "status": job["status"],
        "errors_count": len(job["errors"]),
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }


@app.get("/api/classify/batch/{job_id}/download")
async def classify_batch_download(job_id: str):
    job = state.batch_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job не найден")
    if job["status"] not in ("done", "failed"):
        raise HTTPException(409, f"Job ещё не завершён ({job['processed']}/{job['total']})")

    wb = Workbook()
    ws = wb.active
    ws.title = "Результат"
    ws.append([
        "Строка файла", "Описание", "Артикул", "Производитель", "Страна",
        "Код ТН ВЭД", "Наименование", "Пошлина", "Уверенность",
        "Группа", "Альт. 1", "Альт. 1 пошлина", "Альт. 2", "Альт. 2 пошлина", "Ошибка",
    ])
    for item, r in zip(job["items"], job["results"]):
        source = [item["row"], item["description"], item["article"], item["manufacturer"], item["country"]]
        if r is None:
            ws.append(source + [""] * 9 + ["обработка прервана"])
            continue
        if "error" in r:
            ws.append(source + [""] * 9 + [r["error"]])
            continue
        primary = r.get("primary") or {}
        alts = r.get("alternatives") or []
        a1 = alts[0] if len(alts) > 0 else {}
        a2 = alts[1] if len(alts) > 1 else {}
        ws.append([
            *source,
            primary.get("code", ""),
            primary.get("full_path") or "",
            primary.get("duty_rate") or "",
            primary.get("confidence", ""),
            f'{r.get("group_code", "")} {r.get("group_name", "")}'.strip(),
            a1.get("code", ""),
            a1.get("duty_rate") or "",
            a2.get("code", ""),
            a2.get("duty_rate") or "",
            f"код не выдан: {primary['rejected']}" if primary.get("rejected") else "",
        ])

    bio = BytesIO()
    wb.save(bio)
    bio.seek(0)

    fname = f"tnved_batch_{job_id}.xlsx"
    return StreamingResponse(
        bio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ─── статика ──────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    """Отдаёт index.html с подменой `?v=<STATIC_VER>` у JS/CSS — bust browser-кеша."""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/app.css"', f'href="/static/app.css?v={STATIC_VER}"')
    html = html.replace('src="/static/app.js"', f'src="/static/app.js?v={STATIC_VER}"')
    return Response(content=html, media_type="text/html; charset=utf-8")
