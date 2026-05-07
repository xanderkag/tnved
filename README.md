# ТН ВЭД Ассистент

Определитель кода Товарной номенклатуры ВЭД ЕАЭС для декларантов. FastAPI + LLM + FAISS.

> Форк апстрима [xanderkag/tnved](https://github.com/xanderkag/tnved). Локальный Ollama выпилен, теперь любой OpenAI-совместимый endpoint. Добавлены: пошлины из TWS.BY, batch-режим (xlsx), чат-режим, Docker-обвязка.

## Возможности

- **Двухстадийный pipeline:** triage (определение группы + уточняющие вопросы) → classify (10-значный код + альтернативы + ОПИ).
- **Три UI-режима:**
  - **Один товар** — пошаговая форма с уточнениями.
  - **Пакет (Excel)** — загрузка xlsx, прогресс-бар, скачивание xlsx с результатами. До 500 строк, параллелизм 5.
  - **Чат** — свободный диалог с авто-финализацией.
- **Свежие коды + пошлины** — слияние [infoculture/opencustoms](https://github.com/infoculture/opencustoms) (иерархия) + [TWS.BY](https://www.tws.by/tws/tnved/download/excel) (актуальные листья + ставка пошлины, обновляется ежедневно). 31 622 кода в SQLite, 13 285 со ставкой.
- **Векторный поиск** через `intfloat/multilingual-e5-base` + FAISS, фильтрация по группе.
- **Два провайдера LLM из коробки**: OpenAI и Anthropic (Claude). Конфиг — через шестерёнку в шапке UI (хранится в localStorage и шлётся заголовками `X-LLM-*` per-request) либо через env как fallback.
- **JSON-режим LLM** для стабильного парсинга.

## Структура

```
api.py              # FastAPI: /api/classify/start|finalize, /api/classify/batch, /api/chat/*
classifier.py       # triage + classify, get_client (AsyncOpenAI с timeout)
tnved_data.py       # TNVEDStore: FAISS + SQLite + sentence-transformers
gri.py              # 9 ОПИ
fetch_tnved.py      # download infoculture CSV + TWS.BY xlsx
parse_tnved.py      # CSV+xlsx → SQLite (схема codes + meta)
build_index.py      # embeddings → FAISS, meta.json
demo_server.py      # ⚠️ mock-API для превью без LLM/FAISS (НЕ в Docker-образе)
Dockerfile
docker-compose.yml  # app + Caddy reverse-proxy на :8000
Caddyfile
.env.example
static/{index.html,app.js,app.css}
data/               # генерируется fetch+parse+build_index, в .gitignore
TECH_DEBT.md        # известные ограничения, B-беклог архитектурного ревью
```

## Запуск через Docker (production)

```bash
cp .env.example .env
nano .env   # OPENAI_API_KEY, OPENAI_BASE_URL (если внутренний), HOST_PORT, COMPOSE_PROJECT_NAME
docker compose up -d --build
```

Билд ~3–7 минут: тянет infoculture-CSV + TWS.BY-xlsx + e5-base из HF Hub, считает FAISS.

Healthcheck: `GET /health` (200 если store загружен, 503 в degraded-режиме). Контейнер сам рестартится при сломанной БД.

## Запуск локально (разработка)

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt   # Windows; на *nix venv/bin/python

venv/Scripts/python fetch_tnved.py     # качает CSV+xlsx в data/raw/
venv/Scripts/python parse_tnved.py     # → data/tnved.db
venv/Scripts/python build_index.py     # → data/tnved.faiss + tnved_meta.json (5–10 мин)

set OPENAI_API_KEY=sk-...
venv/Scripts/python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

Открыть **http://localhost:8000**.

## Превью без LLM (для UI-работы)

```bash
venv/Scripts/python -m uvicorn demo_server:app --host 127.0.0.1 --port 8765
```

Возвращает захардкоженные ответы на тех же эндпоинтах. Полезно для верстки/демонстрации, но **классификация не работает** (всё отдаёт посудомойку 8422110000).

## Конфигурация LLM

**Два пути:**

1. **UI (приоритет):** шестерёнка ⚙ в шапке → выбор провайдера, модели, ввод ключа (и опционально base URL для OpenAI). Сохраняется в `localStorage` и подмешивается заголовками `X-LLM-Provider` / `X-LLM-API-Key` / `X-LLM-Model` / `X-LLM-Base-URL` в каждый запрос. Сервер ничего не сохраняет.
2. **Env (fallback):** если в заголовках поля пусты, классификатор подтягивает их из env (см. таблицу ниже). Удобно для дев-стенда / Docker-демо без UI-настройки.

| Переменная | Дефолт | Что |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai` или `anthropic` |
| `OPENAI_API_KEY` | — | для OpenAI / OpenAI-совместимых |
| `ANTHROPIC_API_KEY` | — | для Anthropic |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | внутренний vLLM/шлюз — указать сюда |
| `LLM_MODEL` | `gpt-4o-mini` (или `claude-haiku-4-5` для Anthropic) | имя модели |
| `LLM_TIMEOUT` | `60` | сек, верхний таймаут на LLM-вызов |
| `BATCH_CONCURRENCY` | `5` | параллельных LLM-запросов в одном батче |
| `BATCH_MAX_ROWS` | `500` | максимум строк в xlsx |
| `CHAT_MAX_TURNS` | `6` | после скольких ходов чат финализирует автоматически |
| `MAX_DESCRIPTION_LEN` | `5000` | максимум символов на одно описание |
| `SESSION_TTL_HOURS` | `24` | через сколько чистится in-memory сессия/чат/батч |
| `CLEANUP_INTERVAL_SECONDS` | `600` | как часто запускается cleanup-loop |

## API (важное)

| Метод | URL | Назначение |
|---|---|---|
| `POST` | `/api/classify/start` | Один товар: triage |
| `POST` | `/api/classify/finalize` | Один товар: финальный код |
| `POST` | `/api/classify/batch` | xlsx → job_id (multipart) |
| `GET`  | `/api/classify/batch/{job_id}` | Статус батча |
| `GET`  | `/api/classify/batch/{job_id}/download` | xlsx-результат |
| `POST` | `/api/chat/start` | Новый чат |
| `POST` | `/api/chat/{id}/message` | Реплика в чат |
| `GET`  | `/api/chat/{id}` | Снимок чата |
| `GET`  | `/api/models` | `{models:[...], default:...}` |
| `GET`  | `/health` | 200 если store загружен, иначе 503 |

## Лицензия

Внутренний инструмент TAIPIT, прототип конкурса AI-инициатив. Не для внешнего production без доработок ([TECH_DEBT.md](TECH_DEBT.md)).
