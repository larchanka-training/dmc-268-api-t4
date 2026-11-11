# Security
- SQL uses placeholders (`$1`, `?`) with arguments; flag `fmt.Sprintf` or string concatenation that builds a query.
- Flag `exec.Command` whose program name or arguments come from request data without an allowlist.
- Flag `InsecureSkipVerify: true` outside test files.
- Random values used for tokens, ids or secrets come from `crypto/rand`, never `math/rand`.
- File paths built from user input are cleaned with `filepath.Clean` and checked to stay under the base directory.
- HTML is rendered with `html/template`; flag `text/template` or `template.HTML(...)` on user data.
- Every `http.Server` and `http.Client` sets timeouts; flag `http.Get`, `http.Post` and `http.ListenAndServe` used with the defaults.
- A request body is wrapped in `http.MaxBytesReader` before it is decoded.

# Correctness
- Every returned `error` is checked; flag `_ =` on an error and a call whose error result is dropped.
- Errors are wrapped with `%w` when context is added; flag `%v` or `%s` on an error that callers compare with `errors.Is` or `errors.As`.
- Errors are compared with `errors.Is` or `errors.As`, never with `==` or by matching the message text.
- `defer` is not used inside a loop for per-iteration cleanup; close the resource in the loop body or in a helper function.
- `resp.Body`, `rows` and opened files are closed on every path, including the error paths.
- `rows.Err()` is checked after a `rows.Next()` loop.
- Flag a nil map that is written to, and a slice indexed without a length check on data from outside the function.
- A function that takes a `context.Context` passes it to every call that accepts one; flag `context.Background()` inside a request path.

# Performance
- Flag a database query or an HTTP call inside a loop over rows or request items.
- Flag string concatenation with `+=` inside a loop; use `strings.Builder`.
- A slice or map whose final size is known is created with that capacity.
- Flag `io.ReadAll` on a request body or a file of unbounded size.

# Concurrency
- Every goroutine has a way to stop: a context, a closed channel or a `WaitGroup` the caller waits on; flag a goroutine started in a request handler with none.
- Flag a map read and written from several goroutines without a mutex or `sync.Map`.
- A `sync.Mutex` is unlocked with `defer` right after `Lock`, and is never copied by value.
- Flag a send on a channel that nothing is guaranteed to receive from, and an unbuffered channel used as a result slot for a goroutine that can be abandoned.
- `wg.Add` is called before the goroutine starts, not inside it.

# Style
- Exported functions, types and methods have a doc comment that starts with their name.
- `context.Context` is the first parameter and is named `ctx`.
- Flag `panic` outside `main`, `init` and test code.
- Flag a function with more than five parameters or more than three return values.

# Ignore
- path: go.sum
- path: vendor/**
- path: *.pb.go
- path: *_gen.go
- path: mocks/**
