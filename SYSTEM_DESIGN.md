# SYSTEM_DESIGN

## 1. Purpose

AI Code Review Platform --- платформа для автоматизированного code
review изменений в GitHub и GitLab.

Основные пользовательские сценарии:

-   вход через GitHub OAuth (GitLab OAuth --- Phase 2);
-   подключение repositories через GitHub PAT (GitLab --- Phase 2);
-   обработка событий Pull Request (Merge Request --- Phase 2);
-   запуск code review по webhook-триггеру или по запросу из
    web-интерфейса;
-   сбор diff и необходимого контекста;
-   анализ изменений через LLM Provider Interface;
-   валидация результатов;
-   публикация summary и inline comments обратно в GitHub;
-   управление Organization, участниками, repositories и настройками;
-   управление subscription и оплатой через Stripe;
-   web-интерфейс для управления участниками, repositories,
    subscription и оплатой.

### Scope

MVP:

-   GitHub OAuth;
-   подключение repositories через PAT;
-   роли owner/member;
-   LLM providers: OpenAI, Anthropic;
-   Stripe subscriptions;
-   очередь поверх PostgreSQL;
-   развертывание на одном VDS.

Phase 2 (паттерны закладываются в дизайн, реализация отложена):

-   GitLab: OAuth, adapter, Merge Requests;
-   GitHub App installation flow;
-   роль Platform Admin;
-   BYOK-ключи LLM; дополнительные LLM providers (Gemini, Custom);
-   metered overage billing (`billing_events` пишутся с первого дня).

## 2. Architecture Overview

Архитектура основных application containers:

``` text
┌──────────────────────┐
│    Web Frontend      │      ┌──────────────────────┐
│ React + TypeScript   │      │  GitHub / GitLab /   │
└──────────┬───────────┘      │       Stripe         │
           │ REST             └──────────┬───────────┘
           ▼                         webhooks
┌──────────────────────────────────────────┐
│               API / Backend              │
│              Modular Monolith            │
└──────────┬───────────────────────────────┘
           │ Review Job
           ▼
┌──────────────────────┐
│      Job Queue       │
│    (PostgreSQL)      │
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│    Review Worker     │
│ Preflight → Context  │
│ → LLM → Validation   │
│ → Publish            │
└──────────────────────┘
```

**Technology:**
-   Frontend --- React + TypeScript;
-   Backend --- Python + FastAPI.


**Infrastructure:**
-   PostgreSQL --- primary persistent storage;
-   Queue --- review jobs поверх PostgreSQL (procrastinate);
-   VDS --- infrastructure.

Внешние системы:

-   GitHub;
-   GitLab (Phase 2);
-   Stripe;
-   LLM Providers.

## 3. System Context

``` text
                          ┌───────────────────┐
                          │       User        │
                          │  Owner / Member   │
                          └─────────┬─────────┘
                                    │ HTTPS
                                    ▼
                          ┌───────────────────┐
                          │   Web Frontend    │
                          │ React + TypeScript│
                          └─────────┬─────────┘
                                    │
                                    ▼
               ┌────────────────────────────────────┐
               │      AI Code Review Platform       │
               └──────┬─────────┬──────────┬────────┘
                      │         │          │
                      │         │          │
               ┌──────▼───┐ ┌──▼──────┐ ┌─▼──────────┐
               │ GitHub   │ │ GitLab  │ │ LLM        │
               │          │ │         │ │ Providers  │
               └──────────┘ └─────────┘ └────────────┘
                      │
                      │ Subscription / Checkout
                      ▼
                 ┌──────────┐
                 │  Stripe  │
                 └──────────┘

                          VDS
                 ┌─────────────────────┐
                 │ PostgreSQL          │
                 │ Queue               │
                 └─────────────────────┘
```

## 4. Container Architecture

### 4.1 Web Frontend

Основные frontend components:

``` text
Web Frontend
│
├── Application Shell
│   ├── Header
│   ├── Navigation
│   ├── Global Search
│   └── User / Organization Context
│
├── Dashboard
├── Review UI
├── Repository UI
├── Integrations UI
├── Billing UI
├── Settings UI
└── Admin UI
```

Frontend services:

``` text
Frontend Services
│
├── API Client
├── Auth State
├── Organization Context
├── Review State
└── Query Cache
```

Frontend взаимодействует с Backend через HTTPS/REST.

### 4.2 API / Backend

Backend реализован как modular monolith.

Основные modules:

``` text
API / Backend
│
├── API Layer
├── Auth Module
├── Organization Module
├── Billing Module
├── Integration Module
├── Review Module
└── Persistence Layer
```

#### API Layer

Отвечает за:

-   REST controllers;
-   request validation;
-   authentication middleware;
-   authorization middleware;
-   OAuth callbacks;
-   GitHub/GitLab webhooks;
-   Stripe webhooks.

#### Auth Module

Отвечает за identity и authentication.

Components:

-   OAuth Service;
-   Identity Service;
-   Session Service;
-   Auth Repository.

Поддерживаются:

-   GitHub OAuth (MVP);
-   GitLab OAuth (Phase 2).

Модель identity:

``` text
User
 │
 ├── Identity → GitHub
 │
 └── Identity → GitLab (Phase 2)
```

#### Organization Module

Organization является отдельной бизнес-сущностью (тенантом платформы).

Components:

-   Organization Service;
-   Membership Service;
-   Role / Authorization Service;
-   Organization Repository;
-   Membership Repository.

Модель:

``` text
User
 │
 ▼
Membership
 │
 ▼
Organization
 ├── Repositories
 ├── Subscription
 ├── Settings
 └── Usage
```

`Platform Admin` (Phase 2) имеет platform-wide permissions.

`Owner` управляет своей Organization: участники, подписка, billing,
настройки.

`Member` имеет доступ к repositories, ревью и параметрам ревьюера, но
не управляет участниками и подпиской.

#### Billing Module

Components:

-   Billing Service;
-   Stripe Client;
-   Subscription Service;
-   Billing Webhook Handler;
-   Subscription Repository.

Stripe используется для:

-   checkout;
-   payment;
-   subscription lifecycle.

Локальное состояние subscription хранится в PostgreSQL.

``` text
Stripe
   │
   │ webhook
   ▼
Billing Webhook Handler
   │
   ▼
Subscription Service
   │
   ▼
PostgreSQL
```

Subscription принадлежит Organization.

Основные данные:

-   plan;
-   status;
-   limits;
-   current period;
-   Stripe references.

#### Integration Module

GitHub и GitLab имеют две независимые роли:

1.  OAuth identity provider;
2.  code-hosting integration.

Integration Module отвечает за вторую роль.

Components:

-   Integration Service;
-   GitHub Adapter;
-   GitLab Adapter (Phase 2);
-   Repository Service;
-   Pull Request Service (Merge Request Service --- Phase 2);
-   Webhook Handler;
-   Credential Service;
-   Integration Repository.

Абстракция:

``` text
Integration Service
        │
   ┌────┴────┐
   ▼         ▼
GitHub     GitLab
Adapter    Adapter
   │         │
   ▼         ▼
GitHub API GitLab API
```

Подключение repositories выполняется через GitHub PAT
(user-configured access token); GitHub App installation flow ---
Phase 2.

Credentials хранятся отдельно от основной User model и в необходимом
scope.

#### Review Module

Backend отвечает за создание и orchestration review job.

Components:

-   Review Controller;
-   Review Service;
-   Review Authorization;
-   Review Repository;
-   Review Job Publisher.

Flow:

``` text
POST /reviews
      │
      ▼
Review Service
      │
      ├── validate User
      ├── validate Membership
      ├── validate Repository
      └── validate Subscription
               │
               ▼
          Create Review
               │
               ▼
         Publish Review Job
```

Backend не выполняет длительный AI analysis в request lifecycle.

### 4.3 Job Queue

Queue используется для asynchronous review execution.

``` text
Review API
    │
    ▼
 Job Queue
    │
    ▼
Review Worker
```

Queue реализована поверх PostgreSQL (procrastinate): enqueue job'а и
запись ревью выполняются в одной транзакции --- нет потерянных и
фантомных job'ов при сбое между записью и enqueue. Транспорт очереди
скрыт за тонким слоем enqueue/chain; при необходимости замена не
затрагивает domain logic.

Job содержит идентификатор review и необходимый execution context для
worker.

### 4.4 Review Worker

Review Worker выполняет основной review pipeline.

``` text
Review Worker
│
├── Review Job Handler
├── Review Context Builder
├── Preflight Checker
├── Prompt Builder
├── LLM Gateway
├── Review Analyzer
├── Result Validator
├── Review Publisher
└── Review Persistence
```

## 5. Review Pipeline

``` text
GitHub / GitLab
      │
      │ webhook / on-demand request
      ▼
API / Integration Module
      │
      ▼
Review API
      │
      ▼
Job Queue
      │
      ▼
Review Job Handler
      │
      ▼
Context Builder
      │
      ▼
Preflight
      │
      ▼
Prompt Builder
      │
      ▼
LLM Gateway
      │
      ▼
Review Analyzer
      │
      ▼
Result Validator
      │
      ▼
Review Publisher
      │
      ▼
Billing Event (usage)
      │
      ▼
GitHub / GitLab
```

### 5.1 Context Builder

Context Builder формирует контекст, необходимый для качественного
анализа.

Источники контекста:

-   unified diff;
-   локальный surrounding code;
-   dependency/interface context;
-   PR/MR metadata;
-   project metadata;
-   доступные repository files.

Pipeline:

``` text
PR / MR
   │
   ├── Diff
   ├── Metadata
   └── Files
         │
         ▼
    Diff Parser
         │
         ▼
     Filtering
         │
         ▼
 Context Selection
         │
         ▼
 Chunking / Budgeting
         │
         ▼
   Review Context
```

При подготовке контекста учитываются binary/generated/ignored files и
token/context budget.

### 5.2 Preflight

Preflight выполняется после сборки контекста и до вызова LLM:

-   оценка token budget --- размер контекста против лимитов выбранной
    модели;
-   проверка квоты подписки --- лимит ревью за текущий billing period;
-   supersede-проверка --- прогон не был помечен SUPERSEDED новым
    push'ем в тот же PR.

При превышении бюджета контекст усекается (truncation/downgrade); при
исчерпании квоты прогон завершается отказом с понятной ошибкой, billing
event не создаётся.

### 5.3 Prompt Builder

Prompt Builder объединяет:

-   system instructions;
-   review context;
-   review criteria;
-   expected output schema.

``` text
System Instructions
        +
Review Context
        +
Review Criteria
        +
Output Schema
        │
        ▼
   LLM Request
```

### 5.4 LLM Gateway

LLM Gateway предоставляет provider-independent interface.

``` text
Review Analyzer
      │
      ▼
LLM Gateway
      │
  ┌───┴──────────┬─────────┐
  ▼              ▼         ▼
OpenAI      Anthropic   Gemini
Adapter      Adapter   Adapter
```

MVP подключает OpenAI и Anthropic; Gemini и Custom adapters ---
Phase 2.

Domain/application logic не зависит от конкретного LLM provider.

### 5.5 Review Analyzer

Анализ выполняется по основным категориям:

-   correctness;
-   security;
-   edge cases;
-   performance;
-   readability;
-   best practices.

Результат --- структурированный набор findings и review summary.

### 5.6 Result Validator

До публикации результаты проходят postprocessing:

-   output schema validation;
-   line mapping validation;
-   deduplication;
-   noise filtering;
-   applicability checks.

``` text
LLM Result
    │
    ▼
Schema Validation
    │
    ▼
Line Mapping Validation
    │
    ▼
Deduplication
    │
    ▼
Noise Filtering
    │
    ▼
Publishable Findings
```

### 5.7 Review Publisher

Publisher преобразует validated findings в формат code-hosting provider.

Результаты:

-   high-level summary;
-   inline comments/discussions;
-   suggestions;
-   review status.

``` text
Validated Findings
        │
        ▼
Review Publisher
   ┌────┴────┐
   ▼         ▼
GitHub     GitLab
PR Review  MR Discussions
```

Публикация идемпотентна: уникальный ключ прогона в external id
комментария + upsert статуса --- повторная доставка job'а не создаёт
дубликаты комментариев.

## 6. Review Lifecycle

``` text
CREATED
   │
   ▼
QUEUED
   │
   ▼
RUNNING
   │
   ▼
VALIDATING
   │
   ▼
PUBLISHING
   │
   ▼
COMPLETED
```

Failure path:

``` text
RUNNING / VALIDATING / PUBLISHING
              │
              ▼
            error
              │
       ┌──────┴──────┐
       ▼             ▼
   Retryable     Non-retryable
       │             │
       ▼             ▼
 retry стадии     FAILED
 (до N попыток)  (terminal)
       │
       │ попытки исчерпаны
       ▼
    FAILED
   (terminal)
```

Supersede path --- при новом push в тот же PR/MR активные прогоны
помечаются SUPERSEDED в той же транзакции, что и enqueue нового
ревью:

``` text
QUEUED / RUNNING / VALIDATING / PUBLISHING
              │
              ▼
     SUPERSEDED (terminal)
```

Стадия пайплайна выполняется как отдельный job; стадии идемпотентны,
ретраи выполняются на уровне стадии (retry-политика очереди), а не
всего прогона.

Review сохраняет execution metadata, status (включая SUPERSEDED),
findings и errors.

## 7. Webhook Architecture

Webhook endpoints находятся в API Layer.

``` text
GitHub ───────┐
              │
GitLab ───────┼──► Webhook Handler
                         │
Stripe ───────┘          ▼
              Signature Validation
                         │
                         ▼
                 Delivery Dedup
                         │
                         ▼
                   Event Parser
                         │
                         ▼
                  Event Router
                 ┌─────┼─────┐
                 ▼     ▼     ▼
           Integration Billing Review
```

Webhook flow:

1.  receive event;
2.  validate signature;
3.  dedup по delivery ID --- повторные доставки безопасны
    (idempotent handling);
4.  parse provider event;
5.  route event;
6.  execute соответствующий application service;
7.  при необходимости создать asynchronous job.

## 8. Data Model

Основные сущности:

``` text
User
 │
 ├── Identity → GitHub (GitLab --- Phase 2)
 │
 └── Membership
        │
        ▼
   Organization
        │
   ┌────┼───────────────┐
   ▼    ▼               ▼
Repository Subscription Usage
   │
   ▼
Pull Request / Merge Request
   │
   ▼
Review
   │
   ▼
Findings
```

Ключевые сущности:

### User

Хранит:

-   identity;
-   email;
-   authentication-related data;
-   platform role information.

### Identity

Связывает User с OAuth provider:

``` text
Identity
├── provider
├── provider_user_id
└── User
```

### Membership

Связывает User с Organization:

``` text
Membership
├── User
├── Organization
└── role (owner / member)
```

### Organization

Тенант платформы. Хранит:

-   memberships;
-   repositories;
-   subscription;
-   settings;
-   usage;
-   integration relationships.

### Subscription

Хранит:

-   Organization;
-   plan;
-   status;
-   limits;
-   billing period;
-   Stripe identifiers.

### Integration

Представляет подключение GitHub (GitLab --- Phase 2) к Organization:

``` text
Integration
├── provider
├── credentials (PAT, encrypted at rest)
└── Organization
```

### Repository

Представляет подключённый repository и его provider-specific
identifiers/configuration.

### Pull Request / Merge Request

Хранит:

-   repository;
-   provider-specific PR/MR identifiers;
-   author, head SHA, metadata.

### Review

Хранит:

-   repository;
-   PR/MR reference;
-   status;
-   execution metadata;
-   summary;
-   timestamps;
-   errors.

### Finding

Хранит:

-   review;
-   file;
-   line/range;
-   category;
-   severity;
-   message;
-   suggestion;
-   validation metadata.

### Usage

Хранит:

-   Organization;
-   счётчик ревью за текущий billing period;
-   billing events (основа для будущего metered overage billing).

## 9. Authorization Model

Authorization выполняется на Backend.

Основные уровни:

``` text
Platform
   │
   ▼
Platform Admin (Phase 2) --- platform-wide access
   │
   ▼
Organization
   │
   ├── Owner  --- организация, участники, подписка, billing, настройки
   │
   └── Member --- repositories, ревью, параметры ревьюера
                  (без управления участниками и подпиской)
```

Для review request проверяются:

``` text
Authenticated User
        │
        ▼
Organization Membership
        │
        ▼
Required Role
        │
        ▼
Repository Access
        │
        ▼
Subscription Policy
        │
        ▼
Review Allowed
```

Frontend может скрывать недоступные действия для UX, но security
decision принимается Backend.

## 10. Security and Credentials

Основные secrets:

-   GitHub OAuth credentials;
-   GitLab OAuth credentials;
-   repository access tokens;
-   Stripe credentials;
-   LLM provider credentials;
-   application secrets.

Secrets хранятся в базе данных в зашифрованном виде на этапе MVP:
application-level шифрование (AES-GCM, библиотека cryptography); ключ
шифрования хранится вне базы (environment / secrets manager).

Provider credentials не смешиваются с User domain data.

Webhook requests проходят signature validation до обработки события.

## 11. Persistence

PostgreSQL является primary database.

Логические области данных:

``` text
PostgreSQL
│
├── Users / Identities
├── Organizations / Memberships
├── Subscriptions
├── Integrations
├── Repositories
├── Reviews
├── Findings
└── Usage / Execution Metadata
```

Отдельный object storage на этапе MVP не используется: артефакты
прогона (context snapshot, raw LLM response) хранятся в PostgreSQL.
Вынос в S3-совместимый storage --- post-MVP.

## 12. External Integrations

### GitHub

Используется для:

-   OAuth;
-   repository access;
-   Pull Request data;
-   diff/files;
-   comments/reviews;
-   webhooks.

### GitLab (Phase 2)

Adapter-паттерн закладывается в дизайн. Используется для:

-   OAuth;
-   repository access;
-   Merge Request data;
-   diff/files;
-   discussions/comments;
-   webhooks.

### Stripe

Используется для:

-   checkout;
-   payments;
-   subscriptions;
-   subscription lifecycle webhooks.

### LLM Providers

Подключаются через LLM Gateway:

``` text
Review Worker
      │
      ▼
LLM Gateway Interface
      │
  ┌───┼────────┬─────────┐
  ▼   ▼        ▼         ▼
OpenAI Anthropic Gemini Custom
```

MVP: OpenAI, Anthropic (platform-wide ключи). Gemini, Custom adapters
и BYOK --- Phase 2.

## 13. End-to-End Review Flow

``` text
1. User
   │
   ▼
2. Web Frontend
   │
   ▼
3. API / Review Service
   │
   ├── authentication
   ├── membership
   ├── repository
   └── subscription policy
   │
   ▼
4. Review created
   │
   ▼
5. Job Queue
   │
   ▼
6. Review Worker
   │
   ├── fetch PR/MR
   ├── build context
   ├── preflight (token budget, quota, supersede)
   ├── build prompt
   ├── call LLM
   ├── analyze
   ├── validate
   ├── publish
   └── record billing event
   │
   ▼
7. GitHub / GitLab
   │
   ├── summary
   ├── inline findings
   └── review status
   │
   ▼
8. PostgreSQL
   │
   └── persisted review result
   │
   ▼
9. Web Frontend
      │
      └── displays review status and findings
```

## 14. Architecture Principles

### Provider abstraction

GitHub/GitLab и LLM providers подключаются через adapters/interfaces,
чтобы application/domain logic не зависела от конкретного внешнего API.

### Async review execution

Review выполняется через Queue поверх PostgreSQL (procrastinate) +
Worker, чтобы длительная AI processing не была частью обычного HTTP
request lifecycle. Enqueue job'а и запись ревью выполняются в одной
транзакции; стадии пайплайна идемпотентны, ретраи --- на уровне
стадии. Транспорт очереди скрыт за тонким слоем и может быть заменён
без изменения domain logic.

### Organization-scoped tenancy

Organization является основной бизнес-границей для:

-   repositories;
-   subscription;
-   usage;
-   settings;
-   memberships.

### Backend as security boundary

Authentication, authorization, subscription enforcement и credential
access контролируются Backend.

### Review quality pipeline

Review quality строится через последовательность:

``` text
Context
  ↓
Preflight
  ↓
Prompt
  ↓
LLM
  ↓
Analysis
  ↓
Validation
  ↓
Publishing
```

### Process separation

API и Review Worker --- отдельные процессы с независимым
масштабированием, использующие один PostgreSQL (данные + очередь).
Application data и asynchronous jobs разделяются логически, а не
отдельной инфраструктурой, на этапе MVP.

## 15. Observability

Базовые возможности на этапе MVP:

-   structured logging с correlation по review id сквозь все стадии
    пайплайна;
-   метрики стадий: длительность, количество ретраев, токены и
    стоимость LLM-вызовов;
-   статусы, ошибки и execution metadata ревью доступны через API и
    web-интерфейс;
-   логи и метрики процессов на хосте VDS; выделенная
    observability-инфраструктура --- post-MVP.

## 16. Deployment View

Базовая deployment-модель MVP --- single-host VDS: PostgreSQL, API
Backend и Review Worker работают на одном хосте как отдельные
процессы; Web Frontend раздаётся как static files.

``` text
                        VDS
 ┌─────────────────────────────────────────────┐
 │ ┌──────────────┐     ┌──────────────┐       │
 │ │ Web Frontend │     │ API Backend  │       │
 │ └──────────────┘     └──────┬───────┘       │
 │                             │               │
 │                      ┌──────▼───────┐       │
 │                      │  PostgreSQL  │       │
 │                      │(data + queue)│       │
 │                      └──────▲───────┘       │
 │                       ┌─────┴───────┐       │
 │                       │Review Worker│       │
 │                       └─────┬───────┘       │
 └─────────────────────────────┼───────────────┘
                               │
                     ┌─────────┴──────────┐
                     ▼                    ▼
               GitHub/GitLab         LLM Provider
```

Масштабирование на несколько хостов и выбор production orchestration
technology --- этап post-MVP.

## 17. Architectural Summary

Итоговая модель:

``` text
                          USER
                            │
                            ▼
                   ┌─────────────────┐
                   │  Web Frontend   │
                   │ React/TypeScript│
                   └────────┬────────┘
                            │
                            ▼
                   ┌─────────────────┐
                   │  API / Backend  │
                   │ Modular Monolith│
                   │                 │
                   │ Auth            │
                   │ Organization    │
                   │ Billing         │
                   │ Integration     │
                   │ Review          │
                   └───────┬─────────┘
                           │
                           ▼
                     ┌───────────┐
                     │   Queue   │
                     └─────┬─────┘
                           │
                           ▼
                   ┌─────────────────┐
                   │ Review Worker   │
                   │                 │
                   │ Preflight       │
                   │ Context Builder │
                   │ Prompt Builder  │
                   │ LLM Gateway     │
                   │ Analyzer        │
                   │ Validator       │
                   │ Publisher       │
                   └───────┬─────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
           GitHub       GitLab       LLM APIs
```

Это является базовой system design specification для MVP AI Code Review
Platform.
