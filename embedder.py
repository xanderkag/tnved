"""
Векторизация текстов — два взаимозаменяемых бэкенда.

**api** (основной): OpenAI-совместимый `POST /v1/embeddings`. Модель `bge-m3`
крутится на GPU-сервере, считает минутами вместо часов и не требует ни torch,
ни 1,1 ГБ модели внутри образа.

**local** (запасной): sentence-transformers с `intfloat/multilingual-e5-base`
на CPU. Медленно (3–6 текстов/с) и на слабых хостах падает по памяти,
но не зависит от внешнего сервиса.

Бэкенд выбирается сам: если задан `EMBEDDINGS_BASE_URL` — берём api, иначе local.
Явно переключить — `EMBEDDER_BACKEND=api|local`.

Переменные окружения для api:
    EMBEDDINGS_BASE_URL   адрес шлюза, например http://10.10.13.10:8085/v1
    EMBEDDINGS_API_KEY    наш named-key
    EMBEDDINGS_MODEL      имя модели, по умолчанию bge-m3
    EMBEDDINGS_BATCH      сколько текстов в одном запросе, по умолчанию 64

ВАЖНО: у бэкендов разная размерность вектора (bge-m3 — 1024, e5-base — 768)
и разные требования к префиксам (e5 нужны «passage: » / «query: », bge-m3 — нет).
Индекс, собранный одним бэкендом, непригоден для поиска другим. Поэтому рядом
с индексом пишется `data/tnved_index_info.json`, а `TNVEDStore` при загрузке
сверяет его с активным бэкендом и падает с внятной ошибкой при расхождении.
"""

from __future__ import annotations

import os
import time
from typing import Protocol

import numpy as np

DEFAULT_API_MODEL = "bge-m3"
DEFAULT_LOCAL_MODEL = "intfloat/multilingual-e5-base"


class Embedder(Protocol):
    name: str

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        """Возвращает float32-матрицу (len(texts), dim), строки L2-нормированы."""


def _normalize(vecs: np.ndarray) -> np.ndarray:
    """L2-нормировка — чтобы косинусное сходство считалось через inner product.

    Нормируем сами, а не полагаемся на сервер: разные сборки vLLM/TEI отдают
    и нормированные, и ненормированные векторы.
    """
    vecs = vecs.astype("float32", copy=False)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


class ApiEmbedder:
    """Векторизация через OpenAI-совместимый /v1/embeddings."""

    def __init__(self) -> None:
        from openai import OpenAI

        base_url = os.environ.get("EMBEDDINGS_BASE_URL", "").strip()
        if not base_url:
            raise RuntimeError(
                "EMBEDDINGS_BASE_URL не задан — не знаю, куда обращаться за векторами."
            )
        self.model = os.environ.get("EMBEDDINGS_MODEL", DEFAULT_API_MODEL)
        self.batch = int(os.environ.get("EMBEDDINGS_BATCH", "64"))
        self.name = f"api:{self.model}"
        self._client = OpenAI(
            base_url=base_url,
            api_key=os.environ.get("EMBEDDINGS_API_KEY", "") or "EMPTY",
            timeout=float(os.environ.get("EMBEDDINGS_TIMEOUT", "120")),
            max_retries=3,
        )

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        # bge-m3 обучен без инструкций-префиксов — добавлять их нельзя, ухудшит.
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            chunk = texts[start:start + self.batch]
            resp = self._retry(chunk)
            # порядок в ответе не гарантирован спецификацией — раскладываем по index
            ordered = sorted(resp.data, key=lambda d: d.index)
            out.extend(d.embedding for d in ordered)
        return _normalize(np.asarray(out, dtype="float32"))

    def _retry(self, chunk: list[str], attempts: int = 3):
        last: Exception | None = None
        for n in range(attempts):
            try:
                return self._client.embeddings.create(model=self.model, input=chunk)
            except Exception as exc:  # сеть/503 — подождём и повторим
                last = exc
                if n + 1 < attempts:
                    time.sleep(2 * (n + 1))
        raise RuntimeError(f"Не удалось получить векторы после {attempts} попыток: {last}")


class LocalEmbedder:
    """Векторизация локальной моделью e5 (запасной путь)."""

    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer

        self.model_name = os.environ.get("EMBEDDINGS_MODEL", DEFAULT_LOCAL_MODEL)
        self.batch = int(os.environ.get("EMBEDDINGS_BATCH", "32"))
        self.name = f"local:{self.model_name}"
        self._model = SentenceTransformer(self.model_name)

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        # e5 без префиксов теряет качество: индексируемое — passage, запрос — query.
        prefix = "query: " if is_query else "passage: "
        vecs = self._model.encode(
            [prefix + t for t in texts],
            batch_size=self.batch,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return vecs.astype("float32")


def active_backend() -> str:
    explicit = os.environ.get("EMBEDDER_BACKEND", "").strip().lower()
    if explicit in ("api", "local"):
        return explicit
    return "api" if os.environ.get("EMBEDDINGS_BASE_URL", "").strip() else "local"


def get_embedder() -> Embedder:
    backend = active_backend()
    return ApiEmbedder() if backend == "api" else LocalEmbedder()


def expected_name() -> str:
    """Имя бэкенда без его инициализации — для сверки с сохранённым индексом."""
    if active_backend() == "api":
        return f"api:{os.environ.get('EMBEDDINGS_MODEL', DEFAULT_API_MODEL)}"
    return f"local:{os.environ.get('EMBEDDINGS_MODEL', DEFAULT_LOCAL_MODEL)}"
