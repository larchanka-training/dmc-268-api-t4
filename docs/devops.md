# DevOps: окружение и CD

Окружение команды 4 работает на одном VPS. Адрес — IP сервера, домена нет, только HTTP:

- `http://<IP>/` — фронтенд (SPA из `dmc-268-ui-t4`);
- `http://<IP>/api/health` — бэкенд, ответ `{"status":"ok"}`.

IP хранится в secret `DEPLOY_HOST` и в репозиторий не попадает.

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
VPS (Debian 13), /opt/dmc268, docker compose project "dmc268"
  caddy:2 ── :80 наружу
    ├─ /api/*  → api:8000  (handle_path срезает /api)
    └─ всё остальное → /srv = /opt/dmc268/frontend, SPA fallback на /index.html
  api      ghcr…/dmc-268-api-t4:${IMAGE_TAG}   uvicorn main:app, порт не публикуется
  postgres postgres:17-alpine, volume pgdata, порт не публикуется
  redis    redis:7-alpine, volume redisdata, порт не публикуется
```

| Файл | Назначение |
|---|---|
| `Dockerfile`, `.dockerignore` | образ бэкенда: `python:3.13-slim`, `pip install .`, non-root uid 10001 |
| `deploy/compose.yml` | стек на сервере: api, postgres, redis, caddy |
| `deploy/Caddyfile` | `:80`, `/api/*` → api, остальное → статика фронтенда |
| `.github/workflows/ci.yml` | на PR и push в main: ruff, mypy, pytest; сборка образа, `/health` в контейнере, проверка `compose.yml` |
| `.github/workflows/deploy.yml` | на push в main и вручную: сборка → GHCR → деплой → smoke → проверка портов |
| `ops/bootstrap.sh`, `.github/workflows/bootstrap.yml` (ветка `devops/bootstrap`) | однократная подготовка сервера root-ом: Docker, пользователь `deploy`, `/opt/dmc268` |
| `ops/deploy_key.pub` (ветка `devops/bootstrap`) | публичный ключ CI для пользователя `deploy` |

## Секреты и переменные

Здесь только имена, значения живут в GitHub.

| Имя | Тип | Где | Для чего |
|---|---|---|---|
| `DEPLOY_HOST` | repo secret | api, ui | IP сервера. Secret, а не variable, чтобы маскироваться в логах с первого появления |
| `DEPLOY_SSH_KEY` | repo secret | api, ui | приватный ключ пользователя `deploy` (ed25519) |
| `DEPLOY_KNOWN_HOSTS` | repo secret | api, ui | строка known_hosts ключа хоста ED25519. Secret, потому что в ней IP |
| `POSTGRES_PASSWORD` | repo secret | api | пароль PostgreSQL, сгенерирован `openssl rand -hex 24` |
| `AI_DMC268_T4` | org secret | api | ключи Eurouter → `LLM_PRIMARY_API_KEYS` |
| `AI_DMC268_URL` | org variable | api | → `LLM_PRIMARY_BASE_URL` |
| `LLM_PRIMARY_MODEL` | repo variable | api | id модели Eurouter. **Пока не задана**: строка в `.env` не пишется, deploy выдаёт warning |
| `VPS_DMC268_U`, `VPS_DMC268_P` | org secret | — | root-вход по паролю, используется только в `bootstrap.yml` |
| `GITHUB_TOKEN` | автоматически | api | push образа в GHCR; на сервере `docker login` → pull → `docker logout` |

`/opt/dmc268/.env` на сервере создаётся из этих значений при каждом деплое: владелец `deploy`,
права `600`. В образ и в репозиторий секреты не попадают.

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

## Как добавить воркер

Кода воркера пока нет (раскладка `worker/` в `docs/BACKEND_ARCHITECTURE.md`), поэтому
в compose его нет. Когда код появится, воркер использует тот же образ с другой командой:

```yaml
  worker:
    image: ghcr.io/larchanka-training/dmc-268-api-t4:${IMAGE_TAG:?IMAGE_TAG must be set in .env}
    env_file: .env
    command: ["python", "-m", "worker.loop"]
    restart: unless-stopped
    stop_grace_period: 120s
    depends_on:
      postgres:
        condition: service_healthy
```

`worker` нужно добавить в `[tool.setuptools.packages.find] include`, иначе `pip install .`
не положит пакет в образ. `stop_grace_period` должен быть больше самого долгого ревью
(`docs/BACKEND_ARCHITECTURE.md` § Deployment). Порт воркеру не нужен.

## Решения

1. **Terraform не применяем.** Сервер выдан вручную, Terraform в репозиториях нет. Его
   заменяет идемпотентный `ops/bootstrap.sh`.
2. **Ветки `develop` нет**, деплой идёт только по push в `main`.
3. **Воркера в compose нет**, пока нет кода (см. «Как добавить воркер»). Это остаток DoD
   карточки #14.
4. **Redis поднимается** по карточке спринта, хотя по `docs/WORKFLOW_DESIGN.md` §1 очередь
   живёт в PostgreSQL. Приложение Redis пока не использует (см. «Открытые вопросы»).
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
10. **Порт наружу публикует только Caddy (80).** После каждого деплоя `ss -tuln` на сервере
    проверяет, что вне loopback и link-local слушаются только `22/tcp` и `80/tcp`; иначе
    деплой падает. Если понадобится 443 (HTTPS), его нужно разрешить в шаге «Check exposed
    ports». Дополнительно `/api/health` запрашивается с раннера через интернет, так что
    проверяется и доступность порта 80 снаружи, а не только `localhost` на сервере.
15. **LLMNR и mDNS выключены на сервере.** `systemd-resolved` слушал `5355/tcp` на всех
    интерфейсах. `ops/bootstrap.sh` кладёт drop-in
    `/etc/systemd/resolved.conf.d/90-dmc268-no-multicast.conf` (`LLMNR=no`,
    `MulticastDNS=no`) и перезапускает службу, только если drop-in изменился.
11. **PostgreSQL получает только свои переменные** через интерполяцию compose, а не весь
    `.env`: ключам LLM в контейнере БД делать нечего. api получает `.env` целиком.
12. **`main.py` копируется в образ отдельно.** Он не входит в пакеты setuptools
    (`include = ["domain*", "adapters*"]`), а uvicorn запускает `main:app` из `/app`.
13. **CI работает на системном `python3` раннера.** `actions/setup-python` нет в списке
    разрешённых actions; `requires-python >= 3.12` совпадает с Python на `ubuntu-latest`.
14. **Пользователь `deploy` в группе `docker`**, то есть фактически имеет права root на
    сервере. Это осознанная цена деплоя без sudo; ключ лежит только в secret `DEPLOY_SSH_KEY`.

## Открытые вопросы

1. **Redis или очередь в PostgreSQL.** Карточка спринта требует Redis, а
   `docs/WORKFLOW_DESIGN.md` §1 и `docs/BACKEND_ARCHITECTURE.md` объявляют PostgreSQL
   очередью «without Redis». Redis сейчас запущен, но не используется.
2. **Домен и HTTPS.** Без домена Caddy не может получить сертификат, и всё идёт по HTTP:
   cookie сессий и OAuth-callback в таком виде использовать нельзя. Нужен домен
   (A-запись на сервер), после чего в Caddyfile `:80` меняется на имя хоста, а в compose
   добавляется `443:443`.
3. **Root-вход по паролю на сервере.** Он открыт, а пароль лежит в секретах организации.
   Предложение владельцу: закрыть вход по паролю (`PermitRootLogin prohibit-password`),
   поставить fail2ban.
4. **Модель Eurouter.** `LLM_PRIMARY_MODEL` не выбрана. Пока она не задана как repo
   variable, `.env` не содержит модели, а смоук Gateway (`llm-smoke.yml`) не пройдёт.
