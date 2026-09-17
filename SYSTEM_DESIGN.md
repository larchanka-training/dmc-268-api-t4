# SYSTEM_DESIGN

## 1. Purpose

AI Code Review Platform --- SaaS-платформа для автоматизированного code
review изменений в GitHub и GitLab.

Основные пользовательские сценарии:

-   вход через GitHub или GitLab OAuth;
-   подключение GitHub/GitLab repositories;
-   получение события Pull/Merge Request;
-   запуск code review по триггеру;
-   сбор diff и необходимый контекст;
-   отправляет контекст через LLM Provider Interface;
-   анализирует изменения;
-   валидирует результаты;
-   публикует summary и inline comments обратно в GitHub/GitLab;
-   управление Client, участниками, repositories и настройками;
-   управление subscription и оплатой через Stripe.
-   предоставление web-интерфейса для управления участниками, repositories, а также subscription и оплатой через Stripe.

## 2. Architecture Overview

Архитектура строится вокруг трёх основных application containers:

``` text
┌──────────────────────┐
│    Web Frontend      │
│ React + TypeScript   │
└──────────┬───────────┘
           │ HTTPS / REST
           ▼
┌──────────────────────┐
│     API / Backend    │
│    Modular Monolith  │
└──────────┬───────────┘
           │ Review Job
           ▼
┌──────────────────────┐
│      Job Queue       │
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│    Review Worker     │
│ Context → LLM →      │
│ Validation → Publish │
└──────────────────────┘
```

Infrastructure:

-   PostgreSQL --- primary persistent storage;
-   Queue --- review jobs with Redis;
-   AWS --- базовая cloud infrastructure.

Внешние системы:

-   GitHub;
-   GitLab;
-   Stripe;
-   LLM Providers.

## 3. System Context

``` text
                         ┌───────────────────┐
                         │       User        │
                         │ Developer / Admin │
                         └─────────┬─────────┘
                                   │ HTTPS
                                   ▼
                         ┌───────────────────┐
                         │   Client Portal   │
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

                         AWS
                ┌─────────────────────┐
                │ PostgreSQL          │
                │ Queue               │
                └─────────────────────┘
```

## 4. Container Architecture

### 4.1 Web Frontend

**Technology:** React + TypeScript

Responsibilities:

-   application shell;
-   navigation;
-   dashboard;
-   reviews;
-   repositories;
-   integrations;
-   billing;
-   settings;
-   admin UI;
-   client-side state;
-   API communication;
-   authentication state.

Основные frontend components:

``` text
Web Frontend
│
├── Application Shell
│   ├── Header
│   ├── Navigation
│   ├── Global Search
│   └── User / Client Context
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
├── Client Context
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
├── Client Module
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

-   GitHub OAuth;
-   GitLab OAuth.

Модель identity:

``` text
User
 │
 ├── Identity → GitHub
 │
 └── Identity → GitLab
```

#### Client Module

Client является отдельной бизнес-сущностью.

Components:

-   Client Service;
-   Membership Service;
-   Role / Authorization Service;
-   Client Repository;
-   Membership Repository.

Модель:

``` text
User
 │
 ▼
Membership
 │
 ▼
Client
 ├── Repositories
 ├── Subscription
 ├── Settings
 └── Usage
```

`Platform Admin` имеет platform-wide permissions.

`Client Admin` управляет своим Client.

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

Subscription принадлежит Client.

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
-   GitLab Adapter;
-   Repository Service;
-   Pull Request / Merge Request Service;
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

Job содержит идентификатор review и необходимый execution context для
worker.

### 4.4 Review Worker

Review Worker выполняет основной review pipeline.

``` text
Review Worker
│
├── Review Job Handler
├── Review Context Builder
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

### 5.2 Prompt Builder

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

### 5.3 LLM Gateway

LLM Gateway предоставляет provider-independent interface.

``` text
Review Analyzer
      │
      ▼
LLM Gateway
      │
 ┌────┼─────────────┐
 ▼    ▼             ▼
OpenAI Anthropic   Gemini
Adapter  Adapter   Adapter
```

Domain/application logic не зависит от конкретного LLM provider.

### 5.4 Review Analyzer

Анализ выполняется по основным категориям:

-   correctness;
-   security;
-   edge cases;
-   performance;
-   readability;
-   best practices.

Результат --- структурированный набор findings и review summary.

### 5.5 Result Validator

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

### 5.6 Review Publisher

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
            FAILED
              │
        ┌─────┴─────┐
        ▼           ▼
    Retryable   Non-retryable
```

Review сохраняет execution metadata, status, findings и errors.

## 7. Webhook Architecture

Webhook endpoints находятся в API Layer.

``` text
GitHub ───────┐
              │
GitLab ───────┼──► Webhook Handler
              │          │
Stripe ───────┘          ▼
                  Signature Validation
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
3.  parse provider event;
4.  route event;
5.  execute соответствующий application service;
6.  при необходимости создать asynchronous job.

## 8. Data Model

Основные сущности:

``` text
User
 │
 ├── Identity
 │
 └── Membership
          │
          ▼
        Client
          │
     ┌────┼───────────────┐
     ▼    ▼               ▼
Repository Subscription  Usage
     │
     ▼
  Pull Request /
  Merge Request
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

Связывает User и Client и определяет роль пользователя внутри Client.

### Client

Хранит:

-   subscription;
-   repositories;
-   settings;
-   usage;
-   integration relationships.

### Subscription

Хранит:

-   Client;
-   plan;
-   status;
-   limits;
-   billing period;
-   Stripe identifiers.

### Integration

Представляет подключение GitHub или GitLab к Client.

### Repository

Представляет подключённый repository и его provider-specific
identifiers/configuration.

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

## 9. Authorization Model

Authorization выполняется на Backend.

Основные уровни:

``` text
Platform
   │
   ▼
Platform Admin
   │
   ├── platform-wide access
   │
   ▼
Client
   │
   ▼
Client Admin
   │
   └── Client-scoped management
   │
   ▼
Developer
```

Для review request проверяются:

``` text
Authenticated User
        │
        ▼
Client Membership
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

Secrets хранятся в базе данных в зашифрованном виде на этапе MVP.

Provider credentials не смешиваются с User domain data.

Webhook requests проходят signature validation до обработки события.

## 11. Persistence

PostgreSQL является primary database.

Логические области данных:

``` text
PostgreSQL
│
├── Users / Identities
├── Clients / Memberships
├── Subscriptions
├── Integrations
├── Repositories
├── Reviews
├── Findings
└── Usage / Execution Metadata
```

Object Storage тоже храним в PostgreSQL на этапе MVP.

## 12. External Integrations

### GitHub

Используется для:

-   OAuth;
-   repository access;
-   Pull Request data;
-   diff/files;
-   comments/reviews;
-   webhooks.

### GitLab

Используется для:

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
 ┌────┼────────┬─────────┐
 ▼    ▼        ▼         ▼
OpenAI Anthropic Gemini Custom
```

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
   ├── build prompt
   ├── call LLM
   ├── analyze
   ├── validate
   └── publish
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

Review выполняется через Queue (Redis) + Worker, чтобы длительная AI processing
не была частью обычного HTTP request lifecycle.

### Client-scoped tenancy

Client является основной бизнес-границей для:

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

### Infrastructure separation

Application data и asynchronous jobs имеют
отдельные infrastructure responsibilities.

## 15. Deployment View

Базовая deployment-модель:

``` text
                         AWS
                          │
             ┌────────────┴────────────┐
             │                         │
             ▼                         ▼
      ┌──────────────┐         ┌──────────────┐
      │ Web Frontend │         │ API Backend  │
      └──────────────┘         └──────┬───────┘
                                      │
                             ┌────────┼────────┐
                             ▼                 ▼  
                        PostgreSQL           Queue     
                                               │
                                               ▼
                                        Review Worker
                                               │
                                       ┌───────┴────────┐
                                       ▼                ▼
                                  GitHub/GitLab      LLM Provider
```

AWS является инфраструктурной базой; конкретная production orchestration
technology определяется на этапе deployment design.

## 16. Architectural Summary

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
                  │ Client          │
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
