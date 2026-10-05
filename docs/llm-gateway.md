# LLM Gateway

LLM Gateway — единственное место, где бэкенд AI-ревьюера обращается к языковой
модели. На вход он получает PR (`ReviewRequest`: diff, title, description), на выходе
отдаёт провалидированный `ReviewResult` со списком `Finding`. По формату этот список
совпадает с типом фронтенда `dmc-268-ui-t4/src/modules/runs/domain/finding.ts`.

Спецификация, по которой сделан модуль, лежит в [`docs/specs/llm-gateway.md`](specs/llm-gateway.md).

## Схема вызова

```
ReviewService (позже)
      │  LLMGateway.review(ReviewRequest)            domain/ports.py
      ▼
FallbackLLMGateway                                   adapters/llm/gateway.py
      │  для каждого провайдера по порядку:
      │    review_with_provider()
      │      build_messages()  ── system: prompts/review_v1.md
      │                        └─ user: title/description/diff внутри
      │                                 <<<UNTRUSTED_DIFF>>> … <<<END_UNTRUSTED_DIFF>>>
      │      provider.complete(messages)
      │      parse_review(reply) ── снять ```json```, JSON → ReviewOut → domain Finding
      │        невалидный JSON → один повтор с «верни только JSON» → иначе LLMOutputError
      │  ProviderUnavailableError / LLMOutputError → следующий провайдер
      │  все упали → AllProvidersFailedError(causes)
      ▼
OpenAICompatibleProvider (primary: eurouter)         adapters/llm/openai_compat.py
      │  POST {base_url}/chat/completions, Authorization: Bearer <key из KeyPool>
      │  429 → cooldown ключа (Retry-After | 60 с) → следующий ключ
      │  401/403 → ключ выключен до рестарта → следующий ключ
      │  таймаут, сеть, 5xx, 4xx → ProviderUnavailableError
      ▼
OpenAICompatibleProvider (fallback: ollama, без ключей, без Authorization)
```

| Файл | Ответственность |
|---|---|
| `domain/models.py` | `Finding`, `FindingPosition`, `FindingSuggestion`, `ReviewResult`, `Severity`, `Category`, `DiffSide` |
| `domain/ports.py` | `ReviewRequest`, Protocol `LLMGateway` |
| `domain/errors.py` | `LLMGatewayError` и наследники; наружу уходят только они |
| `adapters/llm/config.py` | `LLMSettings.from_env()`: разбор и проверка env |
| `adapters/llm/keys.py` | `KeyPool` (round-robin, cooldown, отключение ключей) и `mask_key` |
| `adapters/llm/openai_compat.py` | HTTP к `/chat/completions`, ротация ключей, коды ошибок |
| `adapters/llm/prompt.py`, `prompts/review_v1.md` | системный промпт v1, разметка untrusted-данных |
| `adapters/llm/schema.py` | Pydantic `ReviewOut`/`FindingOut`, отбраковка невалидных finding, `to_domain()` |
| `adapters/llm/gateway.py` | ревью одним провайдером с повтором при плохом JSON, fallback между провайдерами |
| `adapters/llm/factory.py` | `build_gateway(settings)` |
| `adapters/llm/smoke.py` | ручная проверка на живом ключе |

Пример использования:

```python
from adapters.llm.config import LLMSettings
from adapters.llm.factory import build_gateway
from domain.ports import ReviewRequest

gateway = build_gateway(LLMSettings.from_env())
result = await gateway.review(ReviewRequest(diff_text=diff, pr_title=title, pr_description=body))
```

## Переменные окружения

Адаптер читает только эти переменные. Значений секретов нет ни в коде, ни в репозитории.

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `LLM_PRIMARY_NAME` | `eurouter` | имя основного провайдера в логах, ошибках и `ReviewResult.provider` |
| `LLM_PRIMARY_BASE_URL` | `https://api.eurouter.ai/api/v1` | в CI: `vars.AI_DMC268_URL` |
| `LLM_PRIMARY_API_KEYS` | **обязательна** | ключи через запятую; в CI: `secrets.AI_DMC268_T4` |
| `LLM_PRIMARY_MODEL` | **обязательна** | id модели у Eurouter |
| `LLM_FALLBACK_ENABLED` | `false` | `true`/`false` (также `1/0`, `yes/no`, `on/off`) |
| `LLM_FALLBACK_NAME` | `ollama` | имя запасного провайдера |
| `LLM_FALLBACK_BASE_URL` | `http://localhost:11434/v1` | OpenAI-совместимый endpoint Ollama |
| `LLM_FALLBACK_MODEL` | обязательна при включённом fallback | например `qwen2.5-coder:7b` |
| `LLM_TIMEOUT_SECONDS` | `120` | таймаут одного HTTP-запроса, `> 0` |
| `LLM_TEMPERATURE` | `0.0` | от `0` до `2` |
| `LLM_JSON_MODE` | `true` | добавляет в запрос `response_format: {"type": "json_object"}` |
| `LLM_MAX_TOKENS` | `4096` | лимит ответа, отправляется как `max_tokens` в каждом запросе, `> 0`. Без него часть провайдеров резервирует под ответ всё окно модели и отклоняет запрос (так было с `qwen3-coder-30b-a3b` на Eurouter) |
| `LLM_MAX_INPUT_TOKENS` | `32000` | жёсткий бюджет входа (рекомендация Миши для `qwen3-coder`), `> 0`. Оценка: символы всех сообщений / 3 (с запасом, код в среднем 3.5-4 символа на токен). Больше бюджета: `LLMInputTooLargeError`, провайдер не вызывается. Уложить контекст в бюджет должен сборщик контекста (шаг `BUILD_CONTEXT`), воркер переводит эту ошибку в `skip_diff_too_large` |
| `LLM_PRIMARY_PROVIDER_ORDER` | пусто | провайдеры Eurouter через запятую, например `scaleway,ovhcloud`. Уходит в запрос как `provider: {order, allow_fallbacks: false}`: сначала первый, потом следующие, других не берём. Нужен, потому что часть провайдеров из каталога модель отклоняет (у `qwen3-coder-30b-a3b` так делает GreenPT), а без закрепления Eurouter иногда выбирает именно их. Пусто = маршрутизация Eurouter по умолчанию |

Если обязательных переменных нет, `LLMConfigurationError` перечисляет их все сразу.

## Лимиты ответа

Совпадают с `docs/schemas/llm-output.schema.json` (контракт из #11):

| Поле | Лимит | Если превышен |
| --- | --- | --- |
| `summary` | 4000 символов | обрезается до 4000 |
| `findings` | 50 | остаются 50 самых серьёзных (critical → low), внутри одной severity порядок модели; в лог WARNING с числом отрезанных |
| `path` | 1024 символа | finding отбрасывается |
| `suggestion.before` / `after` | 50 строк | finding отбрасывается |

Отбрасывание finding следует правилу контракта «invalid ones are dropped, not fixed».
`summary` и число findings ограничиваются, а не отклоняются: из-за длинной сводки или
лишних замечаний не стоит терять всё ревью.

## Запуск и проверки

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
pytest -q
ruff check . && ruff format --check .
mypy domain adapters
# ручной смоук с живым ключом (не в тестах и не в CI):
LLM_PRIMARY_API_KEYS=... LLM_PRIMARY_MODEL=... python -m adapters.llm.smoke tests/fixtures/sql_injection.diff
```

Если `python3 -m venv` падает с ошибкой про `ensurepip` (Debian/Ubuntu без `python3-venv`),
окружение можно создать через `uv venv .venv && uv pip install -e ".[dev]"`.

Smoke выводит `ReviewResult` в JSON и возвращает коды: `0` — успех, `1` — ревью не
удалось (`LLMGatewayError`), `2` — ошибка конфигурации или не читается файл диффа.

## Логирование

Логгер `adapters.llm`. Текст диффа, промпта, ответов модели и finding в лог не попадает,
ключи пишутся только маской (`sk-…abcd`).

| Уровень | Событие |
|---|---|
| INFO | провайдер, модель, длительность, число findings |
| WARNING | 429 и cooldown ключа, 401/403 и отключение ключа, повтор из-за невалидного JSON, число выброшенных finding, переход на следующего провайдера |
| ERROR | `AllProvidersFailedError` |

## Решения

Неоднозначности спеки и то, как они решены.

1. **`LLMConfigurationError` лежит в `domain/errors.py`** и наследует `LLMGatewayError`.
   Спека требует выпускать наружу только исключения из `domain/errors.py`, а ошибка
   конфигурации тоже уходит наружу (из `from_env()` и `build_gateway`).
2. **«Невалидный JSON целиком»** — это и синтаксически битый JSON, и валидный JSON без
   объекта вида `{"summary": str, "findings": list}` (например `[]` или ответ без
   `findings`). В обоих случаях делается один повтор. Если JSON валиден, но плох
   отдельный finding, выбрасывается только этот finding.
3. **Повтор при плохом JSON** уходит тому же провайдеру: исходные messages, затем
   невалидный ответ в роли `assistant`, затем просьба вернуть только JSON. После второго
   провала `LLMOutputError` переключает gateway на следующего провайдера.
4. **`line` проверяется строго:** `"12"`, `12.0` и `true` не приводятся к числу, такой
   finding выбрасывается. Это тот же принцип, что и для severity/category: «похожие»
   значения не приводим. Лишние поля в finding (`confidence` и т.п.) игнорируются.
5. **`message`** сначала обрезается от пробелов по краям, потом проверяются непустота и
   длина ≤ 2000. `summary` тоже обрезается. Отсутствующий `suggestion` равен `null`.
6. **`content: null`** в ответе провайдера считается пустым ответом, то есть невалидным
   JSON с одним повтором. Битый конверт chat completion (нет `choices` и т.п.) и
   неожиданный 2xx, отличный от 200, дают `ProviderUnavailableError`.
7. **Retry-After** понимается только в секундах. HTTP-date или мусор означают 60 с,
   отрицательное значение — 0.
8. **Ротация ключей ограничена размером пула** на один запрос: даже при
   `Retry-After: 0` запрос не зациклится, после N попыток будет `ProviderUnavailableError`.
9. **429/401/403 от провайдера без ключей** (Ollama) ротировать нечем, поэтому сразу
   `ProviderUnavailableError`.
10. **4xx-ошибки:** в сообщение идут код и первые 200 символов тела. Если тело эхом
    возвращает ключ, ключ заменяется маской до обрезки, чтобы обрезка не оставила
    частичный ключ.
11. **Маска ключа:** `key[:3] + "…" + key[-4:]`. У ключей короче 12 символов видны
    только два последних (`…ab`), иначе маска раскрывала бы большую часть ключа.
    `ProviderSettings.api_keys` скрыт из `repr`.
12. **Повторяющиеся ключи** в `LLM_PRIMARY_API_KEYS` схлопываются с сохранением порядка:
    состояние (cooldown/disabled) хранится по значению ключа.
13. **Имя fallback-провайдера должно отличаться от primary**, иначе в `ReviewResult.provider`
    и в логах не понять, кто ответил.
14. **HTTP-клиент создаётся на каждый вызов `complete()`** (`async with httpx.AsyncClient`).
    Жизненный цикл простой, соединения не утекают. Потеря пула соединений ничтожна на
    фоне времени ответа модели. Транспорт инжектируется, так тесты подменяют его
    через `httpx.MockTransport`.
15. **Экранирование маркеров** нечувствительно к регистру и пробелам внутри угловых
    скобок: `<<<END_UNTRUSTED_DIFF>>>` → `<<ESCAPED:END_UNTRUSTED_DIFF>>`. Title и
    description экранируются так же, как diff. Вводная фраза user-сообщения маркеры не
    повторяет, поэтому каждый маркер встречается в сообщении ровно один раз.
16. **В промпте `path` указывается без префиксов `a/`/`b/`**, как в GitHub review comments.
17. **Сборка:** в `pyproject.toml` добавлены `[build-system]` (setuptools) и
    `packages.find` только для `domain*`/`adapters*`. Без этого `pip install -e .` на
    flat-layout с несколькими top-level каталогами падает. Промпт `prompts/*.md` включён
    как package data и читается через `importlib.resources`.
18. **Ruff исключает `*.md`.** Ruff ≥ 0.16 форматирует Python-блоки внутри Markdown и
    требовал бы переформатировать документы в `docs/`, а их этот модуль не меняет.
19. **mypy в strict-режиме** для `domain` и `adapters`.
20. **Тесты 19 и 20** вынесены в отдельные файлы `tests/test_llm_secrets.py` и
    `tests/test_domain_purity.py`. Остальные сценарии разложены по файлам из спеки.
    Общие фейки (часы, записывающий MockTransport, фейковые ключи) лежат в `tests/conftest.py`.

## Расхождения

Места, где этот модуль расходится с другими документами репозитория. Решение по ним
за командой, сами документы здесь не менялись.

1. **Провайдеры.** В `SYSTEM_DESIGN.md` (§5.4, «LLM Providers») и
   `docs/BACKEND_ARCHITECTURE.md` в MVP заложены OpenAI и Anthropic (+ Gemini позже),
   с раскладкой `adapters/llm/anthropic.py, openai.py, google.py`. Карточка спринта
   требует Eurouter (OpenAI-совместимый API) как primary и локальную Ollama как fallback.
   Сделано по карточке: один `OpenAICompatibleProvider` обслуживает оба. Нативного
   Anthropic-адаптера нет. Через Eurouter модели Anthropic доступны по OpenAI-совместимому
   API, поэтому отдельный адаптер нужен только для прямого подключения к Anthropic.
   `docs/configuration.md` задаёт ключ `llm_provider` (default `"anthropic"`, уровень
   INSTALLATION). Gateway его пока не читает, провайдеры задаются только env.
2. **Категории.** В `SYSTEM_DESIGN.md` §5.5 перечислены correctness, security,
   edge cases, performance, readability, best practices. Во фронтенде (`finding.ts`,
   `FindingCategory`): security, correctness, concurrency, performance, maintainability.
   Контракт с фронтендом важнее, поэтому Gateway использует категории `finding.ts`.
   В промпте edge cases отнесены к `correctness`. Readability и best practices в
   отдельные категории не выделены: ревьюер ищет дефекты, а не стиль, а близкое по
   смыслу покрывает `maintainability`. Неизвестные категории отбрасываются.
3. **Severity.** `docs/configuration.md` и `docs/BACKEND_ARCHITECTURE.md` («Finding
   severity — Not persisted») фиксируют, что severity не хранится: это порог
   `min_severity`, как уровень логирования. Gateway severity отдаёт, иначе нечем
   фильтровать. При этом фронтенд (`finding.ts`) показывает severity у уже
   опубликованного finding. Если severity не хранится, UI неоткуда её взять после
   публикации. Команде нужно решить: хранить severity или восстанавливать её другим путём.
4. **Позиция finding.** `SYSTEM_DESIGN.md` описывает `line/range`. Фронтенд и Gateway
   используют одну строку (`path`, `line`, `side`), диапазонов нет.
5. **Имя порта.** `docs/component-architecture-and-ER-model.md` называет порт
   `LLMGatewayPort`, раскладка в `BACKEND_ARCHITECTURE.md` называет его `LLMGateway`.
   Сделано `LLMGateway` в `domain/ports.py`.
