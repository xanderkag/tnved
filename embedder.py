"""
Векторизация текстов — два бэкенда.

**api** (рабочий): OpenAI-совместимый `POST /v1/embeddings`. Модель `bge-m3`
крутится на нашем GPU-сервере, считает минутами вместо часов и не требует
ни torch, ни 1,1 ГБ модели внутри образа.

**local** (запасной): sentence-transformers с `intfloat/multilingual-e5-base`
на CPU. Медленно (3–6 текстов/с) и на слабых хостах падает по памяти.
Только явно — `EMBEDDER_BACKEND=local` — и после
`pip install -r requirements-e5.txt`: в образ sentence-transformers и torch не входят.

Бэкенд по умолчанию — api, и адрес у него обязателен: без `EMBEDDINGS_BASE_URL`
отказ. Раньше без адреса молча включалась e5 — в контейнере это скачивание
модели с HuggingFace и индекс bge-m3 при запросах e5 (спасала только сверка
паспорта индекса). Непонятное `EMBEDDER_BACKEND` — тоже отказ.

Переменные окружения для api (пустая — как незаданная):
    EMBEDDINGS_BASE_URL   адрес во внутренней сети, на kb-docker — см. .env.example
    EMBEDDINGS_API_KEY    ключ, если сервер его требует (Ollama — без ключа)
    EMBEDDINGS_MODEL      имя модели, по умолчанию bge-m3
    EMBEDDINGS_BATCH      сколько текстов в одном запросе, по умолчанию 64
    EMBEDDINGS_TIMEOUT    секунд на запрос, по умолчанию 30; попыток 3, потом отказ

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

from netcheck import require_internal_url

DEFAULT_API_MODEL = "bge-m3"
DEFAULT_LOCAL_MODEL = "intfloat/multilingual-e5-base"


class EmbeddingsUnavailable(RuntimeError):
    """Сервер векторов не ответил. На локальную e5 не переключаемся — это отказ."""


def _env(name: str, default: str) -> str:
    """Пустая переменная — как незаданная: compose передаёт `${X:-}` пустой строкой."""
    return os.environ.get(name, "").strip() or default


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
                "EMBEDDINGS_BASE_URL не задан — не знаю, куда обращаться за векторами. "
                "Локальная e5 — только явно: EMBEDDER_BACKEND=local"
            )
        # Сюда уходит описание товара (вектор запроса) — только во внутреннюю сеть.
        base_url = require_internal_url(base_url, "EMBEDDINGS_BASE_URL")
        self.model = _env("EMBEDDINGS_MODEL", DEFAULT_API_MODEL)
        self.batch = int(_env("EMBEDDINGS_BATCH", "64"))
        self.name = f"api:{self.model}"
        self._client = OpenAI(
            base_url=base_url,
            api_key=_env("EMBEDDINGS_API_KEY", "EMPTY"),
            timeout=float(_env("EMBEDDINGS_TIMEOUT", "30")),
            # Повторяем сами, в _retry. Повторы клиента поверх наших давали до 12
            # попыток по 120 с — запрос при лежащем сервере висел до получаса.
            max_retries=0,
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
        raise EmbeddingsUnavailable(
            f"Сервер векторов {self.model} не ответил за {attempts} попытки: {last}"
        )


class LocalEmbedder:
    """Векторизация локальной моделью e5 (запасной путь, только явно)."""

    def __init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "EMBEDDER_BACKEND=local: нужен sentence-transformers — "
                "pip install -r requirements-e5.txt (в образ он не входит)"
            ) from exc

        self.model_name = _env("EMBEDDINGS_MODEL", DEFAULT_LOCAL_MODEL)
        self.batch = int(_env("EMBEDDINGS_BATCH", "32"))
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
    """api, если явно не сказано local. Непонятное значение — отказ, а не догадка."""
    explicit = os.environ.get("EMBEDDER_BACKEND", "").strip().lower()
    if explicit not in ("", "api", "local"):
        raise RuntimeError(f"EMBEDDER_BACKEND={explicit!r} не понимаю: допустимо api или local")
    return explicit or "api"


def get_embedder() -> Embedder:
    backend = active_backend()
    return ApiEmbedder() if backend == "api" else LocalEmbedder()


def expected_name() -> str:
    """Имя бэкенда без его инициализации — для сверки с сохранённым индексом."""
    if active_backend() == "api":
        return f"api:{_env('EMBEDDINGS_MODEL', DEFAULT_API_MODEL)}"
    return f"local:{_env('EMBEDDINGS_MODEL', DEFAULT_LOCAL_MODEL)}"
