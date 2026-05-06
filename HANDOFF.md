# HANDOFF — состояние перед деплоем (07.05.2026)

Чек-лист для завтрашнего деплоя на kb-docker. Если этот файл устарел — он лежит в feat/initial-deploy на момент последнего коммита.

## Где лежит код

- **GitLab:** [git.taipit.ru/airesearch/tnved](https://git.taipit.ru/airesearch/tnved), MR [#1](https://git.taipit.ru/airesearch/tnved/-/merge_requests/1), ветка `feat/initial-deploy`.
- **Локально (рабочая копия):** `U:\Users\lyapustin.a\ТНВЭД\` (она же запушена). Дубликат для бэкапа: `U:\Users\lyapustin.a\Desktop\ai-platform\tnved\`.
- Origin GitHub-апстрим оставлен как `origin`, GitLab добавлен как `gitlab`.

## Что готово

- Свап `Ollama → AsyncOpenAI` (any compatible endpoint). Всё через env.
- Свежие листья + ставка пошлины из TWS.BY (B-логика — день ежедневный, source of truth).
- Три UI-вкладки: одиночный товар, batch xlsx, чат.
- Docker-обвязка: Dockerfile + docker-compose.yml + Caddy reverse-proxy.
- Архитектурное ревью + 11 фиксов (см. TECH_DEBT.md секция «🏗️ Архитектурное ревью», B1–B18).
- mock-сервер `demo_server.py` для превью без LLM (исключён из Docker-образа).

## Что НУЖНО решить ДО деплоя

| # | Решение | Зачем |
|---|---|---|
| 1 | **Имя сабдомена** (например `tnved.taipit.ru`) | в письмо Павлу + DNS |
| 2 | **HOST_PORT** (предложение: `8082`) | в `.env` на сервере и в письме Павлу |
| 3 | **LLM-эндпоинт** | OpenAI прямой / внутренний vLLM-шлюз / что-то ещё |
| 4 | **OPENAI_API_KEY** | в `.env` на сервере |

## Деплой (по checklist `02-first-deploy.md`)

### 1. На GitLab — создать Deploy Token
Settings → Repository → Deploy tokens → scope `read_repository`. Сохранить `username + token`.

### 2. На сервере (kb-docker, 10.10.13.10)

```bash
ssh kb-docker
cd ~

git clone "https://gitlab+deploy-token-NNN:gldt-XXXXX@git.taipit.ru/airesearch/tnved.git"
cd tnved

# на момент первого деплоя берём ветку (если main ещё пустой/только-README):
git checkout feat/initial-deploy

cp .env.example .env
nano .env
# заполнить:
#   OPENAI_API_KEY=...
#   OPENAI_BASE_URL=...    (пустой если OpenAI; или https://... для vLLM)
#   LLM_MODEL=gpt-4o-mini  (или подходящая)
#   HOST_PORT=8082
#   COMPOSE_PROJECT_NAME=tnved

chmod 600 .env

docker compose up -d --build   # 3–7 минут на первый билд
```

### 3. Проверить что взлетело

```bash
docker compose ps     # оба контейнера healthy
curl http://localhost:8082/health
# {"status":"ok","vectors":15614,"groups":97,"static_version":"<sha1[:8]>"}

curl -fsS http://localhost:8082/ | head -5   # HTML с подменой ?v=...

docker compose logs --tail=80
```

### 4. Письмо Павлу — шаблон

> Павел, привет. Готовим к деплою на `10.10.13.10` новый сервис `tnved` (Ассистент по ТН ВЭД для декларантов).
>
> Прошу:
> 1. **Сабдомен** `<уточнить>.taipit.ru` (доступ из корп.сети, как у kb).
> 2. TLS-сертификат на этот поддомен.
> 3. nginx server_block копией с kb (с WS-headers — на всякий случай, хотя WS у нас не используется).
> 4. `proxy_pass http://10.10.13.10:8082`.
> 5. **`client_max_body_size 20m`** — для загрузки xlsx-батчей до 10 МБ.
> 6. **`proxy_read_timeout 600s`** — батчи на 500 строк могут идти ~10 минут.
>
> Стек: FastAPI + Caddy в Docker, healthcheck `GET /health`. Спасибо!

### 5. Открыть в корп.браузере

`https://<имя>.taipit.ru` → должны увидеть три вкладки. На «Один товар» вписать описание реального товара → triage и финал должны вернуть **разные** коды для разных входов (в отличие от мок-сервера).

Если 502 / Bad Gateway → попросить Павла проверить `proxy_pass` (порт 8082 на хосте).

## Если что-то не так

- **healthcheck падает 503** — посмотреть `docker compose logs app | grep FATAL`. Скорее всего FAISS/SQLite не собрались на этапе билда (TWS.BY 503'нула).
- **батч застревает на N/M** — OpenAI таймаутит, см. `docker compose logs app | grep -i timeout`. Поправить `LLM_TIMEOUT` в `.env`, перезапустить.
- **«ставка не указана» у всех кодов** — TWS.BY не приехал во время билда. Решение в B17 (sentinel ≥50% со ставкой) — теперь билд просто упадёт, видно сразу.
- **браузер показывает старый JS/CSS** — закрыто B11 (cache-bust по sha1), но если хочется удостовериться — `curl -s http://localhost:8082/ | grep app.js` должен показать `?v=<хеш>`.

## Что отложили из B-беклога (после первого боевого использования)

- **B3** Чат жжёт токены квадратично (triage на каждом ходе) — рефакторинг на «один triage + накопление контекста». ~1 час.
- **B5** Скачивание xlsx без auth — нужно UX-решение про токен в URL.
- **B6** Rate-лимит — Caddy middleware или slowapi.
- **B9** Полная защита от пустого fetch (флаг `--strict`) — частично закрыта B17 sentinel-чеком.
- **B16** Retry на сломанный JSON-ответ от LLM (это исходный TECH_DEBT #3).

Полный список — в [TECH_DEBT.md](TECH_DEBT.md), секция «🏗️ Архитектурное ревью».
