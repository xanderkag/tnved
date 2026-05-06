# ТН ВЭД Ассистент

Профессиональный определитель кода Товарной номенклатуры внешнеэкономической деятельности ЕАЭС для декларантов.

## Возможности

- **Двухстадийный pipeline** классификации:
  1. **Triage** — определение группы ТН ВЭД (2 знака) + оценка полноты описания + адаптивные уточняющие вопросы
  2. **Classify** — финальный 10-значный код + 1–2 альтернативы + применённые ОПИ
- **Два режима ввода:** простой (текст) и подробный (структурированная форма с 8 полями)
- **Векторный поиск** по 27 168 кодам ТН ВЭД с фильтром по предопределённой группе (точность ↑)
- **Ссылки на ОПИ** (Основные правила интерпретации) с пояснениями
- **Полная иерархия** классификации: раздел → группа → позиция → субпозиция → подсубпозиция
- **Локальные LLM** через Ollama (qwen2.5:14b по умолчанию), без внешних API
- **Структурированный JSON-вывод** от LLM (`format='json'`) — стабильный парсинг

## Структура проекта

```
TNVED/
├── api.py              # FastAPI бэкенд
├── classifier.py       # Двухстадийный pipeline (triage + classify)
├── tnved_data.py       # Загрузка FAISS, SQLite, embedder
├── gri.py              # 9 правил ОПИ (Основные правила интерпретации)
├── fetch_tnved.py      # Скачать справочник ТН ВЭД (CSV из открытых источников)
├── parse_tnved.py      # Парсинг CSV → SQLite
├── build_index.py      # Embeddings → FAISS-индекс
├── app.py              # ⚠️ Старая Streamlit-версия (deprecated)
├── requirements.txt
├── static/             # Frontend (HTML + vanilla JS + CSS)
│   ├── index.html
│   ├── app.js
│   └── app.css
├── data/
│   ├── raw/tnved.csv   # Исходник от ФТС (~5 МБ)
│   ├── tnved.db        # SQLite (28k записей)
│   ├── tnved.faiss     # Векторный индекс (~80 МБ)
│   └── tnved_meta.json # Метаданные кодов
├── README.md
└── TECH_DEBT.md        # Известные ограничения и план развития
```

## Установка

**Требования:** Python 3.9+, Ollama, ~6 GB свободного места (индекс + модели).

```bash
cd /Users/alexanderliapustin/Desktop/TNVED

# 1. Виртуальное окружение
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 2. Скачать данные (если data/ пуста)
venv/bin/python fetch_tnved.py
venv/bin/python parse_tnved.py
venv/bin/python build_index.py

# 3. Скачать LLM-модель Ollama
ollama pull qwen2.5:14b
ollama serve  # если ещё не запущена

# 4. Запустить сервер
venv/bin/uvicorn api:app --host 127.0.0.1 --port 8765
```

Открыть **http://localhost:8765**

## Архитектура

```
              ┌───────────────────────┐
              │   Frontend (vanilla)  │
              │  3 шага: ввод →       │
              │  уточнения → итог     │
              └──────────┬────────────┘
                         │
                         ▼  HTTP/JSON
              ┌──────────────────────┐
              │   FastAPI (api.py)   │
              │   in-memory sessions │
              └──────────┬───────────┘
                         │
            ┌────────────┼─────────────┐
            ▼            ▼             ▼
      ┌──────────┐ ┌──────────┐ ┌──────────┐
      │ Triage   │ │ Search   │ │Classify  │
      │ Stage 1  │ │ FAISS+   │ │ Stage 2  │
      │ (LLM)    │ │ filter   │ │ (LLM)    │
      └────┬─────┘ │ by group │ └────┬─────┘
           │       └─────┬────┘      │
           └─────────────┼───────────┘
                         ▼
                  ┌──────────────┐
                  │ Ollama       │
                  │ qwen2.5:14b  │
                  │ format=json  │
                  └──────────────┘
```

## API

### `POST /api/classify/start`
Старт сессии. Принимает либо `description` (простой режим), либо `fields` (подробный).

```json
{
  "mode": "simple",
  "description": "хлопковая ткань суровая, 150 г/м²",
  "model": "qwen2.5:14b"
}
```

Ответ:
```json
{
  "session_id": "abc123...",
  "group": { "code": "52", "name": "Хлопок" },
  "completeness": "medium",
  "missing_aspects": ["плотность ниток", "тип переплетения"],
  "questions": [
    { "id": "q1", "question": "...", "hint": "..." }
  ]
}
```

### `POST /api/classify/finalize`
Получение финального кода. Принимает ответы на уточняющие вопросы.

```json
{
  "session_id": "abc123...",
  "answers": [
    { "id": "q1", "question": "...", "answer": "..." }
  ]
}
```

Ответ:
```json
{
  "result": {
    "primary": {
      "code": "5208210000",
      "reasoning": "...",
      "confidence": "high",
      "hierarchy": [
        { "code": "52", "description": "..." },
        { "code": "5208", "description": "..." },
        ...
      ]
    },
    "alternatives": [...],
    "gri_applied": ["1", "6"],
    "gri_explained": [...],
    "checks_required": [...]
  }
}
```

### `GET /api/models`
Список моделей Ollama.

## Данные

**Источник:** [infoculture/opencustoms](https://github.com/infoculture/opencustoms) — данные ФТС России в формате CSV.

⚠️ Репозиторий не обновляется с 2017 года — для production нужен свежий источник (см. TECH_DEBT.md).

## Лицензия

Прототип. Не для использования в production без доработок (см. TECH_DEBT.md).
