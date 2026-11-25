# Security
- SQL is built only with bound parameters or the SQLAlchemy query builder; flag f-strings, `%` or `.format()` inside `text()`, `execute()` or a raw cursor call.
- Every route that reads or writes a row owned by a user or tenant filters by the authenticated principal's id; flag a lookup by the path id alone.
- Every route except health and docs declares an auth dependency (`Depends(...)`) on the route or on its router; flag a new route without one.
- Request bodies are Pydantic models; flag a handler that takes `dict`, `Any` or `Request.json()` for a body it then stores or passes on.
- Response models never include password hashes, tokens or API keys; flag returning an ORM object from a route with no `response_model`.
- Secrets come from settings or the environment; flag a key, password or token literal in code, tests excluded.
- Flag `subprocess` calls with `shell=True` or a command assembled from request data.
- Flag `pickle.loads`, `yaml.load` without `SafeLoader`, and `eval`/`exec` on data that came from a request, a file upload or the network.
- A URL taken from user input is validated against an allowlist of hosts before the server fetches it.
- File paths built from user input are resolved and checked to stay inside the intended directory.

# Correctness
- No mutable default arguments (`[]`, `{}`, `set()`); use `None` and create the value inside the function.
- Flag `except Exception` or a bare `except` that neither re-raises nor logs the exception.
- An exception raised inside an `except` block uses `raise ... from exc`.
- Datetimes are timezone-aware; flag `datetime.utcnow()` and `datetime.now()` without a `tz` argument.
- Money is `Decimal` or an integer in minor units; flag `float` arithmetic on amounts.
- A database write spanning several statements runs in one transaction; flag a commit between statements that must succeed or fail together.
- A schema change in `models/` comes with an Alembic migration in the same pull request.

# Performance
- Flag a database query or an HTTP call inside a loop over rows or request items; fetch in one query or batch the calls.
- List endpoints take a limit with an upper bound; flag a query that returns a whole table.
- Flag `await` inside a `for` loop over independent calls where `asyncio.gather` or a task group fits.

# Concurrency
- No blocking calls inside `async def`: flag `requests`, `time.sleep`, synchronous database drivers and file reads of unbounded size.
- Flag module-level mutable state that request handlers write to without a lock.
- A background task opens its own database session; flag passing the request's session into it.

# Style
- Public functions and methods in `domain/` and `api/` have type annotations on every parameter and on the return value.
- Log calls pass values as arguments or `extra`, not through an f-string.
- Route handlers stay thin: flag business logic longer than about 30 lines inside a handler instead of a service function.

# Ignore
- path: uv.lock
- path: poetry.lock
- path: alembic/versions/**
- path: **/__snapshots__/**
- path: *_pb2.py
