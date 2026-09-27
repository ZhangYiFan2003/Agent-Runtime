# Shared Provider Gateway

Axiom can route LLM calls from distributed Workers through one internal `providerd` process. The
gateway is opt-in; local CLI and developer workflows can continue to use the direct
`OpenAICompatibleClient` path.

```text
Workers
  |
  v
GatewayLlmClient
  |  NDJSON stream
  v
providerd
  |
  v
ordered Provider targets
```

`providerd` is the shared governance point for the single-host deployment. Its concurrency,
pending admission, RPM tokens, circuit state, and rate-limit cooldowns are visible to every Worker.
The state is process-local and intentionally resets when the single providerd replica restarts.

## Retry ownership

Runtime retry and gateway routing are different authorities:

```text
Runtime RetryPolicy = whether another model attempt is allowed
Provider Gateway    = where the current model attempt goes
```

For each gateway request, providerd examines the route's targets in order. It can skip a target
before an upstream request when its circuit is open, its rate cooldown is active, its RPM admission
is unavailable, or its bounded concurrency/pending admission is full. The first eligible target is
selected deterministically.

After providerd starts a real upstream request, it never retries or switches targets. A 429, 5xx,
timeout, connection failure, authentication failure, validation failure, or partial-stream failure
is returned to the Worker as one structured failure. The durable Runtime records that attempt and
decides whether another attempt is permitted. A later Runtime attempt can select a different target
because the shared target state has changed. Partial generations are never combined.

## Configuration

Gateway clients use the existing `llm` configuration:

```json
{
  "llm": {
    "provider": "gateway",
    "route": "default",
    "gateway_url": "http://providerd:8070"
  }
}
```

Provider routes use the existing `AxiomConfig.provider_gateway` tree. Each route is an ordered list
of targets. A target defines an ID, provider, model, base URL, context window, credential environment
variable name, concurrency and pending limits, optional RPM, and circuit settings. The credential
field is an environment variable name such as `AXIOM_API_KEY`; it is never the credential value.

`AXIOM_PROVIDER_ROUTES_JSON` can override the structured routes for deployment without introducing a
second configuration format. It contains the same JSON object shape as
`provider_gateway.routes`. Do not put credential values in it.

Direct mode remains backward compatible:

```json
{
  "llm": {
    "provider": "deepseek",
    "model": "deepseek-v4-flash"
  }
}
```

## Governance

Each configured target has:

- shared `max_concurrency`;
- bounded `max_pending` and `admission_timeout_seconds`;
- optional token-bucket `requests_per_minute`;
- `CLOSED`, `OPEN`, and `HALF_OPEN` circuit states;
- transient failure threshold, open interval, and bounded half-open probes;
- a separate 429 cooldown that respects numeric `Retry-After`.

Timeout, connection, and selected transient server failures affect the circuit. Authentication,
authorization, validation, unsupported request, and context-size failures do not. A 429 affects
short-term admission but does not assert that the provider is down.

Shared TPM admission is deferred. Provider-reported input, output, cached-input, and reasoning usage
continues through the normalized stream and remains authoritative for Runtime accounting.

## Cost and RunBudget

`RunBudget` remains the only per-Run call/token/cost authority. Gateway mode does not create a second
cost budget.

Before a call, the Runtime reserves input cost conservatively using the maximum Worker-side pricing
entry configured for the route's possible targets. A hard `max_cost_usd` therefore requires that
pricing configuration to cover every possible target. When `provider_selected` and usage arrive,
the reservation is reconciled with the actual provider/model pricing. A selected target without
configured pricing produces `cost_known=false` and `cost_usd=null`, never zero; when a hard cost
limit is active it also fails clearly because the limit cannot be enforced honestly.

Every real upstream attempt still consumes one Runtime model-call unit. providerd performs no hidden
attempts, so retry counts, model-call counts, token usage, cost, and traces remain aligned.

## Protocol and cancellation

Workers send normalized messages, tool schemas, the system prompt, logical route, and bounded
generation settings to `POST /v1/chat`. providerd returns NDJSON events and preserves the existing
normalized stream types, including thinking, text, fragmented tool calls, usage, and finish reason.
The first gateway-specific event identifies the logical route and actual target without exposing a
credential or secret-bearing URL.

Closing or cancelling the Worker stream closes the internal HTTP response. providerd monitors the
connection, closes the upstream stream, and releases target capacity. There is no result cache or
detached generation.

## Security boundary

providerd is trusted internal infrastructure and is the only Runtime service that receives external
provider credentials in gateway deployment mode. Workers receive only the logical route and internal
gateway URL. Web, Runtime API, PostgreSQL, sandboxd, and per-Run Sandbox containers do not join the
`provider-control` network. Sandbox containers retain `network=none` and cannot bypass the Runtime to
call providerd directly.

`GET /health` and `GET /v1/providers` expose route count, target provider/model, circuit state,
active/pending counts, limits, cooldowns, and aggregate governance counters. They do not expose API
keys, credential environment values, authorization headers, or base URLs.

## Observability

Existing LLM spans retain Runtime retry attributes and add the logical route, actual provider/model,
target ID, gateway admission wait, circuit state, and rate-limit evidence. Existing Run metrics
aggregate provider attempts, failures, rate-limited attempts, gateway wait, and actual target pairs.
providerd health provides current shared target state; no second trace store or provider dashboard is
introduced.

## Limits

- One providerd replica; governance state is in memory and is not HA.
- A providerd restart fails active streams and resets circuit, RPM, concurrency, and cooldown state.
- Routing is deterministic ordered availability, not weighted, quality-based, or ML routing.
- Shared TPM admission is deferred; actual provider usage remains recorded.
- Provider configuration and credentials are single-host, trusted-operator infrastructure, not
  tenant-specific policy or billing.
