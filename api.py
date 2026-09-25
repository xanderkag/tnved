"""
FastAPI-бэкенд: двухстадийный пайплайн классификации ТН ВЭД.

Запуск: venv/bin/uvicorn api:app --host 127.0.0.1 --port 8765 --reload
"""

from __future__ import annotations

import asyncio
import hashlib
import os
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
from tnved_data import TNVEDStore

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
BATCH_CONCURRENCY = int(os.environ.get("BATCH_CONCURRENCY", "5"))
BATCH_MAX_ROWS = int(os.environ.get("BATCH_MAX_ROWS", "500"))
CHAT_MAX_TURNS = int(os.environ.get("CHAT_MAX_TURNS", "6"))
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
    )

    session["answers"] = [a.model_dump() for a in req.answers]
    session["full_description"] = description
    session["result"] = result
    session["finalized_at"] = datetime.utcnow().isoformat()

    return {
        "session_id": req.session_id,
        "description": description,
        "group": {
            "code": group_code,
            "name": session["triage"].get("group_name", ""),
        },
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
    else:
        lines.append("Пришлите больше деталей — состав, назначение, форму, технические параметры.")
    return "\n".join(lines)


async def _chat_handle_user(chat_id: str, text: str, cfg: LLMConfig) -> dict:
    store = _require_store()
    chat = state.chats[chat_id]
    chat["messages"].append({"role": "user", "content": text})
    chat["description"] = (chat["description"] + "\n" + text).strip() if chat["description"] else text

    triage_res = await triage(store, chat["description"], cfg)

    user_turns = sum(1 for m in chat["messages"] if m["role"] == "user")
    completeness = triage_res.get("completeness", "low")
    questions = triage_res.get("questions", [])

    finalize = (
        completeness == "high"
        or not questions
        or user_turns >= CHAT_MAX_TURNS
    )

    if finalize:
        result = await classify(
            store,
            description=chat["description"],
            group_code=triage_res.get("group_code", ""),
            cfg=cfg,
        )
        chat["triage"] = triage_res
        chat["result"] = result
        chat["phase"] = "finalized"
        chat["messages"].append({
            "role": "assistant",
            "content": _chat_format_finalization(triage_res, result),
        })
    else:
        chat["triage"] = triage_res
        chat["messages"].append({
            "role": "assistant",
            "content": _chat_format_questions(triage_res),
        })

    return {
        "chat_id": chat_id,
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

DESC_COL_KEYWORDS = ("описан", "наим", "название", "товар", "descr", "name", "product")


def _detect_descriptions(rows: list[tuple]) -> list[str]:
    """Извлекает описания из xlsx-строк. Если есть строка-заголовок с ключевым словом — берёт ту колонку, иначе — первую непустую."""
    if not rows:
        return []
    header = [str(c).lower().strip() if c is not None else "" for c in rows[0]]
    col_idx = next(
        (i for i, h in enumerate(header) if any(k in h for k in DESC_COL_KEYWORDS)),
        None,
    )
    if col_idx is not None:
        data_rows = rows[1:]
    else:
        col_idx = 0
        data_rows = rows
    out = []
    for row in data_rows:
        if not row or len(row) <= col_idx:
            continue
        val = row[col_idx]
        if val is None:
            continue
        s = str(val).strip()
        if s:
            out.append(s)
    return out


async def _classify_one_for_batch(description: str, cfg: LLMConfig) -> dict:
    """В батче пропускаем уточнения: триаж → классификация на исходном описании."""
    store = _require_store()
    triage_result = await triage(store, description, cfg)
    group_code = triage_result.get("group_code", "")
    result = await classify(store, description, group_code, cfg=cfg)
    return {
        "group_code": group_code,
        "group_name": triage_result.get("group_name", ""),
        "primary": result.get("primary", {}),
        "alternatives": result.get("alternatives", [])[:2],
    }


async def _run_batch(job_id: str, descriptions: list[str], cfg: LLMConfig) -> None:
    job = state.batch_jobs[job_id]
    sem = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def worker(idx: int, desc: str) -> None:
        async with sem:
            try:
                row = await _classify_one_for_batch(desc, cfg)
                row["description"] = desc
                job["results"][idx] = row
            except Exception as e:
                job["results"][idx] = {"description": desc, "error": str(e)}
                job["errors"].append({"row": idx + 1, "error": str(e)})
            finally:
                job["processed"] += 1

    try:
        await asyncio.gather(*(worker(i, d) for i, d in enumerate(descriptions)))
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
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:
        raise HTTPException(400, f"Не смог прочитать xlsx: {e}")

    descriptions = _detect_descriptions(rows)
    if not descriptions:
        raise HTTPException(400, "В xlsx не нашёл строк с описаниями товаров")
    if len(descriptions) > BATCH_MAX_ROWS:
        raise HTTPException(400, f"Слишком много строк: {len(descriptions)} > лимит {BATCH_MAX_ROWS}")

    job_id = uuid.uuid4().hex[:12]
    state.batch_jobs[job_id] = {
        "id": job_id,
        "filename": file.filename,
        "total": len(descriptions),
        "processed": 0,
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "llm_model": cfg.model,
        "results": [None] * len(descriptions),
        "errors": [],
    }

    asyncio.create_task(_run_batch(job_id, descriptions, cfg))

    return {"job_id": job_id, "total": len(descriptions), "status": "running"}


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
        "№", "Описание", "Код ТН ВЭД", "Наименование", "Пошлина", "Уверенность",
        "Группа", "Альт. 1", "Альт. 1 пошлина", "Альт. 2", "Альт. 2 пошлина", "Ошибка",
    ])
    for i, r in enumerate(job["results"], start=1):
        if r is None:
            ws.append([i, "", "", "", "", "", "", "", "", "", "", "обработка прервана"])
            continue
        if "error" in r:
            ws.append([i, r["description"], "", "", "", "", "", "", "", "", "", r["error"]])
            continue
        primary = r.get("primary") or {}
        alts = r.get("alternatives") or []
        a1 = alts[0] if len(alts) > 0 else {}
        a2 = alts[1] if len(alts) > 1 else {}
        ws.append([
            i,
            r["description"],
            primary.get("code", ""),
            primary.get("full_path") or "",
            primary.get("duty_rate") or "",
            primary.get("confidence", ""),
            f'{r.get("group_code", "")} {r.get("group_name", "")}'.strip(),
            a1.get("code", ""),
            a1.get("duty_rate") or "",
            a2.get("code", ""),
            a2.get("duty_rate") or "",
            "",
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
