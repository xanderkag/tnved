# ТН ВЭД Ассистент

Определитель кода Товарной номенклатуры ВЭД ЕАЭС для декларантов. FastAPI + LLM + FAISS.

> Форк апстрима [xanderkag/tnved](https://github.com/xanderkag/tnved). Локальный Ollama выпилен, теперь любой OpenAI-совместимый endpoint. Добавлены: пошлины из TWS.BY, batch-режим (xlsx), чат-режим, Docker-обвязка.

## Возможности

- **Двухстадийный pipeline:** triage (определение группы + уточняющие вопросы) → classify (10-значный код + альтернативы + ОПИ).
- **Три UI-режима:**
  - **Один товар** — пошаговая форма с уточнениями.
  - **Пакет (Excel)** — загрузка xlsx, прогресс-бар, скачивание xlsx с результатами. До 500 строк, параллелизм 5. Колонки узнаются по заголовкам (шапка — в любой из первых 10 строк): описание обязательно, артикул, производитель и страна происхождения идут модели вместе с ним; колонки с кодом ТН ВЭД и пошлиной не читаются. В результате — номер строки исходного файла; строка без описания попадает туда с ошибкой, а не пропадает.
  - **Чат** — свободный диалог с авто-финализацией.
- **Свежие коды + пошлины** — слияние [infoculture/opencustoms](https://github.com/infoculture/opencustoms) (иерархия) + [TWS.BY](https://www.tws.by/tws/tnved/download/excel) (актуальные листья + ставка пошлины, обновляется ежедневно). 31 622 кода в SQLite, 13 285 со ставкой.
- **Кандидаты — только действующие коды:** 13 289 десятизначных, которые есть в тарифе. 6- и 8-значные — только путь к коду; коды, оставшиеся лишь в дереве 2017 (2 325), сняты и моделью не предлагаются.
- **Векторный поиск** — `bge-m3` на нашем сервере (`EMBEDDINGS_BASE_URL`) + FAISS, фильтрация по группе. Индекс собирается заранее (`build_index.py`); при старте модель сверяется с его паспортом. Локальная `e5` — только явно (`EMBEDDER_BACKEND=local` + `requirements-e5.txt`), в образ не входит.
- **Только наша модель**: OpenAI-совместимый vLLM во внутренней сети, задаётся на сервере (`LLM_BASE_URL`, `LLM_MODEL`). Внешних провайдеров и выбора модели из UI нет — описания товаров наружу не уходят.
- **JSON-режим LLM** для стабильного парсинга.

## Структура

```
api.py              # FastAPI: /api/classify/start|finalize, /api/classify/batch, /api/chat/*
classifier.py       # triage + classify, get_client (AsyncOpenAI с timeout)
tnved_data.py       # TNVEDStore: FAISS + SQLite
embedder.py         # векторы: bge-m3 по EMBEDDINGS_BASE_URL (e5 на CPU — только явно)
gri.py              # 9 ОПИ
fetch_tnved.py      # download infoculture CSV + TWS.BY xlsx
parse_tnved.py      # CSV+xlsx → SQLite (схема codes + meta)
build_index.py      # векторы → FAISS + meta.json + паспорт индекса (какой моделью собран)
check_embeddings.py # сверка сервера векторов с индексом — по самим векторам, не по имени
demo_server.py      # ⚠️ mock-API для превью без LLM/FAISS (НЕ в Docker-образе)
Dockerfile
constraints.txt     # версии пакетов, с которыми образ проверен на kb-docker (pip freeze)
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
nano .env   # LLM_BASE_URL, LLM_MODEL, EMBEDDINGS_BASE_URL (наши модели), HOST_PORT, COMPOSE_PROJECT_NAME
docker compose up -d --build
```

Индекс в образе не считается: до сборки в `data/` должны лежать `tnved.db`, `tnved.faiss`, `tnved_meta.json` и `tnved_index_info.json` (`data/` в `.gitignore` — файлы переносятся отдельно). Без индекса полный билд падает с причиной; `LITE_MODE=1` собирается и без него. Нет `tnved.db` — билд сам скачает и разберёт справочник. torch и модель e5 в образ не входят.

Версии пакетов образа закреплены в `constraints.txt` — это набор, проверенный на kb-docker. faiss-cpu — не выше 1.13: с 1.14 колесо для Linux требует AVX, а у kb-docker процессор без AVX, и поиск падает с SIGILL (процесс умирает без трейсбэка, Caddy отдаёт 502). Поднимаете версию — соберите, проверьте на kb-docker и перепишите `constraints.txt` из `pip freeze` контейнера.

Сменили `EMBEDDINGS_BASE_URL` — `docker compose exec app python check_embeddings.py`. Паспорт индекса сверяет только имя модели, а у Ollama и vLLM оно одно (`bge-m3`); скрипт сравнивает сами векторы с записанными в индексе и отказывает, если косинус ниже 0,995.

Healthcheck: `GET /health` (200 если store загружен, 503 в degraded-режиме). Контейнер сам рестартится при сломанной БД.

## Запуск локально (разработка)

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt   # Windows; на *nix venv/bin/python

set EMBEDDINGS_BASE_URL=http://10.10.28.10:11434/v1   # bge-m3 (Ollama): векторы индекса и запросов
venv/Scripts/python fetch_tnved.py     # качает CSV+xlsx в data/raw/
venv/Scripts/python parse_tnved.py     # → data/tnved.db
venv/Scripts/python build_index.py     # → data/tnved.faiss + tnved_meta.json + tnved_index_info.json

set LLM_BASE_URL=http://10.10.33.10:8100/v1
set LLM_MODEL=qwen36-vllm                    # имя из GET /v1/models
venv/Scripts/python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

Без сервера векторов — локальная e5 на CPU, только явно: `pip install -r requirements-e5.txt`, `set EMBEDDER_BACKEND=local`, и индекс пересобрать ею же (часы на CPU): индекс bge-m3 с ней не сойдётся по паспорту, сервис не стартует.

Открыть **http://localhost:8000**.

## Превью без LLM (для UI-работы)

```bash
venv/Scripts/python -m uvicorn demo_server:app --host 127.0.0.1 --port 8765
```

Возвращает захардкоженные ответы на тех же эндпоинтах. Полезно для верстки/демонстрации, но **классификация не работает** (всё отдаёт посудомойку 8422110000).

## Конфигурация LLM

Только наша модель на нашем железе (решение 25.09.2026): описания товаров не уходят во внешние сервисы.

- Модель задаётся **только на сервере**, через env. Значений по умолчанию нет: без `LLM_BASE_URL` и `LLM_MODEL` сервис не стартует. Адрес, который резолвится вне внутренней сети, — тоже отказ при старте. Та же проверка — для `EMBEDDINGS_BASE_URL`.
- Запрос с заголовками `X-LLM-*` получает **400**: адрес, ключ и модель из запроса не принимаются.
- Модель не ответила за `LLM_TIMEOUT`, ответ обрезан по `LLM_MAX_TOKENS` или не JSON — **503** с причиной. Повторов нет, на другую модель сервис не переключается.
- Qwen3.x вызывается без «размышлений» (`chat_template_kwargs.enable_thinking=false`): ответ сразу JSON (`response_format=json_object`). Шаблоны других моделей этот ключ игнорируют.
- `LLM_PROVIDER`, `OPENAI_*`, `ANTHROPIC_*` больше не читаются; если заданы — сервис не стартует, чтобы не было иллюзии, что они работают.
- Векторы запросов — тот же принцип: `EMBEDDINGS_BASE_URL` обязателен в полном режиме, модель должна совпасть с паспортом индекса. Сервер векторов не ответил за 3 попытки — **503** «Векторный поиск недоступен»; на локальную e5 сервис не переключается.

| Переменная | Дефолт | Что |
|---|---|---|
| `LLM_BASE_URL` | — (обязательна) | OpenAI-совместимый адрес vLLM во внутренней сети, например `http://10.10.33.10:8100/v1` |
| `LLM_MODEL` | — (обязательна) | имя модели, как его отдаёт `GET <LLM_BASE_URL>/models`; на kb-docker — `qwen36-vllm` |
| `LLM_API_KEY` | пусто | если vLLM запущен с `--api-key` |
| `LLM_TIMEOUT` | `60` | сек на один вызов модели, без повторов. Замер 25.09 на qwen36-vllm: 5–30 с на вызов при 5 параллельных |
| `LLM_MAX_TOKENS` | `2048` | потолок ответа; обрезанный ответ — отказ. Замер 25.09: ответ до ~900 токенов |
| `EMBEDDINGS_BASE_URL` | — (обязательна в полном режиме) | OpenAI-совместимый `/v1/embeddings` во внутренней сети; на kb-docker — `http://10.10.33.10:11434/v1` (vLLM; индекс собран на Ollama 10.10.28.10, векторы сверены 25.09) |
| `EMBEDDINGS_MODEL` | `bge-m3` | должна совпасть с паспортом `data/tnved_index_info.json`, иначе сервис не стартует |
| `EMBEDDINGS_API_KEY` | пусто | если сервер векторов требует ключ; Ollama — без ключа |
| `EMBEDDINGS_TIMEOUT` | `30` | сек на запрос векторов; 3 попытки, потом 503 |
| `EMBEDDER_BACKEND` | `api` | `local` — e5 на CPU: только вне образа (`requirements-e5.txt`) и с индексом, собранным ею же |
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
| `GET`  | `/api/codes` | Версия справочника: `db_built_at`, `tariff_as_of`, `codes10`, `codes10_current` |
| `GET`  | `/api/codes/{code}` | Карточка кода (2–10 цифр): `status` current / retired / not_leaf, `in_tariff`, `name`, `path` (уровни), `hierarchy`, `duty_rate`, версия. Нет кода — 404, не тот формат — 400 |
| `GET`  | `/api/models` | `{model}` — какая модель отвечает (задаётся только на сервере) |
| `GET`  | `/health` | 200 если store загружен, иначе 503 |

## Лицензия

Внутренний инструмент TAIPIT, прототип конкурса AI-инициатив. Не для внешнего production без доработок ([TECH_DEBT.md](TECH_DEBT.md)).
