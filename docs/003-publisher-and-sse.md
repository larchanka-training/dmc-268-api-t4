# Публикация ревью в VCS и статус по SSE

Когда LLM вернула замечания, воркер сам публикует их в pull request: inline-комментарии на
строки диффа и итоговый Overview Review в Check Run. Ход задачи API отдаёт консоли потоком
Server-Sent Events.

## Схема

```
воркер: задача из очереди
  │  diff и метаданные PR                                GitProvider
  │  разбор и фильтр диффа                               domain/diff.py, domain/diff_filter.py
  │  продление аренды
  │  LLMGateway.review(ReviewRequest)                    docs/llm-gateway.md
  │  build_plan(findings, CommentableLines.from_files)   domain/publishing.py
  │  продление аренды
  ▼  ReviewPublisher.publish(PR на head_sha)             adapters/github/publisher.py
       ├─ Check Run «AI review»: вердикт, summary, conclusion
       └─ одно ревью со всеми новыми inline-комментариями
```

Шаг подключён в `worker/process.py` (`ReviewStage`) и включён всегда. Если нет переменной LLM
или GitHub App, воркер не стартует и называет недостающую (`worker/__main__.py`). Без
`DATABASE_URL` API обрабатывает задачи у себя, без шага публикации, так что публикует только
процесс воркера.

Если аренду задачи перехватили, воркер останавливается и ничего не публикует. Ошибки шага
переводятся в `error_kind`:

| Ошибка | `error_kind` | Повтор |
|---|---|---|
| `ForgeUnavailableError`, включая `ForgeRateLimitedError` | `forge_unavailable` | да |
| `AllProvidersFailedError`, `ProviderUnavailableError` | `provider_unavailable` | да |
| `InstallationGoneError` | `installation_removed` | нет |
| `PrNotFoundError` | `pr_not_found` | нет |
| `DiffFormatError` | `diff_parse_failed` | нет |
| `LLMOutputError` | `llm_output_invalid` | нет |
| `LLMInputTooLargeError` | `skip_diff_too_large` | нет |
| остальное, включая `ForgePermissionError` | `internal_error` | нет |

## Что публикуется

`build_plan` (`domain/publishing.py`) не делает ввода-вывода и одинаков для любого форжа.
Замечания проходят фильтры по порядку `PIPELINE_SPEC.md §6.2`:

1. позиция `(path, line, side)` должна быть в диффе. Допустимые позиции берутся из хунков
   парсера (`CommentableLines.from_files`), поэтому выдуманная строка не станет комментарием;
2. severity не ниже `min_severity`;
3. отпечаток ещё не публиковался в этом PR;
4. не больше `max_comments`, сначала самые серьёзные.

Настройки `PublishPolicy`:

| Поле | По умолчанию | Смысл |
|---|---|---|
| `min_severity` | `medium` | ниже этого уровня замечание не публикуется |
| `max_comments` | `25` | предел inline-комментариев за один прогон |
| `request_changes_at` | `None` | с какого уровня ревью требует изменений; `None` — никогда |

Вердикт:

| Условие | Вердикт | Событие ревью | Conclusion |
|---|---|---|---|
| есть замечание не ниже `request_changes_at` | `changes_requested` | `REQUEST_CHANGES` | `failure` |
| есть замечания не ниже `min_severity` | `comments` | `COMMENT` | `neutral` |
| замечаний нет | `clean` | `COMMENT` | `success` |

**Комментарий** содержит severity, категорию и текст. Исправление показывается блоком
`suggestion`, если `before` совпадает со строками новой стороны диффа, иначе обычным diff-блоком.
В конце стоит скрытый маркер `<!-- dmc268:fp=… -->`.

**Отпечаток**: sha256 от пути, нормализованного текста замечания и кода на якорной строке.
Номер строки в него не входит, чтобы вставка выше по файлу не вызывала повторную публикацию.

**Summary** в Check Run содержит вердикт, summary модели, риски по severity, список
проверенных файлов и число замечаний, скрытых как уже опубликованные или сверх лимита.

## GitHub

- **Check Run «AI review»** создаётся на `head_sha` PR. Если вызывающий передал существующий
  Check Run с тем же SHA, он обновляется на месте.
- **Ревью** отправляется, только когда есть новые комментарии или вердикт требует изменений
  (`PublishPlan.needs_review`). Все комментарии уходят одним запросом
  `POST /repos/{owner}/{repo}/pulls/{n}/reviews`; сторона `new` становится `RIGHT`, `old` — `LEFT`.
- **422 на inline-комментарии**: ревью отправляется повторно без них, а их текст попадает в
  тело ревью.
- **Ответы GitHub**: 403 → `ForgePermissionError`; 429 или 403 с признаками лимита →
  `ForgeRateLimitedError`; 404 и 410 → `PrNotFoundError`; прочие ошибки →
  `ForgeUnavailableError`.
- Публикации в один репозиторий выполняются по очереди (`asyncio.Lock` на репозиторий).

Права GitHub App: `Pull requests: Read and write` и `Checks: Read and write`; установка должна
их принять.

## Статус по SSE

`GET /jobs/{job_id}/events` (`api/jobs.py`, контракт в `docs/openapi.yaml`) требует сессию.
На чужую или несуществующую задачу ответ одинаковый: 404.

```
retry: 3000

id: <seq>
event: snapshot | step | status | end
data: <json>
```

- `snapshot` приходит первым на каждом подключении: статус задачи и шаги на этот момент.
  После переподключения клиент рисует состояние заново по нему, `Last-Event-ID` не нужен.
- `step` и `status` приходят по мере хода задачи.
- `end` приходит, когда задача в конечном статусе, после чего сервер закрывает поток.
- `: ping` раз в 15 секунд держит соединение живым.

Поля события: `job_id`, `seq`, `at`, `status`, `step`, `step_status`, `stage`, `attempt`,
`error_kind`. `stage` — стадия прогресс-бара консоли (`domain/jobs.py`):

| `stage` | Шаги |
|---|---|
| `collecting_context` | `CLAIMED`, `AUTH`, `FETCH_DIFF`, `BUILD_CONTEXT`, `REDACT` |
| `reviewing` | `GATE`, `LLM_CALL` |
| `validating` | `POSTPROCESS` |
| `publishing` | `POST_FEEDBACK`, `METER` |

События API берёт из порта `JobEventsSource`. Реализация сейчас одна: в памяти процесса
(`adapters/memory/job_events.py`).

## Переменные окружения

Воркеру нужны `DATABASE_URL`, переменные LLM из [`docs/llm-gateway.md`](llm-gateway.md) и
переменные GitHub App: `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `GITHUB_APP_SLUG`,
`GITHUB_WEBHOOK_SECRET`.

## Проверки

Тесты: `tests/domain/test_publishing.py`, `tests/adapters/test_github_publisher.py`,
`tests/api/test_job_events.py`, `tests/worker/test_review_stage.py`, `tests/worker/test_main.py`.

Ручной смоук на тестовом PR (пишет в GitHub, не для CI):

```bash
set -a; . ./.env; set +a
python -m ops.publish_smoke --installation-id <id> --repo <you>/<test-repo> --pr <n>
```

Скрипт оставит в PR один комментарий на первой добавленной строке и Check Run «AI review» с
summary и напечатает ссылки. Полный путь проверяется push'ем в тестовый PR репозитория, где
установлен App.

## Решения

1. **Бот не блокирует мёрж.** Conclusion по умолчанию `neutral`, `request_changes_at = None`.
   Блокировка включается значением severity в `PublishPolicy`.
2. **Summary живёт в Check Run.** Ревью публикуется только с новыми комментариями или при
   блокировке, иначе каждый повторный прогон добавлял бы в PR «ничего нового».
3. **Check Run привязан к коммиту.** На том же SHA он обновляется, после push создаётся новый.
4. **Все комментарии одним запросом**, чтобы не упираться во вторичные лимиты GitHub на
   создание контента.
5. **`suggestion` только при точном совпадении `before`**, чтобы кнопка «Commit suggestion» не
   испортила файл.
6. **Маркер отпечатка в каждом комментарии.** По нему находятся id созданных комментариев, и по
   нему же список опубликованного можно восстановить с форжа.
7. **SSE дополняет polling, а не заменяет его.** Каждое событие несёт и шаг бэкенда, и `stage`
   консоли, так что консоль рисует прогресс, не зная набора шагов.

## Ограничения

- Повторный прогон публикует замечания заново: список уже опубликованных отпечатков пока пустой.
- `PublishPolicy` одна на все репозитории, значения по умолчанию.
- Исходящий текст не проходит редакцию секретов: `redact` по умолчанию возвращает текст как есть.
- Очередь публикаций в репозиторий действует только внутри одного процесса.
- Воркер не передаёт существующий Check Run, поэтому каждый прогон создаёт новый.
- Воркер не записывает шаги задачи, а источник событий в памяти: на сервере поток SSE
  отвечает 404.
- `GET /jobs` и `GET /jobs/{job_id}` не реализованы.
- GitLab не поддерживается.
