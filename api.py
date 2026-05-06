"""
FastAPI-бэкенд: двухстадийный пайплайн классификации ТН ВЭД.

Запуск: venv/bin/uvicorn api:app --host 127.0.0.1 --port 8765 --reload
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook, load_workbook
from pydantic import BaseModel, Field

from classifier import classify, merge_qa, normalize_input, triage
from tnved_data import TNVEDStore

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
DEFAULT_LLM = os.environ.get("LLM_MODEL", "gpt-4o-mini")
BATCH_CONCURRENCY = int(os.environ.get("BATCH_CONCURRENCY", "5"))
BATCH_MAX_ROWS = int(os.environ.get("BATCH_MAX_ROWS", "500"))

# ─── глобальные ресурсы ───────────────────────────────────────────────────────

class App:
    store: TNVEDStore | None = None
    sessions: dict[str, dict] = {}
    batch_jobs: dict[str, dict] = {}


state = App()


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Загружаем ресурсы ТН ВЭД...")
    state.store = TNVEDStore()
    print(f"Готово. Векторов: {state.store.index.ntotal:,}, групп: {len(state.store.groups)}")
    yield


app = FastAPI(lifespan=lifespan, title="ТН ВЭД Ассистент")


# ─── схемы ────────────────────────────────────────────────────────────────────

class StartRequest(BaseModel):
    mode: str = Field("simple", pattern="^(simple|detailed)$")
    description: Optional[str] = None
    fields: Optional[dict] = None
    model: str = DEFAULT_LLM


class AnswerItem(BaseModel):
    id: str
    question: str
    answer: str


class FinalizeRequest(BaseModel):
    session_id: str
    answers: list[AnswerItem] = []
    model: str = DEFAULT_LLM


class SkipRequest(BaseModel):
    session_id: str
    model: str = DEFAULT_LLM


# ─── endpoints ────────────────────────────────────────────────────────────────

@app.post("/api/classify/start")
async def classify_start(req: StartRequest):
    description = normalize_input(req.description, req.fields)
    if not description:
        raise HTTPException(400, "Пустое описание товара")

    triage_result = await triage(state.store, description, req.model)

    session_id = uuid.uuid4().hex[:12]
    state.sessions[session_id] = {
        "id": session_id,
        "created_at": datetime.utcnow().isoformat(),
        "mode": req.mode,
        "description": description,
        "model": req.model,
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
async def classify_finalize(req: FinalizeRequest):
    session = state.sessions.get(req.session_id)
    if not session:
        raise HTTPException(404, "Сессия не найдена")

    description = merge_qa(
        session["description"],
        [a.model_dump() for a in req.answers],
    )
    group_code = session["triage"].get("group_code", "")

    result = await classify(
        state.store,
        description=description,
        group_code=group_code,
        model=req.model,
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
    return {"models": [DEFAULT_LLM], "default": DEFAULT_LLM}


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


async def _classify_one_for_batch(description: str, model: str) -> dict:
    """В батче пропускаем уточнения: триаж → классификация на исходном описании."""
    triage_result = await triage(state.store, description, model)
    group_code = triage_result.get("group_code", "")
    result = await classify(state.store, description, group_code, model)
    return {
        "group_code": group_code,
        "group_name": triage_result.get("group_name", ""),
        "primary": result.get("primary", {}),
        "alternatives": result.get("alternatives", [])[:2],
    }


async def _run_batch(job_id: str, descriptions: list[str], model: str) -> None:
    job = state.batch_jobs[job_id]
    sem = asyncio.Semaphore(BATCH_CONCURRENCY)

    async def worker(idx: int, desc: str) -> None:
        async with sem:
            try:
                row = await _classify_one_for_batch(desc, model)
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
    model: str = Form(DEFAULT_LLM),
):
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
        "model": model,
        "results": [None] * len(descriptions),
        "errors": [],
    }

    asyncio.create_task(_run_batch(job_id, descriptions, model))

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
    return FileResponse(STATIC_DIR / "index.html")
