# Security
- Database queries are built with bound parameters or a query builder; flag any query assembled by string formatting or concatenation.
- Flag operating-system commands run through a shell, or assembled from request data, instead of an argument list with a fixed program name.
- Flag user input that reaches `eval`, a template compiled at run time, or a deserializer that can construct arbitrary objects (pickle, Java serialization, YAML without a safe loader).
- User-controlled data rendered into HTML is escaped by the template engine; flag raw-HTML helpers and string-built markup.
- A URL supplied by a user is checked against an allowlist of schemes and hosts before the server requests it.
- File paths built from user input are normalized and checked to stay inside the intended directory.
- A redirect target taken from a request parameter is a relative path or a host from an allowlist.
- Every new endpoint requires authentication unless the same change states why it is public.
- An endpoint that returns or changes an object by id checks that the caller owns it or has a role that allows it.
- Roles and permissions are checked on the server; flag a check that exists only in client code.
- Flag passwords, API keys, tokens, private keys and connection strings written as literals in code, configuration or tests.
- Passwords are hashed with bcrypt, scrypt or Argon2; flag MD5, SHA-1 and unsalted SHA-256 for passwords.
- Flag MD5, SHA-1, DES, RC4 and ECB mode used for anything that protects data.
- Tokens, session ids and password-reset codes come from a cryptographically secure random generator.
- Flag disabled TLS certificate or hostname verification.
- Tokens and secrets are compared with a constant-time function.
- Session cookies are set with `Secure`, `HttpOnly` and `SameSite`.
- Flag a CORS policy that combines a wildcard or reflected origin with credentials.
- State-changing endpoints that use cookie authentication verify a CSRF token or the request origin.
- Flag log statements that write passwords, tokens, full card numbers, authorization headers or whole request bodies.
- Error responses sent to a client carry no stack trace, SQL text or internal host name.
- File uploads are limited in size and checked by content type and extension against an allowlist; stored names are generated, not taken from the client.
- Endpoints for login, password reset and one-time codes have rate limiting or lockout.
- Flag a JWT that is decoded without verifying the signature, or verified with an algorithm taken from the token header.
- Flag a new dependency fetched from a URL, a git branch or an unpinned version range.
- Flag debug modes, default credentials and permissive settings (`DEBUG = True`, `0.0.0.0/0`, public buckets) in configuration that ships to production.

# Correctness
- A failed authorization or validation check stops the request; flag code that logs the failure and continues.
- Flag a caught exception around a security check that lets the request proceed as if the check had passed.

# Ignore
- path: *.lock
- path: package-lock.json
- path: go.sum
- path: vendor/**
- path: node_modules/**
