# Серверные тесты подбора ТН ВЭД — покрытие рисков (Q-TN-VED-POLNOE-1)

Запуск (без сети, без платных моделей — канал решений, шлюз, дерево и хранилища подменены):

    python -m unittest discover -s tests        # 92 теста, < 1 с

| Риск из поручения | Где проверено |
|---|---|
| Контракт navigate: поля ответа, request_id, codes с именами, card, quota, без cost_usd для site | `test_navigate_api.NavigateContractTests.test_done_contract_on_site`, `test_internal_channel_sees_cost_and_has_no_quota` |
| Состояния done / question / empty (код 0 знаков, карточки нет) / частичный код (карточка позиции) | `test_navigate_api` (done, question_state, partial_code), `test_navigator_walk` (done, below_threshold, uncertain_heading) |
| Ошибки: пустое/длинное описание 422, навигатор выключен 503, шлюз лёг 503, квота шлюза 503 + Retry-After, неверный путь 400, прочее 500 | `test_navigate_api.NavigateContractTests` |
| Ветвление дерева: один вариант без модели, кандидаты + «другая позиция», спуск от корня, лист без вопросов | `test_navigator_walk` |
| Порог уверенности: ровно порог — идём дальше, ниже — вопрос | `test_navigator_walk.test_threshold_is_inclusive` |
| Числовые границы (не более 30 / более 30 / 75,1 → «прочие», два значения → модель) | `test_navigator_walk.test_numeric_boundaries_are_decided_by_rule`, `ConditionsBoundaryTests` |
| Несуществующий и снятый код: в дереве нет, карточка retired / 404 / 400 | `test_navigator_walk.test_retired_code_never_enters_tree`, `test_navigate_api.CardTests`, `test_leaf_sync` |
| Расхождение листьев с ЕТТ (8112924100 → …01/…09) | `test_leaf_sync` (audit, apply, идемпотентность, статус карточки, место в дереве) |
| Повреждённый ответ модели (пустой JSON, не JSON, вариант вне списка, нечисловая уверенность) | `test_decisions.test_corrupted_answers_are_unavailable_not_keyerror`, `test_choice_outside_list_is_unavailable` |
| Timeout и ограниченный retry (паузы 1-2-4-8, бюджет, 0 — одна попытка), 429 частоты — повтор, квота — без повтора, 401 — сразу | `test_decisions` |
| Нормализация и кэш: регистр/пробелы/точка, другой path — другой ключ, повтор не списывает товар | `test_navigate_api.CacheAndQuotaTests` |
| Ответы кнопками бесплатны только с request_id того же описания | `CacheAndQuotaTests.test_answers_with_request_id_are_free_new_description_is_not`, `test_nav_limits.FollowTests` |
| Конкурирующие запросы не обходят лимит (6 параллельных → 3 × 200, 3 × 429, модель 3 раза) | `CacheAndQuotaTests.test_concurrent_requests_cannot_overrun_quota` |
| Квоты 3/10, потолок сервиса 503, внутренний канал без лимита, возврат при отказе | `test_nav_limits.QuotaTests`, `CacheAndQuotaTests`, `NavigateContractTests.test_gateway_down_is_503_and_refunded` |
| Пропуск: верный, истёкший, чужая подпись, мусор, длинный id, нет секрета; X-Real-IP/X-Forwarded-For только при доверии прокси | `test_nav_limits.PassTests` |
| Сутки UTC: сброс счётчиков и привязок | `test_nav_limits.QuotaTests.test_utc_day_rollover_resets_counters_and_bindings` |
| Пошлина: источник, as_of, редакция, сноски по датам, ставка для РФ, будущая редакция не показывается, TWS-запас с источником, у снятого кода ставки нет | `test_card_provenance.DutyTests`, `StoreDutyTests` |
| НДС: 22 % всегда с источником, 10 % — условно (перечень, акт, partial, условие, дата), исключения | `test_card_provenance.VatTests` |
| Отзыв: валидация, только на выданный request_id, один на товар (замена), потолок site, журнал выключен — 503 | `test_navigate_api.FeedbackTests` |
| Хранение: шаги и кэш без текста описаний и адреса; описание только с отзывом, обрезано; сводка только внутренняя | `FeedbackTests.test_storage_keeps_no_description_until_feedback_and_trims_it`, `test_stats_internal_only` |
| «Не подошёл» снимает ответ из кэша, и после перезапуска | `test_nav_feedback_cache` |
| Выгрузка карточек для сайта | `test_export_cards` |

Не проверено этими тестами:
- качество выбора ветки моделью — это замер корпуса (`eval_navigator.py`, 88 решений ЕЭК), платный;
- полный режим FAISS и сервер векторов (поиск кандидатов подменён);
- nginx: заголовок X-Channel, лимит тела, публичные пути — проверяет codex_web после интеграции;
- клиентская страница и E2E — у website_dev (Q-P1-TN-VED-1).
