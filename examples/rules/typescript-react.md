# Security
- Flag `dangerouslySetInnerHTML` whose value is not passed through a sanitizer such as DOMPurify.
- Flag `eval`, `new Function` and `setTimeout`/`setInterval` called with a string.
- A URL from user input or from an API response is checked to start with `http:`, `https:` or `/` before it goes into `href`, `src` or `window.location`.
- Tokens and session ids are not written to `localStorage` or `sessionStorage`; flag any new write of a credential there.
- Flag secrets, API keys and private endpoints hard-coded in client code; only variables with the public prefix (`VITE_`, `NEXT_PUBLIC_`) may reach the bundle.
- `target="_blank"` links to external sites carry `rel="noopener noreferrer"`.
- Flag `postMessage` listeners that do not check `event.origin`.

# Correctness
- `useEffect`, `useMemo` and `useCallback` list every value they read in the dependency array; flag a missing dependency or a disabled `exhaustive-deps` check.
- Hooks are called at the top level of a component or a custom hook, never inside a condition, a loop or after an early return.
- State is never mutated in place: flag `push`, `splice`, `sort` or property assignment on a value held in `useState` or in a store.
- An effect that subscribes, sets a timer or starts a fetch returns a cleanup that unsubscribes, clears the timer or aborts the request.
- List items use a stable id as `key`; flag the array index as `key` on a list that can be reordered, filtered or appended to.
- Flag `as any`, `as unknown as T` and the non-null `!` operator on data that came from an API response or user input.
- Every `fetch` or client call handles a failed request and a non-2xx status; flag a promise with neither `catch` nor `try`/`catch`.
- Flag `==` and `!=` except in a comparison with `null`.
- A `switch` over a union type has a `default` branch that fails on an unhandled member.

# Performance
- Flag a new object, array or inline function passed as a prop to a component wrapped in `React.memo`.
- Flag a state update inside an effect that depends on the same state without a guard.
- Routes and heavy components (charts, editors, maps) are loaded with `lazy` or a dynamic `import()`.
- Flag whole-library imports where a per-module import exists, for example `import _ from "lodash"`.
- A handler bound to `scroll`, `resize` or `input` that triggers a request or a layout read is debounced or throttled.

# Concurrency
- A request started by a component is aborted or its result discarded when the component unmounts or the input changes; flag setting state from a stale response.
- Flag a submit handler that can fire twice before the first request settles.

# Style
- Components are function components; flag a new class component.
- Props are typed with an interface or a type alias; flag `any` and `object` in a props type.
- Flag user-visible strings hard-coded in JSX when the file already uses the translation function.
- Interactive elements are `button` or `a`; flag an `onClick` on a `div` or `span` without `role` and keyboard handling.

# Ignore
- path: package-lock.json
- path: pnpm-lock.yaml
- path: yarn.lock
- path: dist/**
- path: *.snap
- path: *.generated.ts
