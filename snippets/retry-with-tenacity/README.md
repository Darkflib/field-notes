---
title: Retrying transient HTTP failures with tenacity
summary: Retry 429s, 5xx gateway errors, and dropped connections with jittered backoff, and fail fast on everything else.
tags: [python, http, resilience]
libraries: [tenacity, httpx]
timeout: 30
network: false
---

## Problem

Remote calls fail transiently, and a bare `for i in range(3)` loop retries the wrong
things: it hammers a server returning 404 and makes a 503 storm worse by retrying in lockstep.

## Run it

```sh
uv run main.py
```

It replays scripted responses through `httpx.MockTransport`, so it runs offline and gives the same result every time.

## Why this approach

- **A predicate, not an exception list.** `retry_if_exception(is_transient)` keeps the
  "is this worth another go?" decision in one testable function. It covers status codes as well as exception types.
- **`Retrying` object over the `@retry` decorator.** The policy becomes a value you can
  build per call site, shrink in tests, and inject, rather than something fixed at import time.
- **Jittered exponential backoff.** `wait_exponential_jitter` spreads out clients that
  failed together, so they don't come back together.
- **Why not `stamina`?** It's a fine opinionated wrapper around tenacity with good defaults and
  instrumentation built in. Reach for it when you want sensible defaults everywhere; use
  tenacity directly when the retry decision itself is the interesting part.

## Gotchas

- Without `reraise=True`, the caller gets `tenacity.RetryError` rather than the
  `httpx.HTTPStatusError` it was written to handle, and its `except` clauses silently stop matching.
- `raise_for_status()` is what turns a 503 into an exception. Leave it out and nothing is
  ever retried, because a 503 is a perfectly successful HTTP exchange.
- The retry wraps the whole block. Any side effect inside `with attempt:` happens once per attempt.

## When not to use it

- **Non-idempotent requests.** A timed-out POST may have committed. Use an idempotency key, or don't retry.
- **Deep call stacks.** If every layer retries three times, four layers make 81 attempts.
  Retry at one layer, usually the outermost.
- **When you need a circuit breaker.** Retries against a dead dependency just add latency.
  Stop calling it for a while instead.
