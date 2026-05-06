"""
Streamlit-ассистент для определения кода ТН ВЭД.
Поддерживает диалог с уточняющими вопросами при неоднозначной классификации.

Запуск: streamlit run app.py --server.port 8765
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import faiss
import numpy as np
import ollama
import streamlit as st
from sentence_transformers import SentenceTransformer

# ─── пути ─────────────────────────────────────────────────────────────────────

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "tnved.db"
FAISS_PATH = BASE / "data" / "tnved.faiss"
META_PATH = BASE / "data" / "tnved_meta.json"

MODEL_NAME = "intfloat/multilingual-e5-base"
DEFAULT_LLM = "qwen2.5:14b"
TOP_K = 12

# ─── кэш ──────────────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner="Загружаем модель embeddings...")
def load_embedder() -> SentenceTransformer:
    return SentenceTransformer(MODEL_NAME)


@st.cache_resource(show_spinner="Загружаем FAISS-индекс...")
def load_index():
    index = faiss.read_index(str(FAISS_PATH))
    with open(META_PATH, encoding="utf-8") as f:
        meta = json.load(f)
    return index, meta


# ─── поиск ────────────────────────────────────────────────────────────────────

def search(query: str, top_k: int = TOP_K) -> list[dict]:
    embedder = load_embedder()
    index, meta = load_index()
    vec = embedder.encode(
        [f"query: {query}"],
        normalize_embeddings=True,
        convert_to_numpy=True,
    ).astype("float32")
    scores, ids = index.search(vec, top_k)
    return [
        {**meta[idx], "score": float(score)}
        for score, idx in zip(scores[0], ids[0])
        if idx >= 0
    ]


# ─── промпты ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты эксперт по классификации товаров по ТН ВЭД ЕАЭС.

Тебе дают описание товара и список кандидатов из справочника ТН ВЭД.

Правила:
1. Если описания достаточно — сразу давай ответ:
   КОД: <10-значный код>
   ОБОСНОВАНИЕ: <2-3 предложения>
   УВЕРЕННОСТЬ: <высокая / средняя / низкая>

2. Если не хватает информации — задай ОДИН конкретный вопрос:
   ВОПРОС: <вопрос>

3. Каждый следующий вопрос должен касаться ДРУГОГО аспекта — не повторяй уже заданные вопросы.
   Если ответ на предыдущий вопрос получен — обязательно его учти и либо выдай КОД, либо спроси про другой аспект.

4. После 2 вопросов — обязательно выдавай КОД, даже если уверенность средняя.

Формат ответа — строго одно из двух: начинается с "ВОПРОС:" или с "КОД:". Никакого вводного текста до метки."""


def build_first_message(query: str, candidates: list[dict]) -> str:
    lines = [f"Описание товара: {query}\n", "Кандидаты из справочника ТН ВЭД:"]
    for i, c in enumerate(candidates, 1):
        lines.append(f"  {i}. [{c['code']}] {c['full_path'] or c['description']}")
    return "\n".join(lines)


def chat_stream(messages: list[dict], llm_model: str):
    stream = ollama.chat(
        model=llm_model,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        stream=True,
    )
    for chunk in stream:
        content = chunk.get("message", {}).get("content", "")
        if content:
            yield content


def is_question(text: str) -> bool:
    return text.strip().startswith("ВОПРОС:")


def format_response(text: str) -> str:
    """Красиво оформляем финальный ответ."""
    result = {}
    for line in text.splitlines():
        if line.startswith("КОД:"):
            result["code"] = line.replace("КОД:", "").strip()
        elif line.startswith("ОБОСНОВАНИЕ:"):
            result["reason"] = line.replace("ОБОСНОВАНИЕ:", "").strip()
        elif line.startswith("УВЕРЕННОСТЬ:"):
            result["confidence"] = line.replace("УВЕРЕННОСТЬ:", "").strip()

    if "code" in result:
        conf_emoji = {"высокая": "🟢", "средняя": "🟡", "низкая": "🔴"}.get(
            result.get("confidence", "").lower(), "⚪"
        )
        return (
            f"### Код ТН ВЭД: `{result['code']}`\n\n"
            f"{result.get('reason', '')}\n\n"
            f"{conf_emoji} Уверенность: **{result.get('confidence', '—')}**"
        )
    return text


# ─── проверка готовности ───────────────────────────────────────────────────────

def check_readiness() -> list[str]:
    issues = []
    if not DB_PATH.exists():
        issues.append(f"`{DB_PATH.name}` не найден → запустите `python parse_tnved.py`")
    if not FAISS_PATH.exists():
        issues.append(f"`{FAISS_PATH.name}` не найден → запустите `python build_index.py`")
    if not META_PATH.exists():
        issues.append(f"`{META_PATH.name}` не найден → запустите `python build_index.py`")
    return issues


# ─── UI ───────────────────────────────────────────────────────────────────────

def reset():
    st.session_state.messages = []
    st.session_state.candidates = []
    st.session_state.stage = "input"
    st.session_state.query = ""


def main():
    st.set_page_config(page_title="ТН ВЭД Ассистент", page_icon="🔍", layout="wide")

    # Инициализация состояния
    if "stage" not in st.session_state:
        reset()

    # Сайдбар
    with st.sidebar:
        st.header("Настройки")
        llm_model = st.text_input("Модель Ollama", value=DEFAULT_LLM)
        top_k = st.slider("Кандидатов из индекса", 5, 20, TOP_K)
        show_candidates = st.checkbox("Показывать кандидатов", value=False)
        st.divider()
        if st.button("🔄 Новый запрос", use_container_width=True):
            reset()
            st.rerun()

    st.title("🔍 Ассистент по ТН ВЭД")
    st.caption("Определение кода Товарной номенклатуры ВЭД ЕАЭС")

    issues = check_readiness()
    if issues:
        st.error("Система не готова:")
        for i in issues:
            st.markdown(f"- {i}")
        st.stop()

    # ── Начальный ввод ────────────────────────────────────────────────────────
    if st.session_state.stage == "input":
        with st.form("query_form"):
            query = st.text_area(
                "Описание товара",
                placeholder="Например: хлопковая ткань суровая, плотность 150 г/м², ширина 150 см",
                height=100,
            )
            submitted = st.form_submit_button("Определить код", type="primary")

        if submitted and query.strip():
            st.session_state.query = query.strip()
            with st.spinner("Ищем кандидатов..."):
                st.session_state.candidates = search(query.strip(), top_k)

            first_msg = build_first_message(query.strip(), st.session_state.candidates)
            st.session_state.messages = [{"role": "user", "content": first_msg}]
            st.session_state.stage = "thinking"
            st.rerun()

    # ── Показываем историю диалога ────────────────────────────────────────────
    if st.session_state.stage in ("thinking", "clarifying", "done"):
        st.markdown(f"**Товар:** {st.session_state.query}")

        if show_candidates and st.session_state.candidates:
            with st.expander(f"Найдено кандидатов: {len(st.session_state.candidates)}"):
                for c in st.session_state.candidates:
                    st.markdown(
                        f"**`{c['code']}`** — {c['description']}  \n"
                        f"<small>{c.get('full_path', '')} | сходство: {int(c['score']*100)}%</small>",
                        unsafe_allow_html=True,
                    )
                    st.divider()

        # Показываем предыдущие вопросы и ответы (пропускаем первое user-сообщение с кандидатами)
        history = st.session_state.messages[1:]  # без первого технического сообщения
        for msg in history:
            if msg["role"] == "assistant":
                if is_question(msg["content"]):
                    with st.chat_message("assistant"):
                        st.markdown(msg["content"].replace("ВОПРОС:", "**Уточняющий вопрос:**"))
                else:
                    with st.chat_message("assistant"):
                        st.markdown(format_response(msg["content"]))
            elif msg["role"] == "user":
                with st.chat_message("user"):
                    st.markdown(msg["content"])

    # ── LLM генерирует ответ ──────────────────────────────────────────────────
    if st.session_state.stage == "thinking":
        with st.chat_message("assistant"):
            collected = []
            placeholder = st.empty()
            try:
                for chunk in chat_stream(st.session_state.messages, llm_model):
                    collected.append(chunk)
                    placeholder.markdown("".join(collected) + "▌")
                response = "".join(collected)
                placeholder.empty()

                st.session_state.messages.append({"role": "assistant", "content": response})

                if is_question(response):
                    st.markdown(response.replace("ВОПРОС:", "**Уточняющий вопрос:**"))
                    st.session_state.stage = "clarifying"
                elif response.strip().startswith("КОД:"):
                    st.markdown(format_response(response))
                    st.session_state.stage = "done"
                else:
                    # LLM нарушила формат — показываем как есть и завершаем
                    st.warning("Не удалось распознать формат ответа. Попробуйте переформулировать запрос.")
                    st.markdown(response)
                    st.session_state.stage = "done"

            except Exception as e:
                st.error(f"Ошибка Ollama: {e}")
                st.info(f"Убедитесь что Ollama запущена и модель `{llm_model}` скачана.")
                st.session_state.stage = "input"

        st.rerun()

    # ── Ввод ответа на уточняющий вопрос ─────────────────────────────────────
    if st.session_state.stage == "clarifying":
        if "clarify_input" not in st.session_state:
            st.session_state.clarify_input = ""

        with st.form("clarify_form", clear_on_submit=True):
            answer = st.text_input("Ваш ответ:", value="")
            submitted = st.form_submit_button("Отправить", type="primary")

        if submitted and answer.strip():
            st.session_state.messages.append({"role": "user", "content": answer.strip()})
            st.session_state.stage = "thinking"
            st.rerun()

    # ── Финал: кнопка нового запроса ─────────────────────────────────────────
    if st.session_state.stage == "done":
        st.divider()
        if st.button("🔄 Новый запрос"):
            reset()
            st.rerun()


if __name__ == "__main__":
    main()
