# DevOps: окружение и CD

Окружение команды 4 работает на одном VPS по HTTPS. Своего домена нет, имя сайта даёт
сервис sslip.io: имя `<IP с дефисами>.sslip.io` резолвится в сам IP.

- `https://<IP-с-дефисами>.sslip.io/` — фронтенд (SPA из `dmc-268-ui-t4`);
- `https://<IP-с-дефисами>.sslip.io/api/health` — бэкенд, ответ `{"status":"ok"}`;
- `http://…` на порт 80 перенаправляется на `https://…`.

IP хранится в secret `DEPLOY_HOST`. Имя сайта workflow вычисляют из него
(`${DEPLOY_HOST//./-}.sslip.io`) и сразу маскируют в логах. Ни IP, ни имя сайта в
репозиторий не попадают.

## Схема

```
push в main (dmc-268-api-t4)                push в main (dmc-268-ui-t4)
        │                                            │
        ▼                                            ▼
 deploy.yml: build ──► ghcr.io/larchanka-training/   deploy.yml: pnpm build
        │              dmc-268-api-t4:<sha>, :latest         │
        ▼                                                     ▼
 deploy (ssh deploy@VPS по ключу)                 rsync dist/ → /opt/dmc268/frontend
   scp compose.yml, Caddyfile → /opt/dmc268
   .env (umask 077) из GitHub Secrets
   docker compose pull && up -d
   smoke: curl localhost/api/health
        │
        ▼
VPS у Contabo (Debian 13), /opt/dmc268, docker compose project "dmc268"
  caddy:2 ── :80 (ACME HTTP-01, редирект на https) и :443/tcp наружу, сертификат Let's Encrypt в volume caddy_data
    ├─ /api/*  → api:8000  (handle_path срезает /api)
    └─ всё остальное → /srv = /opt/dmc268/frontend, SPA fallback на /index.html
  api      ghcr…/dmc-268-api-t4:${IMAGE_TAG}   uvicorn main:app, порт не публикуется   сети edge + db
  worker   тот же образ, python -m worker, ×WORKER_REPLICAS                     сети edge + db
  postgres postgres:17-alpine, volume pgdata, порт не публикуется              только сеть db (internal)
  backup   postgres:17-alpine, pg_dump раз в сутки в volume pgbackups (7 шт.)  только сеть db
  postgres-replica  postgres:17-alpine, streaming standby, volume pgreplica    только сеть db
  gatus    страница статуса на status.<SITE_ADDRESS> (через Caddy)            сети edge + db
```

| Файл | Назначение |
|---|---|
| `Dockerfile`, `.dockerignore` | образ бэкенда: `python:3.13-slim`, `pip install .`, non-root uid 10001 |
| `deploy/compose.yml` | стек на сервере: api, worker, postgres, postgres-replica, backup, gatus, caddy |
| `deploy/gatus.yml` | проверки страницы статуса |
| `ops/backup/`, `ops/replica/` | скрипт бэкапа; скрипт реплики и `pg_hba.conf` primary |
| `.github/workflows/llm-balance.yml` | вручную: остаток баланса Eurouter в Summary |
| `deploy/Caddyfile` | сайт `{$SITE_ADDRESS}` (из `.env`), автоматический HTTPS, `/api/*` → api, остальное → статика фронтенда |
| `.github/workflows/ci.yml` | на PR и push в main: ruff, mypy, pytest; сборка образа, `/health` в контейнере, проверка `compose.yml` |
| `.github/workflows/llm-smoke.yml` | вручную (`workflow_dispatch`): смоук LLM Gateway с живым ключом на `tests/fixtures/sql_injection.diff`; в Summary только provider, model и счётчики severity/category, текст модели в лог не попадает |
| `.github/workflows/deploy.yml` | на push в main и вручную: сборка → GHCR → деплой → smoke → проверка портов |
| `ops/bootstrap.sh`, `.github/workflows/bootstrap.yml` | однократная подготовка сервера root-ом: Docker, `rsync` (для деплоя фронтенда), пользователь `deploy`, `/opt/dmc268`, LLMNR/mDNS выключены. Запускается вручную (`workflow_dispatch`) или push в ветку `devops/bootstrap`, но не push в `main` |
| `ops/deploy_key.pub` | публичный ключ CI для пользователя `deploy` |
| `ops/observability/`, `.github/workflows/observability-sync.yml` | дашборды, правила алертов (с тестами `promtool` в CI), внешняя проверка и `sync.py`, который всё это отправляет в Grafana; workflow запускается вручную (docs/observability.md) |
| `deploy/lab/` | лаборатория наблюдаемости на ноутбуке: Alloy и конфиги как в продакшене, локальные Prometheus, Loki и Grafana; `lab.py test` проверяет весь путь, `lab.py drill` репетирует сбои (deploy/lab/README.md) |

## Секреты и переменные

Здесь только имена, значения живут в GitHub.

| Имя | Тип | Где | Для чего |
|---|---|---|---|
| `DEPLOY_HOST` | repo secret | api, ui | IP сервера. Secret, а не variable, чтобы маскироваться в логах с первого появления. Из него вычисляется имя сайта `SITE_ADDRESS` |
| `DEPLOY_SSH_KEY` | repo secret | api, ui | приватный ключ пользователя `deploy` (ed25519) |
| `DEPLOY_KNOWN_HOSTS` | repo secret | api, ui | строка known_hosts ключа хоста ED25519. Secret, потому что в ней IP |
| `POSTGRES_PASSWORD` | repo secret | api | пароль PostgreSQL, сгенерирован `openssl rand -hex 24` |
| `AI_DMC268_T4` | org secret | api | ключи Eurouter → `LLM_PRIMARY_API_KEYS` |
| `AI_DMC268_URL` | org variable | api | → `LLM_PRIMARY_BASE_URL` |
| `LLM_PRIMARY_MODEL` | repo variable | api | id модели Eurouter, с 22.10 `qwen3-coder-next-80b` (провайдер ionos, `docs/specs/llm-gateway.md`). Если переменную удалить, строка в `.env` не пишется, deploy выдаёт warning |
| `LLM_PRIMARY_PROVIDER_ORDER` | repo variable | api, llm-smoke | провайдеры Eurouter по порядку, например `scaleway,ovhcloud`. Пусто = маршрутизация Eurouter по умолчанию. Подробности в `docs/llm-gateway.md` |
| `SENTRY_DSN` | repo secret | api | DSN проекта Sentry → `SENTRY_DSN` в `.env` для api и worker. Без него деплой не стартует (docs/observability.md) |
| `GRAFANA_CLOUD_PROM_URL`, `GRAFANA_CLOUD_PROM_USER` | repo variable | api | Prometheus стека Grafana Cloud: URL (базовый, query или push: деплой пишет push) и id пользователя → `.env` для Alloy (решение 22) |
| `GRAFANA_CLOUD_LOKI_URL`, `GRAFANA_CLOUD_LOKI_USER` | repo variable | api | то же для Loki |
| `GRAFANA_CLOUD_TOKEN` | repo secret | api | токен access policy Grafana Cloud (metrics write, logs write). В контейнер его получает только Alloy, через интерполяцию compose |
| `POSTGRES_EXPORTER_PASSWORD` | repo secret | api | пароль read-only роли `dmc268_exporter` (`pg_monitor`) для экспортера Postgres в Alloy; сгенерирован `openssl rand -hex 32`. Роль создаётся один раз вручную (tasks/observability-checklist.md, фаза 3) |
| `SENTRY_WEBHOOK_SECRET` | repo secret | api | client secret внутренней интеграции Sentry `telegram-relay`: им подписана каждая доставка реле → `.env`. Без него `/api/internal/alerts/sentry` отвечает 404 |
| `ALERTS_TELEGRAM_BOT_TOKEN`, `ALERTS_TELEGRAM_CHAT_ID` | repo secret | api | бот и чат команды для реле Sentry → `.env`. Без них реле пишет в лог `alert.dropped` вместо отправки. Те же значения один раз вводятся вручную в contact point `telegram` в Grafana |
| `GRAFANA_URL` | repo variable | api | адрес стека Grafana Cloud (`https://<стек>.grafana.net`): аннотация деплоя в `deploy.yml` и `observability-sync.yml` |
| `GRAFANA_SA_TOKEN` | repo secret | api | токен service account Grafana (дашборды, аннотации, alerting): аннотация деплоя и `ops/observability/sync.py`. Только CI |
| `SENTRY_AUTH_TOKEN` | repo secret | api | Organization Token Sentry (`org:ci`): релиз, коммиты и деплой `production` в `deploy.yml`. Только CI |
| `GRAFANA_SM_URL`, `GRAFANA_SM_TOKEN` | repo variable, repo secret | api | API Synthetic Monitoring и его токен: внешняя проверка `/api/health`, которую создаёт `sync.py`. Без них проверка пропускается с warning |
| `GRAFANA_PROM_DATASOURCE_UID`, `SENTRY_ORG_URL` | repo variable, необязательные | api | uid источника Prometheus для правил алертов (по умолчанию `grafanacloud-prom`) и адрес организации Sentry для ссылки на трейсы с дашборда API |
| `VPS_DMC268_U`, `VPS_DMC268_P` | org secret | — | root-вход по паролю, используется только в `bootstrap.yml` |
| `WORKER_REPLICAS` | repo variable | api | число контейнеров воркера, по умолчанию `1` |
| `WORKER_CONCURRENCY` | repo variable | api | слотов на контейнер воркера, по умолчанию `5` |
| `GITHUB_TOKEN` | автоматически | api | push образа в GHCR; на сервере `docker login` → pull → `docker logout` |

`/opt/dmc268/.env` на сервере создаётся из этих значений при каждом деплое: владелец `deploy`,
права `600`. В образ и в репозиторий секреты не попадают. Ещё деплой пишет константы:
`LLM_TIMEOUT_SECONDS=300` (решение 17), `ENV=production` и `LOG_DIR=/logs` (JSON-логи в
ротируемые файлы `/opt/dmc268/logs`, см. docs/observability.md § Local fallback). Если
заданы все шесть настроек Grafana Cloud и экспортера, ещё `COMPOSE_PROFILES=monitoring`,
и compose запускает Alloy; если хоть одной нет, деплой выдаёт warning и идёт без Alloy.

## Как задеплоить вручную

Actions → **Deploy** → *Run workflow* (ветка `main`) с пустым `image_tag` — собрать текущий
коммит и выложить его:

```bash
gh workflow run deploy.yml -R larchanka-training/dmc-268-api-t4 --ref main
```

## Как откатиться

Каждый деплой публикует образ с тегом полного sha коммита. Чтобы откатиться, запустите
**Deploy** с `image_tag` = sha старого коммита. Сборка тогда пропускается, на сервер
выкладывается указанный образ:

```bash
gh workflow run deploy.yml -R larchanka-training/dmc-268-api-t4 --ref main -f image_tag=<старый sha>
```

То же самое прямо на сервере (пользователь `deploy`):
`cd /opt/dmc268 && IMAGE_TAG=<старый sha> docker compose up -d api`. Переменная окружения
перекрывает `IMAGE_TAG` из `.env`, и следующий деплой вернёт актуальный тег.

## Масштабирование воркеров

Воркер уже в `deploy/compose.yml` (тот же образ, `python -m worker`, `stop_grace_period: 10m`).
Масштабируется двумя переменными репозитория без правок кода:

- `WORKER_REPLICAS` — число контейнеров (`deploy.replicas` в compose);
- `WORKER_CONCURRENCY` — параллельных ревью в одном контейнере (по умолчанию 5).

Несколько контейнеров безопасны: задачи забираются через `SKIP LOCKED`, а периодические задачи
(reaper) берут advisory lock (`worker/periodic.py`). Ограничения:

- **Соединения с БД.** Каждый контейнер открывает до `POOL_SIZE` (7) соединений плюс одно для
  `LISTEN`. `WORKER_REPLICAS × 8` + api должно оставаться заметно ниже `max_connections` (100).
- **Лимиты LLM-провайдера.** Потолок задают параллельные запросы к Eurouter и вторичные лимиты
  GitHub, а не CPU (`docs/BACKEND_ARCHITECTURE.md` § Processes). **Заглушка:** лимиты Eurouter
  на ключ неизвестны, ждём их от владельца ключа `AI_DMC268_T4`.
- **Метрики воркера.** Alloy скрейпит `worker:9101` по имени сервиса; при `WORKER_REPLICAS` > 1
  Docker DNS отдаёт по очереди разные контейнеры, и метрики воркера становятся неполными.
  Для нескольких реплик нужен discovery по контейнерам в `deploy/alloy/config.alloy`
  (вопрос к DevOps спринта 3).
- **Память VPS.** Swap нет (решение 7), каждый контейнер воркера — отдельный Python-процесс.
- **Целевое число воркеров.** **Заглушка:** ждём техлида. По `BACKEND_ARCHITECTURE.md` § Scale
  envelope одного воркера с `WORKER_CONCURRENCY=5` хватает до сотен ревью в час.

Проверить вручную на сервере: `cd /opt/dmc268 && docker compose up -d --scale worker=2`.
Следующий деплой вернёт число из `WORKER_REPLICAS`.

## Прод-окружение

Прод — это текущий VPS (решение 25). Что сделано в спринте 4:

- **Изолированная сеть.** Две сети compose: `edge` (Caddy, api, worker; выход в интернет нужен
  api и воркеру для GitHub и LLM) и `db` с `internal: true` (api, worker, postgres). У PostgreSQL
  нет маршрута наружу, Caddy до него не достаёт. Порты наружу по-прежнему только 80 и 443.
- **Логи.** У всех сервисов `json-file` с `max-size: 20m`, `max-file: 5`, иначе логи Docker
  растут до заполнения диска.
- **Старые образы.** После деплоя удаляются неиспользуемые образы старше 7 дней
  (`docker image prune --all --filter until=168h`). Раньше `docker image prune` удалял только
  dangling-образы, а образы с тегом sha копились. Откат на более старый sha скачает образ из
  GHCR заново.
- **Бэкапы БД** раз в сутки на этом же сервере, см. «Бэкапы PostgreSQL».
- **Реплика БД** в соседнем контейнере на этом же сервере, см. «Реплика PostgreSQL».
- **Страница статуса** на этом же сервере, см. «Страница статуса».
- **Healthcheck** добавлен Caddy (read-only запрос к admin API на `127.0.0.1:2019` внутри
  контейнера). У воркера его нет, см. заглушку ниже.

**Заглушки, которые ждут решений:**

| Что | Ждём от кого | Что нужно | Где заглушка |
|---|---|---|---|
| Реплика и страница статуса на втором сервере | Владелец сервера | Второй VPS; туда переезжают `postgres-replica` и `gatus` (решение 26) | `ops/replica/standby.sh`, `deploy/gatus.yml`, `deploy/compose.yml` |
| Копия бэкапов вне VPS | Команда: кто заводит хранилище | S3-совместимый бакет (Cloudflare R2, Backblaze B2 или Contabo Object Storage) и ключи в GitHub Secrets; затем шифрование и отправка дампа | `ops/backup/pg-backup.sh`, `deploy/compose.yml` |
| Healthcheck воркера | Бэкенд (владелец воркера) | Heartbeat из цикла воркера (файл или строка в БД) | `deploy/compose.yml`, сервис `worker` |
| Свой домен | Владелец сервера / организатор | Домен вместо `sslip.io`; тогда меняется `SITE_ADDRESS` и три URL GitHub App (решение 18) | — |
| Root-вход по паролю | Владелец сервера | Согласие закрыть его и поставить fail2ban | `ops/bootstrap.sh` |

## Бэкапы PostgreSQL

Сервис `backup` в `deploy/compose.yml` (образ `postgres:17-alpine`, скрипт
`ops/backup/pg-backup.sh`): раз в час проверяет, когда был последний дамп, и если больше
24 часов назад — делает `pg_dump --format=custom` в volume `pgbackups`. Хранятся 7 последних.
Дамп пишется во временный файл, проверяется `pg_restore --list` и только потом
переименовывается, так что недописанный файл бэкапом не считается. Перезапуски и деплои
лишних дампов не создают.

**Заглушка:** бэкапы лежат на этом же VPS (решение 27.10: пока проще). Они спасают от ошибок
с данными (неудачная миграция, случайное удаление), но не от потери сервера. Для копии вне VPS
нужно хранилище (см. таблицу заглушек выше).

Команды на сервере (пользователь `deploy`, `cd /opt/dmc268`):

```bash
# список бэкапов
docker compose exec backup ls -lh /backups
# сделать бэкап прямо сейчас (например, перед миграцией)
docker compose exec backup sh /opt/backup/pg-backup.sh --now
# скопировать бэкап на сервер
docker compose cp backup:/backups/<файл>.dump ./
```

**Восстановление** (в тихий период; api и worker останавливаются, чтобы не писали в базу):

```bash
docker compose stop api worker
docker compose exec backup sh -c 'pg_restore --clean --if-exists --no-owner -d "$PGDATABASE" /backups/<файл>.dump'
docker compose start api worker
```

**Проверка восстановления** раз в месяц, без влияния на рабочую базу:

```bash
docker compose exec backup sh -c 'createdb restore_check && pg_restore --no-owner -d restore_check "$(ls -1t /backups/*.dump | head -n 1)" && psql -d restore_check -Atc "select count(*) from pr_review_jobs"; dropdb restore_check'
```

## Реплика PostgreSQL

Сервис `postgres-replica` — streaming standby (физическая репликация), только чтение. При
первом старте `ops/replica/standby.sh` копирует primary через `pg_basebackup` по сети `db` и
создаёт слот `replica1`; дальше реплика сама получает WAL. Primary запускается со своим
`pg_hba.conf` (`ops/replica/pg_hba.conf`, в нём строка `replication`) и
`max_slot_wal_keep_size=2GB`: если реплика лежит, primary держит не больше 2 ГБ WAL и диск не
забивается. Реплика подключается тем же пользователем, что и приложение (суперпользователь
образа), отдельного секрета не нужно.

Пока реплика на том же сервере (решение 26): защищает от поломки файлов primary, но не от
потери сервера. От ошибок с данными защищают бэкапы, реплика повторяет и ошибки.

```bash
cd /opt/dmc268
# реплика получает WAL: state = streaming, отставание в байтах
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select client_addr, state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) as lag_bytes from pg_stat_replication"'
# слот и сколько WAL он держит
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select slot_name, active, wal_status from pg_replication_slots"'
```

Пересоздать реплику (например, после восстановления primary из бэкапа или если `wal_status`
стал `lost`):

```bash
docker compose rm -sf postgres-replica
docker volume rm dmc268_pgreplica
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select pg_drop_replication_slot(slot_name) from pg_replication_slots where not active"'
docker compose up -d postgres-replica
```

Переключиться на реплику, если primary сломан: `docker compose exec postgres-replica su-exec
postgres pg_ctl promote`, затем поменять хост в `DATABASE_URL` на `postgres-replica`. Это
ручной шаг на крайний случай; автоматического переключения нет.

## Страница статуса

`https://status.<SITE_ADDRESS>` — Gatus (`deploy/gatus.yml`). `sslip.io` отдаёт IP для любого
поддомена, поэтому DNS-запись не нужна, сертификат Caddy выпустит сам. Раз в минуту проверяет:
сайт и `/api/health` снаружи (через интернет, Caddy и HTTPS, плюс срок сертификата не меньше
14 дней), API изнутри, порты PostgreSQL и реплики. История хранится в volume `gatus_data`.
Чтобы добавить проверку, достаточно дописать её в `deploy/gatus.yml` и задеплоить.

Ограничения: страница на том же сервере (решение 26) и упадёт вместе с ним; воркера на ней
нет, пока у него нет heartbeat; уведомлений она не шлёт, алерты в Telegram — по плану
`docs/observability.md`.

## Сертификаты Let's Encrypt

Caddy сам выпускает и продлевает сертификат (HTTP-01, volume `caddy_data`). Продление начинается
примерно за треть срока до окончания, поэтому при переходе Let's Encrypt на 45-дневные
сертификаты Caddy будет продлевать их примерно раз в месяц сам, без настройки. Cert-Manager нужен
только в Kubernetes, у нас его нет. Вместо ручного графика стоит алерт: если сертификату осталось
меньше 14 дней, продление сломано (правило `TlsCertificateExpiry`, `ops/observability/alerts/edge.rules.yaml`). Каждый деплой
дополнительно печатает срок сертификата в Summary.

## Алерты

Алерты спринта 4 входят в план мониторинга DevOps спринта 3 (`docs/observability.md`,
фазы 3–4): метрики и логи собирает Alloy, правила лежат в `ops/observability/alerts/`, ошибки
приходят из Sentry через реле, всё уходит в один Telegram-чат. Своих алертов не делаем, чтобы
не было дублей. Runbook на каждый алерт — `docs/runbooks.md`.

| Что требует спринт 4 | Правило | Состояние |
|---|---|---|
| Падение сервисов | `TargetDown`, `TelemetryMissing`, `SiteUnreachable` (внешняя проверка Synthetic Monitoring), `ContainerRestarts` | работает после включения (ниже) |
| Всплеск 5xx | `ApiErrorRate`, `Edge5xx` (метрики Caddy на `:2020`) | то же |
| Переполнение диска | `HostResources` (диск > 85 %) | то же |
| Срок сертификата | `TlsCertificateExpiry` (< 14 дней) | то же |
| Приближение к бюджету LLM | `LlmSpend`: `llm_cost_usd_total` против `llm_daily_budget_usd` | молчит до фазы 5: ждёт решения команды о ценах и дневном бюджете в USD (`tasks/observability-checklist.md`). Остаток баланса Eurouter пока смотрим вручную, workflow **LLM balance** |
| Страница статуса (опц.) | в плане нет | своя на Gatus, см. «Страница статуса» |

Alloy сидит в сетях `edge` (отправка в Grafana Cloud, метрики api, worker, caddy) и `db`
(экспортер Postgres). Telegram: secrets `ALERTS_TELEGRAM_BOT_TOKEN` и `ALERTS_TELEGRAM_CHAT_ID`
(реле Sentry), те же значения один раз вводятся в contact point `telegram` в Grafana.

Включение после мержа (`tasks/observability-checklist.md`, фазы 3–4): деплой без warning
«Alloy not started», в Grafana `up` = 1 у всех целей, Actions → **Observability sync**
(сначала `dry_run`), токен бота в contact point `telegram`, правила алертов в Sentry, учения
(остановить api → один алерт `TargetDown` в Telegram).

## SLA

Берём SLO из `docs/observability.md` § SLOs (DevOps спринта 3), чтобы у команды был один набор
целей: доступность API 99,5 % запросов без 5xx, 95 % ревью опубликованы за 10 минут, 99 %
ревью завершаются успешно, 99,9 % валидных вебхуков приняты. **Заглушка:** цели
предварительные, ждут утверждения техлидом / Mikhail и месяца данных (фаза 6 того плана).

## Ротация секретов

Пункт карточки спринта 4 «ротация секретов». Где живут секреты — в таблице выше. После смены значения в GitHub достаточно запустить **Deploy**: `.env` и
`github-app.pem` перезаписываются целиком.

| Секрет | Как сменить | Нужен ли простой |
|---|---|---|
| `DEPLOY_SSH_KEY` | Новый ключ ed25519 → `ops/deploy_key.pub` → **Bootstrap** (кладёт `authorized_keys`) → secret → **Deploy** | нет |
| `POSTGRES_PASSWORD` | На сервере `docker compose exec postgres psql -U dmc -c "ALTER USER dmc PASSWORD '…'"`, затем secret → **Deploy**. Только secret не поможет: пароль в volume `pgdata` задаётся один раз при создании | секунды, пока api и воркер перезапускаются |
| `AI_DMC268_T4` | Владелец org-секрета выпускает новый ключ → secret → **Deploy** → отозвать старый | нет |
| `GH_CLIENT_SECRET`, `GH_APP_PRIVATE_KEY` | Новый секрет / ключ в настройках GitHub App → secret → **Deploy** → удалить старый в App | нет, у App может быть два ключа одновременно |
| `GH_WEBHOOK_SECRET` | Одновременно в GitHub App и в secret → **Deploy**. Доставки в промежутке получат 401; их можно переотправить вручную из Recent Deliveries в настройках App | минуты |

## Решения

1. **Terraform не применяем.** Сервер выдан вручную, Terraform в репозиториях нет. Его
   заменяет идемпотентный `ops/bootstrap.sh`.
2. **Ветки `develop` нет**, деплой идёт только по push в `main`.
3. **Воркер в compose**, тот же образ с командой `python -m worker` (см. «Масштабирование
   воркеров»).
4. **Очередь в PostgreSQL, Redis не используется** (решение команды, 2026-10-04).
5. **IP в workflow берётся из secret `DEPLOY_HOST`**, а не из `vars.VPS_DMC268_IP_T4`:
   переменная не маскируется и уже однажды попала в публичный лог.
   `DEPLOY_KNOWN_HOSTS` тоже secret, потому что строка known_hosts содержит IP.
6. **CI входит на сервер пользователем `deploy` по ключу.** Root-пароль используется только
   в `bootstrap.yml`. Ключ из secret загружается в `ssh-agent` и на диск раннера не пишется.
   Ключ хоста закреплён (`StrictHostKeyChecking yes`), fingerprint ED25519 сверен с
   разведкой: `SHA256:thboimir3k5nXDksardL0AR+0o+pmSf2ncadd18m1pU`.
7. **Образ собирается в CI** (на сервере нет swap) и публикуется в GHCR с тегом sha, а
   `latest` ставится только для `main`. Деплой всегда идёт по sha, `latest` нужен для
   удобства. `docker login` на сервере живёт только на время pull, затем `docker logout`.
8. **Секреты рантайма лежат в `/opt/dmc268/.env`**, который пишется по SSH из GitHub
   Secrets (`umask 077`, атомарно через `.env.new` → `mv`). Vault и Doppler не используем.
   Значения очищаются от `\r`/`\n`, чтобы секрет с переводом строки не сломал файл.
9. **Root-вход по паролю не отключён** — это решение владельца сервера (см. «Открытые вопросы»).
10. **Порты наружу публикует только Caddy: 80 и 443/tcp.** После каждого деплоя `ss -tuln` на
    сервере проверяет, что вне loopback и link-local слушаются только `22/tcp`, `80/tcp` и
    `443/tcp`; иначе деплой падает. HTTP/3 (443/udp) не публикуется. Дополнительно с раннера
    через интернет проверяются `https://<site>/api/health` с проверкой сертификата (curl
    без `-k`) и редирект `http` → `https`.
11. **PostgreSQL получает только свои переменные** через интерполяцию compose, а не весь
    `.env`: ключам LLM в контейнере БД делать нечего. api получает `.env` целиком.
12. **`main.py` копируется в образ отдельно.** Он не входит в пакеты setuptools
    (`include = ["domain*", "adapters*"]`), а uvicorn запускает `main:app` из `/app`.
13. **CI работает на системном `python3` раннера.** `actions/setup-python` нет в списке
    разрешённых actions; `requires-python >= 3.12` совпадает с Python на `ubuntu-latest`.
14. **Пользователь `deploy` в группе `docker`**, то есть фактически имеет права root на
    сервере. Это осознанная цена деплоя без sudo; ключ лежит только в secret `DEPLOY_SSH_KEY`.
15. **LLMNR и mDNS выключены на сервере.** `systemd-resolved` слушал `5355/tcp` на всех
    интерфейсах. `ops/bootstrap.sh` кладёт drop-in
    `/etc/systemd/resolved.conf.d/90-dmc268-no-multicast.conf` (`LLMNR=no`,
    `MulticastDNS=no`) и перезапускает службу, только если drop-in изменился.
16. **Bootstrap не запускается от push в `main`.** Он меняет сервер с правами root, поэтому
    срабатывает только вручную (`workflow_dispatch`) или от push в ветку `devops/bootstrap`.
    Повторный прогон безопасен: скрипт идемпотентный.
17. **`LLM_TIMEOUT_SECONDS=300`** в `.env` (пишет `deploy.yml`) и в `llm-smoke.yml`, вместо
    120 по умолчанию. `glm-5.2` медленная: смоук на маленьком диффе из `tests/fixtures`
    занял ~95 с, так что на реальных PR 120 с не хватит. Job смоука поднят до 15 минут:
    два запроса (ответ и корректирующий повтор при невалидном JSON) по 300 с — это до
    10 минут.
18. **HTTPS через sslip.io.** Своего домена нет, а cookie `__Host-*` для входа через GitHub
    требуют `Secure`, то есть HTTPS. Имя `<IP с дефисами>.sslip.io` резолвится в IP сервера,
    и Caddy получает на него сертификат Let's Encrypt (HTTP-01 через порт 80). Имя
    вычисляется в `deploy.yml` из `DEPLOY_HOST`, маскируется и пишется в `.env` как
    `SITE_ADDRESS`; оттуда же `APP_ORIGIN=https://<site>` и
    `GITHUB_CALLBACK_URL=https://<site>/api/auth/github/callback`. Volume `caddy_data`
    обязателен: без него сертификат перевыпускался бы на каждом деплое и упёрся бы в лимиты
    Let's Encrypt. Первый выпуск занимает до минуты, поэтому smoke делает повторы.
    **В настройках GitHub App** нужно указать адреса на https: Callback URL
    `https://<site>/api/auth/github/callback`, Setup URL `https://<site>/api/github/setup`
    и Webhook URL `https://<site>/api/webhooks/github`. Префикс `/api` обязателен: без него
    Caddy отдаёт запрос фронтенду, и GitHub получает 405. В коде и в `openapi.yaml` путь
    записан без префикса (`/webhooks/github`), потому что Caddy срезает `/api`.
    Если появится свой домен, меняется только `SITE_ADDRESS` (и эти три URL).
19. **Старый адрес `http://<IP>` ведёт на имя сайта.** Сертификата на голый IP нет, поэтому
    стандартный редирект Caddy на `https://<IP>` в браузере давал ошибку. В Caddyfile отдельный
    блок `http://{$SERVER_IP}` отвечает 308 на `https://<site>{uri}`. `SERVER_IP` пишет в `.env`
    `deploy.yml` из `DEPLOY_HOST`, smoke проверяет этот редирект.
20. **Swagger за префиксом `/api`.** Caddy срезает `/api` перед бэкендом, поэтому uvicorn
    запускается с `--root-path /api` (Dockerfile): роуты не меняются, а `/api/docs` грузит
    `/api/openapi.json`, а не `/openapi.json`, который отдал бы фронтенд.
21. **Хостер: Contabo**, а не Hetzner из карточки спринта 1: сервер выдан вручную,
    это видно в приветствии SSH.
22. **Метрики и логи уходят в Grafana Cloud через Alloy.** Сервис `alloy` в compose
    (профиль `monitoring`, docs/observability.md § Metrics) скрейпит `api:9100`,
    `worker:9101` и `caddy:2020`, запускает экспортеры хоста, контейнеров и Postgres,
    читает `./logs` и отправляет всё в Grafana Cloud. Портов он не публикует, и эти три
    порта доступны только в сети compose, так что проверка портов (решение 10) не
    меняется; smoke проверяет, что `/api/metrics` отвечает 404. Сокеты Docker и
    containerd смонтированы read-only: это эквивалент root, та же осознанная цена, что в
    решении 14. Postgres запускается с `shared_preload_libraries=pg_stat_statements`,
    поэтому первый деплой с этой командой перезапускает Postgres (несколько секунд).
23. **Cert-Manager не используем.** Он для Kubernetes; сертификаты выпускает и продлевает Caddy,
    продление контролирует алерт TLS certificate expiry из `docs/observability.md`.
24. **PostgreSQL только во внутренней сети `db`.** Выход в интернет из контейнера БД не нужен.
25. **Прод — текущий VPS** (решение 2026-10-27). Отдельного сервера под прод нет; staging
    тоже нет, поэтому изменения деплоя проверяются CI и смоуком после деплоя.
26. **Реплика БД и страница статуса пока на этом же VPS** (решение 2026-10-27). Когда появится
    второй сервер, `postgres-replica` и `gatus` переезжают туда; порт 5432 primary тогда
    открывается только для IP второго сервера.

## Открытые вопросы

1. **Root-вход по паролю на сервере.** Он открыт, а пароль лежит в секретах организации.
   Предложение владельцу: закрыть вход по паролю (`PermitRootLogin prohibit-password`),
   поставить fail2ban.
2. **Модели Anthropic в Eurouter недоступны.** Для `claude-sonnet-4-6`, `claude-haiku-4.5` и
   других Eurouter отвечает `400 No providers available for model … with given preferences`.
   Причина (документация Eurouter, раздел Routing → Data residency): по умолчанию запросы идут
   только к провайдерам с EU-хостингом, а Claude в Eurouter доступен только через AWS Bedrock
   вне EU. Сейчас используется `qwen3-coder-30b-a3b` (рекомендация Миши) с
   `LLM_PRIMARY_PROVIDER_ORDER=scaleway,ovhcloud`. Вернуть Claude можно, только ослабив
   требование EU-хостинга, а это решение команды по данным.
3. **Спринт 4: SLO и второй сервер.** Утверждение SLO (раздел «SLA») — вопрос к техлиду.
   Второй сервер под реплику и страницу статуса (решение 26) — вопрос к владельцу сервера.
4. **Бюджет LLM.** У Eurouter предоплаченный баланс в EUR: при балансе меньше €0,10 API отвечает
   `402 payment_required`. Остаток отдаёт `GET /api/v1/credits` (`total_credits`, `total_usage`)
   тем же ключом. Лимит запросов по тарифу: Free 60 RPM и 10 тыс. запросов в месяц, Plus 100 RPM,
   Pro 500 RPM. Остаток можно посмотреть workflow **LLM balance** (Actions → Run workflow).
   Нужно от владельца аккаунта: тариф и порог алерта (дневной бюджет или минимальный остаток
   в EUR).
