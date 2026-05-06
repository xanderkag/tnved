"""
FastAPI-бэкенд: двухстадийный пайплайн классификации ТН ВЭД.

Запуск: venv/bin/uvicorn api:app --host 127.0.0.1 --port 8765 --reload
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

import ollama
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from classifier import classify, merge_qa, normalize_input, triage
from tnved_data import TNVEDStore

BASE = Path(__file__).parent
STATIC_DIR = BASE / "static"
DEFAULT_LLM = "qwen2.5:14b"

# ─── глобальные ресурсы ───────────────────────────────────────────────────────

class App:
    store: TNVEDStore | None = None
    sessions: dict[str, dict] = {}


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
    try:
        client = ollama.AsyncClient()
        result = await client.list()
        names = [m.model for m in result.models]
        return {"models": names, "default": DEFAULT_LLM}
    except Exception as e:
        return {"models": [DEFAULT_LLM], "default": DEFAULT_LLM, "error": str(e)}


# ─── статика ──────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
