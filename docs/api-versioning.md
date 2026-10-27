# Версия API: переезд на `/api/v1` (#43)

`docs/openapi.yaml` объявляет сервер `https://{host}/api/v1`, а роуты были смонтированы от
корня: снаружи API отвечал на `/api/me`, `/api/webhooks/github` и т. д. Теперь каждый роут
смонтирован под `/v1` (`api/main.py`, `API_PREFIX` в `api/deps.py`). Caddy по-прежнему
срезает `/api`, uvicorn по-прежнему стоит с `--root-path /api`, так что публичные адреса
получаются `https://<site>/api/v1/...`, как в спеке.

Те же роуты временно отдаются и от корня (`/api/me`, `/api/webhooks/github`, ...). В схему
OpenAPI они не попадают. Так ни GitHub, ни уже открытый фронтенд не ломаются в момент выката,
а переключать их можно в любом порядке.

Без версии остаются только служебные `/` и `/health`: healthcheck Docker, smoke в
`deploy.yml` и будущий внешний uptime-чек ходят на `/api/health`.

## Что меняется и где

| Что | Где | Старое значение | Новое значение |
|---|---|---|---|
| Webhook URL | настройки GitHub App | `https://<site>/api/webhooks/github` | `https://<site>/api/v1/webhooks/github` |
| Setup URL | настройки GitHub App | `https://<site>/api/github/setup` | `https://<site>/api/v1/github/setup` |
| Callback URL входа | настройки GitHub App / OAuth App | `https://<site>/api/auth/github/callback` | добавить `https://<site>/api/v1/auth/github/callback` |
| `GITHUB_CALLBACK_URL` | `.github/workflows/deploy.yml`, `docker-compose.yml`, `ci.yml` | `.../auth/github/callback` | `.../v1/auth/github/callback` |
| `VITE_API_BASE_URL` | фронтенд: `deploy.yml`, `.env.example`, `src/app/config/env.ts`, моки | `/api` | `/api/v1` |
| Проксирование | `deploy/Caddyfile` | `handle_path /api/*` | без изменений |

## Порядок переключения

Каждый шаг можно выкатывать отдельно, откат шага означает возврат к старому значению.

1. **API (этот PR).** Деплой бэкенда. Проверка:
   `curl -s https://<site>/api/v1/me` и `curl -s https://<site>/api/me` оба отвечают
   `401 {"error":"no_session"}`, `/api/health` отвечает `{"status":"ok"}`.
2. **Фронтенд.** PR во фронтенд-репозитории с `VITE_API_BASE_URL=/api/v1`. Деплой только
   после шага 1, иначе запросы фронта получат 404. Проверка: вход, список репозиториев,
   страница ревью с живым прогрессом (SSE `/api/v1/jobs/{id}/events`).
3. **GitHub App** (делает владелец приложения в Settings → Developer settings → GitHub Apps):
   - Webhook URL → `https://<site>/api/v1/webhooks/github`; в Advanced → Recent Deliveries
     проверить, что новые доставки получают 2xx (кнопка Redeliver подходит для проверки);
   - Setup URL → `https://<site>/api/v1/github/setup`;
   - Callback URL: **добавить** `https://<site>/api/v1/auth/github/callback`, старый пока
     не удалять (GitHub App позволяет держать несколько callback URL).
4. **Callback в деплое.** После шага 3 поменять `GITHUB_CALLBACK_URL` в `deploy.yml` на
   `https://$SITE_ADDRESS/api/v1/auth/github/callback` (и локальные значения в
   `docker-compose.yml`, `ci.yml`, `docs/TEST_PLAN.md`, `docs/001-github-auth-backend.md`).
   Порядок важен: если поменять переменную раньше, чем зарегистрирован новый callback,
   GitHub ответит на вход ошибкой `redirect_uri mismatch`.
5. **Уборка.** Когда в access-логе API неделю нет запросов на роуты без `/v1` (поле
   `http.route`, кроме `/` и `/health`), удалить второй `include_router` в
   `_include_routes`, перевести тесты на `/v1/...`, удалить старый Callback URL в
   GitHub App и второй префикс в `_WEBHOOK_PREFIXES`.

## Что не меняется

- `deploy/Caddyfile`: `/api/*` по-прежнему уходит в API с отрезанным `/api`.
- Swagger UI остаётся на `https://<site>/api/docs`, схема на `/api/openapi.json`; в ней
  теперь только пути `/v1/...`.
- Формат ответов и cookie сессии (`__Host-session`, путь `/`) не меняются, так что
  пользователей не разлогинит.
