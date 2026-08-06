# Operate model rate limits and retries

Use this guide to shape model traffic before quota exhaustion and recover from recognized rate-limit failures. It separates provider quotas, agent concurrency, retry timing, observable failure messages, and practical tuning.

## Check prerequisites

Configure a working model provider first, then obtain that deployment's current TPM and RPM quotas. Treat the profiles below as starting points and validate them under representative load; shared provider usage and provider-side burst rules can reduce capacity available to this process.

## Shape traffic proactively

Every model created by `research_agent/model_factory.py` is wrapped by the reliability layer. For asynchronous `ainvoke` calls, proactive shaping runs only when both `MODEL_TPM` and `MODEL_RPM` are positive.

```dotenv
MODEL_TPM=120000
MODEL_RPM=500
```

The limiter:

- keeps estimated input plus output tokens in a rolling 60-second window;
- spaces requests using the configured RPM;
- operates at 80% of both configured limits as a safety margin;
- estimates with the `cl100k_base` tokenizer when available, otherwise roughly one token per four characters;
- assumes 1,000 output tokens when a call does not provide `max_tokens`.

Proactive shaping currently wraps `ainvoke`, not synchronous `invoke`. Setting either limit to zero or a negative value disables proactive shaping entirely; it does not create a one-dimensional TPM-only or RPM-only limiter.

### Size every request below safe TPM capacity

Current limitation: admission has no oversized-request guard. If estimated prompt plus output tokens exceed `int(0.8 * MODEL_TPM)`, the request can never satisfy the safe-capacity check and can remain in the admission loop indefinitely rather than failing fast.

Configure `MODEL_TPM` so each estimated request is at most 80% of the configured TPM, or reduce prompt size and requested output tokens. Lower concurrency when otherwise admissible requests compete for the rolling window; concurrency reduction cannot make one individually oversized request admissible.

### Separate quotas from concurrency

`MODEL_TPM` and `MODEL_RPM` describe model-provider capacity. They do not limit the number of research units; `MAX_CONCURRENT_RESEARCH_UNITS` controls delegated research concurrency and defaults to `3`.

If quota failures persist, reduce concurrency before raising retries. More retries add traffic after a quota error and cannot increase provider capacity.

## Recover with reactive retries

Both `invoke` and `ainvoke` are wrapped with exponential-backoff retry logic. The wrapper retries exceptions whose text contains recognized indicators such as `429`, `rate limit`, `too many requests`, `throttl`, `quota exceeded`, `tokens per minute`, or `requests per minute`.

| Variable | Default | Effect |
| --- | ---: | --- |
| `MODEL_MAX_RETRIES` | `5` | Retries after the initial call; default permits six total attempts. |
| `MODEL_INITIAL_BACKOFF` | `1.0` | First nominal delay in seconds. |
| `MODEL_MAX_BACKOFF` | `60.0` | Nominal delay cap. |
| `MODEL_BACKOFF_MULTIPLIER` | `2.0` | Exponential multiplier. |
| `MODEL_RETRY_JITTER` | `true` | Randomize actual wait to 50-100% of nominal delay. |

With defaults, nominal waits are 1, 2, 4, 8, and 16 seconds; jitter can shorten each wait. The implementation does not read provider `Retry-After` headers.

Non-matching exceptions are raised immediately. Azure content-filter errors are explicitly excluded because repeating the same request is not expected to make it acceptable; other provider errors are only retried when their message matches a configured indicator.

Configuration is read when `research_agent/retry_utils.py` is imported. Restart the CLI or server after changing environment values.

## Choose a tuning profile

Set TPM and RPM from the provider deployment, then adjust retry timing for the operating environment.

### Strict or low-tier quota

```dotenv
MODEL_MAX_RETRIES=10
MODEL_INITIAL_BACKOFF=2.0
MODEL_MAX_BACKOFF=120.0
MODEL_BACKOFF_MULTIPLIER=2.0
MODEL_RETRY_JITTER=true
```

Pair this with lower `MAX_CONCURRENT_RESEARCH_UNITS`. Long retry windows improve the chance of crossing a provider reset boundary but increase worst-case task latency.

### Higher paid quota

```dotenv
MODEL_MAX_RETRIES=3
MODEL_INITIAL_BACKOFF=0.5
MODEL_MAX_BACKOFF=30.0
MODEL_BACKOFF_MULTIPLIER=1.5
MODEL_RETRY_JITTER=true
```

Use only after observing that proactive shaping stays below the provider's real quotas.

### Local Ollama

```dotenv
MODEL_MAX_RETRIES=2
MODEL_INITIAL_BACKOFF=0.5
MODEL_MAX_BACKOFF=10.0
MODEL_BACKOFF_MULTIPLIER=1.5
MODEL_RETRY_JITTER=true
```

Local overload may surface as timeouts or connection errors rather than recognized rate-limit text; those errors are not automatically retried by this wrapper.

### Disable reactive retries

Set `MODEL_MAX_RETRIES=0`. The initial model call still occurs and any error is returned immediately.

## Interpret failure messages

A retryable failure logs the function, current attempt count, randomized wait, and original error:

```text
WARNING:retry_utils:Rate limit hit in invoke (attempt 1/6). Retrying in 0.73s... Error: 429 Too Many Requests
```

Exhaustion logs the configured retry count and re-raises the last exception:

```text
ERROR:retry_utils:Rate limit error persisted after 5 retries in invoke. Last error: 429 Too Many Requests
```

A non-retryable exception logs `Non-retryable error in <function>` and is raised without waiting. Exact logger prefixes and provider messages may vary with logging configuration and SDK versions.

## Troubleshoot reliability

### Rate limits continue after retries

1. Confirm TPM and RPM against the exact model deployment or region.
2. Lower `MAX_CONCURRENT_RESEARCH_UNITS` and check for other clients sharing quota.
3. Inspect whether failures are token, request, daily-usage, or account limits; only rolling capacity may recover quickly.
4. Reduce prompt size or expected output before extending retry duration.
5. Increase `MODEL_INITIAL_BACKOFF` or `MODEL_MAX_RETRIES` only when waiting can plausibly restore capacity.

### Requests are slower than expected

Check the 80% safety margin and RPM-derived spacing before blaming the provider. Overly conservative configured quotas, high token estimates, repeated retries, verification rounds, and research concurrency can all add latency.

If the service is healthy and paid quota permits it, raise `MODEL_TPM`/`MODEL_RPM` to the actual limits or reduce retry count and multiplier. Do not disable jitter across many synchronized replicas unless you accept burst risk.

If one request never leaves admission, compare its estimated prompt plus `max_tokens` (1,000 when omitted) with `int(0.8 * MODEL_TPM)`. Raise TPM only to a quota the provider actually grants; otherwise shrink input/output before retrying.

### Synchronous calls still burst

This is expected: proactive shaping currently affects asynchronous model calls only. Use asynchronous execution for shaped traffic or place a provider-aware gateway/limiter in front of synchronous calls.

### A transient error is not retried

Compare the exception text with the recognized rate-limit indicators. The wrapper intentionally does not retry arbitrary network, authentication, content-filter, or malformed-request failures; fix credentials or request input instead of broadening retry duration.

### Verify the implementation

Run the focused suite:

```bash
uv run pytest tests/test_retry_utils.py -v
```

These tests cover indicator detection, sync and async retries, backoff bounds, jitter, exhaustion, content-filter exclusion, and environment-derived configuration.

## Related documentation

- [Configuration](configuration.md)
- [Evaluation](evaluation.md)
- [Installation](../getting-started/installation.md)
- [Local development](../getting-started/local-development.md)
- [Handbook index](../README.md)
