# Langfuse Trace Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `metergraphrelay pull langfuse`, importing Langfuse GENERATION observations into metergraph-native JSONL rows via server-side trace-name/tag/environment/time filtering against Langfuse's v2 Observations API, per the approved design spec at `docs/superpowers/specs/2026-08-07-langfuse-trace-import-design.md`.

**Architecture:** A new `src/metergraphrelay/providers/langfuse.py` module mirrors the existing `providers/openai.py` shape (a `normalize_*` pure function plus a `pull_*` orchestration function), using Python's stdlib `urllib.request` exactly the way `push.py` already does — same mocking seam, same error-handling idiom, no new runtime dependency. `cli.py` gains a real `pull langfuse` dispatch branch (replacing today's stub) with full flag/`--help` coverage. All filtering (`--trace-name`, `--tag`, `--environment`, `--since`/`--until`) is expressed as Langfuse's own `filter`/`fromStartTime`/`toStartTime`/`environment` query parameters — never downloaded broadly and filtered locally.

**Tech Stack:** Python 3.10+, stdlib `urllib.request`/`json`/`base64`/`datetime`, `argparse` (already used by `cli.py`), `pytest` + `unittest.mock` (existing test stack, no new test dependency).

## Global Constraints

- Python stdlib only for HTTP (`urllib.request`) — no new runtime dependency (design spec: Architecture, Alternatives A).
- Langfuse Cloud and self-hosted **v4+ only**; `GET /api/public/v2/observations`, `type=GENERATION` only — no SPAN/EVENT, no scores/evals, no legacy v1 API (design spec: Scope).
- Default `--count` = **100** GENERATION observations overall (never distinct traces) when no selectors are given (design spec: CLI, Targeting & filtering).
- `--trace-name` is repeatable with **OR** semantics; `--tag` is repeatable with **AND** semantics; `--trace-name`/`--tag`/`--environment`/`--since`/`--until` all combine with each other via **AND**; filtering is **server-side only** — never local/content-based filtering (design spec: Targeting & filtering).
- No `--include-content` opt-in gate — content is always transferred; this must be stated as an explicit warning in README and `--help` (design spec: Explicit content-transfer warning).
- **No `--trace-id` selector** in this version (design spec: Non-goals).
- `sdk` = fixed `"metergraphrelay"`, `sdk_version` = this tool's running `__version__`, `source` = fixed `"langfuse"`, `provider` = explicit metadata → conservative model-family inference → `"unknown"` (design spec: Mapping, resolved sdk/source revision).
- `cost_usd` = Langfuse's `totalCost` used as-is, **no recomputation** (design spec: Mapping).
- Atomic output: write to a temp file, `os.replace()` into `--output` only after the entire pull succeeds; a fatal failure anywhere leaves a pre-existing `--output` file completely untouched (design spec: Architecture, Failure semantics).
- Fatal (non-zero exit, clear stderr message, no output-file change): missing/invalid credentials, auth failure, network failure, malformed/unexpected response shape, Langfuse rejecting the constructed filter. Non-fatal (skip + stderr warning + counted): a single malformed GENERATION observation (design spec: Failure semantics).
- `.env`/environment variables are the preferred credential/config path; `--langfuse-public-key`/`--langfuse-secret-key`/`--base-url` CLI flags are an override escape hatch, not the primary interface (design spec: Auth & config).
- Secrets are never logged or persisted; error messages name missing variables, never values (design spec: Auth & config, Security & privacy).
- `config.py`'s `CREDENTIAL_SPECS["langfuse"]` already lists `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` — **do not modify `config.py`**.

---

## Verified Langfuse v2 Observations API Reference

Fetched directly from Langfuse's own API definition source (`raw.githubusercontent.com/langfuse/langfuse/main/fern/apis/server/definition/observations.yml` and `commons.yml`, plus corroborating official changelog/discussion pages), 2026-08-07. This is the authoritative source Task 1 implements against — nothing below is guessed.

**Endpoint:** `GET /api/public/v2/observations`

**Request query parameters** (from `observations.yml`):
`fields` (comma-separated field groups), `name`, `userId`, `sessionId`, `type`, `traceId`, `level`, `parentObservationId`, `isRootObservation`, `environment` (repeatable), `fromStartTime` (datetime), `toStartTime` (datetime), `version`, `expandMetadata`, `limit` (integer), `cursor` (string), `parseIoAsJson` (boolean), `filter` (JSON-encoded string, see below).

Note the confirmed param names are `fromStartTime`/`toStartTime` and `limit`/`cursor` — **not** `fromTimestamp`/`toTimestamp` or `page`/`limit` as the design spec's Architecture section speculatively assumed before this verification. Per the design spec's own instruction ("tests must assert against whatever representation is actually implemented... this spec deliberately does not hardcode a guessed parameter name"), this plan implements the verified names.

**Response shape:** `{"data": [<ObservationV2>, ...], "meta": {"cursor": "<base64 string, absent/null when no further page>"}}`.

**Relevant `ObservationV2` fields:** `id`, `type`, `name`, `traceId`, `startTime` (ISO 8601 string), `endTime`, `level` (`DEBUG`/`DEFAULT`/`WARNING`/`ERROR`), `statusMessage`, `userId`, `sessionId`, `isRootObservation`, `parentObservationId`, `input`, `output`, `model`, `providedModelName`, `totalCost` (nullable double), `usageDetails` (`map<string, integer>`, modern replacement for the deprecated nested `usage.input`/`usage.output`/`usage.total` object — same key names carried forward), `metadata`; and, via the `trace_context` field group (`fields=...,trace_context`): `traceName`, `tags`, `environment`, `release`.

**Correction (post-implementation, per live v4 evidence):** this section originally listed only `providedModelName` as the model-name field, based on the fern spec's naming and not independently verified against a live response at the time. A live v4 Observations API run showed the model name actually comes back on `model` (e.g. `"gpt-4o-mini"`, `"claude-3-5-haiku-latest"`), with `providedModelName` returned as `None` in practice. `normalize_observation` and `infer_provider` were corrected to read `model` as primary, falling back to `providedModelName` for robustness — see the committed fix on `codex/langfuse-trace-import`.

**`filter` parameter** — a JSON-encoded array of condition objects, passed as a query-string value. Confirmed condition shape and operators (Langfuse's November 2025 "Advanced Filtering for Public Traces and Observations API" changelog, and GitHub discussion #12067 "Traces / Observation API filter for multiple names"):

```json
[
  {"type": "stringOptions", "column": "traceName", "operator": "any of", "value": ["name1", "name2"]},
  {"type": "arrayOptions", "column": "tags", "operator": "all of", "value": ["tag1", "tag2"]}
]
```

- `stringOptions` + `"any of"` → OR-of-many (exact match for `--trace-name`'s required OR semantics).
- `arrayOptions` + `"all of"` → AND-of-many (exact match for `--tag`'s required AND semantics; `arrayOptions` also supports `"any of"`/`"none of"`, unused here).
- Multiple conditions in the top-level array combine with AND — this matches Langfuse's UI filter-bar convention that the same `filter` parameter mirrors, and is consistent with every source consulted; no source directly contradicts it.

**Remaining verification gap, carried into Task 1's Step 1 as an explicit live-check:** the exact `column` name for trace name (`"traceName"`) and tags (`"tags"`) on the **observations** filter specifically — as opposed to the **traces** filter, which definitely supports them — is corroborated by the `ObservationV2` field list (which explicitly lists `traceName`/`tags` as available, denormalized-from-trace fields) but no single source quotes a byte-for-byte `filter` example using `column: "traceName"` against `/v2/observations`. Task 1 implements this as the primary hypothesis (it is the best-evidenced reading of multiple independent, current, official sources) and includes a concrete live-check step before the task is considered done.

**Langfuse Cloud default base URL:** `https://cloud.langfuse.com` (EU region default; confirmed live). The env var name is `LANGFUSE_BASE_URL`, Langfuse's current official default. **Corrected post-implementation:** this section originally named the env var `LANGFUSE_HOST`, reasoning at the time that changing it was "a spec-level decision outside this plan's scope" — that decision has since been made and implemented; every `LANGFUSE_HOST` reference throughout this plan has been updated to `LANGFUSE_BASE_URL` to match the committed code, with no `LANGFUSE_HOST` fallback retained.

---

## File Structure

- **Create:** `src/metergraphrelay/providers/langfuse.py` — HTTP fetch layer, filter/query-param construction, provider inference, content mapping, row normalization, and `pull_langfuse` pagination/orchestration. Mirrors `providers/openai.py`'s existing single-file, normalize-plus-pull shape.
- **Create:** `tests/providers/test_langfuse.py` — unit tests for every function above.
- **Modify:** `src/metergraphrelay/cli.py` — add langfuse flags to `pull_langfuse_parser`, add `--help` text, add `_resolve_langfuse_credentials`/`_run_pull_langfuse` helpers, replace the current `langfuse`-goes-through-`_not_implemented` stub branch with a real dispatch branch.
- **Modify:** `tests/test_cli.py` — replace the now-obsolete "reports not implemented" langfuse test, add dispatch/credential/base-url/help/doc-consistency tests.
- **Modify:** `tests/conftest.py:8-10` — add `LANGFUSE_BASE_URL` to the autouse-cleared env var set (a new env var this feature reads directly, outside `CREDENTIAL_SPECS`).
- **Modify:** `README.md` — add a "Pull from Langfuse" section (quickstart, credential/host config, selector examples, v4+/generation-only/privacy statements).
- **Modify:** `.env.example` — add a commented, optional `LANGFUSE_BASE_URL` line.
- **Not modified:** `src/metergraphrelay/config.py` (already correct), `src/metergraphrelay/push.py` (already provider-agnostic), `src/metergraphrelay/demo.py`, `pyproject.toml` (version bump is a separate follow-up chore commit per this repo's existing convention — see git log — not part of this feature's implementation).

---

### Task 1: Verified filter & query-parameter construction

**Files:**
- Create: `src/metergraphrelay/providers/langfuse.py`
- Test: `tests/providers/test_langfuse.py`

**Interfaces:**
- Produces: `build_filter(trace_names: list[str], tags: list[str]) -> str | None`; `build_base_params(*, until: str, since: str | None, trace_names: list[str], tags: list[str], environment: str | None) -> dict[str, str]`; module constants `DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"`, `OBSERVATIONS_PATH = "/api/public/v2/observations"`, `PAGE_LIMIT = 1000`, `RESPONSE_FIELDS = "core,basic,time,io,usage,model,trace_context"`.

- [ ] **Step 1: Live-check the verification gap before writing code**

Per the "Verified Langfuse v2 Observations API Reference" section above, one detail is not byte-for-byte confirmed: whether `column: "traceName"` and `column: "tags"` are accepted `filter` columns specifically on `/api/public/v2/observations` (as opposed to only on `/api/public/traces`). Before writing `build_filter`, do ONE of:

  a. If you have Langfuse Cloud credentials available: run
     ```bash
     curl -s -u "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" \
       "https://cloud.langfuse.com/api/public/v2/observations?type=GENERATION&limit=1&filter=%5B%7B%22type%22%3A%22stringOptions%22%2C%22column%22%3A%22traceName%22%2C%22operator%22%3A%22any%20of%22%2C%22value%22%3A%5B%22nonexistent-trace-name-xyz%22%5D%7D%5D"
     ```
     Expected on success: HTTP 200 with `{"data": [], "meta": {...}}` (empty because the trace name doesn't exist — the point is the request is *accepted*, not that it returns data). A 400 response with a message naming `traceName` as an invalid column means the hypothesis is wrong; in that case, re-run without the `filter` param and separately check the response's `traceName` field is present under `fields=core,basic,time,io,usage,model,trace_context`, then consult `https://api.reference.langfuse.com/` (Langfuse's interactive API reference) for the exact accepted column name and adjust Step 3's `column` values accordingly before proceeding.

  b. If no live credentials are available: check `https://api.reference.langfuse.com/` (Langfuse's interactive, always-current API reference, generated from the same source this plan already cited) for the `GET /api/public/v2/observations` endpoint's `filter` parameter documentation and confirm the `traceName`/`tags` column names directly from there instead.

  Record which method was used and the outcome in the Task 1 commit message.

- [ ] **Step 2: Write the failing tests for `build_filter`**

```python
# tests/providers/test_langfuse.py
import json

from metergraphrelay.providers.langfuse import build_base_params, build_filter


def test_build_filter_returns_none_when_no_selectors():
    assert build_filter([], []) is None


def test_build_filter_trace_name_uses_string_options_any_of():
    result = build_filter(["support-bot", "billing-bot"], [])
    assert json.loads(result) == [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": ["support-bot", "billing-bot"],
        }
    ]


def test_build_filter_tags_uses_array_options_all_of():
    result = build_filter([], ["prod", "tier-1"])
    assert json.loads(result) == [
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod", "tier-1"],
        }
    ]


def test_build_filter_combines_trace_name_and_tags():
    result = build_filter(["support-bot"], ["prod"])
    assert json.loads(result) == [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": ["support-bot"],
        },
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod"],
        },
    ]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `pytest tests/providers/test_langfuse.py -v`
Expected: `ERROR` / `ModuleNotFoundError: No module named 'metergraphrelay.providers.langfuse'` (the module doesn't exist yet).

- [ ] **Step 4: Create the module with `build_filter`**

```python
# src/metergraphrelay/providers/langfuse.py
from __future__ import annotations

import json
from typing import Any

DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"
OBSERVATIONS_PATH = "/api/public/v2/observations"
PAGE_LIMIT = 1000
# core+basic+time cover id/type/name/traceId/startTime/endTime/level/statusMessage/
# parentObservationId/sessionId; io covers input/output; usage covers usageDetails
# and totalCost; model covers providedModelName; trace_context denormalizes
# traceName/tags/environment/release onto each observation. Requesting all of them
# up front avoids silently missing a field the normalize step depends on.
RESPONSE_FIELDS = "core,basic,time,io,usage,model,trace_context"


def build_filter(trace_names: list[str], tags: list[str]) -> str | None:
    conditions: list[dict[str, Any]] = []
    if trace_names:
        conditions.append(
            {
                "type": "stringOptions",
                "column": "traceName",
                "operator": "any of",
                "value": list(trace_names),
            }
        )
    if tags:
        conditions.append(
            {
                "type": "arrayOptions",
                "column": "tags",
                "operator": "all of",
                "value": list(tags),
            }
        )
    if not conditions:
        return None
    return json.dumps(conditions)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/providers/test_langfuse.py -v`
Expected: 4 passed.

- [ ] **Step 6: Write the failing tests for `build_base_params`**

```python
def test_build_base_params_minimal_defaults():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since=None,
        trace_names=[],
        tags=[],
        environment=None,
    )
    assert params["type"] == "GENERATION"
    assert params["toStartTime"] == "2026-08-07T00:00:00+00:00"
    assert "fromStartTime" not in params
    assert "environment" not in params
    assert "filter" not in params


def test_build_base_params_includes_since_and_environment():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since="2026-08-01T00:00:00+00:00",
        trace_names=[],
        tags=[],
        environment="production",
    )
    assert params["fromStartTime"] == "2026-08-01T00:00:00+00:00"
    assert params["environment"] == "production"


def test_build_base_params_includes_filter_when_selectors_given():
    params = build_base_params(
        until="2026-08-07T00:00:00+00:00",
        since=None,
        trace_names=["support-bot"],
        tags=["prod"],
        environment=None,
    )
    assert json.loads(params["filter"]) == [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": ["support-bot"],
        },
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod"],
        },
    ]
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `pytest tests/providers/test_langfuse.py -v -k build_base_params`
Expected: FAIL with `AttributeError` / `ImportError: cannot import name 'build_base_params'`.

- [ ] **Step 8: Implement `build_base_params`**

```python
def build_base_params(
    *,
    until: str,
    since: str | None,
    trace_names: list[str],
    tags: list[str],
    environment: str | None,
) -> dict[str, str]:
    params: dict[str, str] = {
        "type": "GENERATION",
        "toStartTime": until,
        "fields": RESPONSE_FIELDS,
        "parseIoAsJson": "true",
    }
    if since:
        params["fromStartTime"] = since
    if environment:
        params["environment"] = environment
    filter_json = build_filter(trace_names, tags)
    if filter_json:
        params["filter"] = filter_json
    return params
```

- [ ] **Step 9: Run all tests in this task to verify they pass**

Run: `pytest tests/providers/test_langfuse.py -v`
Expected: 7 passed.

- [ ] **Step 10: Commit**

```bash
git add src/metergraphrelay/providers/langfuse.py tests/providers/test_langfuse.py
git commit -m "feat(langfuse): add verified filter/query-parameter construction

Verified against Langfuse's own fern API definitions (observations.yml,
commons.yml) plus official changelog/discussion sources; live-checked
via <method from Step 1> that traceName/tags are valid filter columns
on GET /api/public/v2/observations."
```

---

### Task 2: HTTP fetch layer

**Files:**
- Modify: `src/metergraphrelay/providers/langfuse.py`
- Test: `tests/providers/test_langfuse.py`

**Interfaces:**
- Consumes: nothing from Task 1 directly (independent layer); shares the module.
- Produces: `class LangfuseAPIError(Exception)`; `fetch_observations_page(base_url: str, public_key: str, secret_key: str, params: dict[str, str]) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/providers/test_langfuse.py (additions)
import base64
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from metergraphrelay.providers.langfuse import LangfuseAPIError, fetch_observations_page


def _mock_response(status, body: bytes):
    response = MagicMock()
    response.status = status
    response.read.return_value = body
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def test_fetch_observations_page_sends_basic_auth_header():
    body = json.dumps({"data": [], "meta": {"cursor": None}}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        fetch_observations_page(
            "https://cloud.langfuse.com", "pk-1", "sk-1", {"type": "GENERATION"}
        )

    request = mock_urlopen.call_args.args[0]
    expected = "Basic " + base64.b64encode(b"pk-1:sk-1").decode()
    assert request.get_header("Authorization") == expected


def test_fetch_observations_page_builds_correct_url():
    body = json.dumps({"data": [], "meta": {"cursor": None}}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        fetch_observations_page(
            "https://cloud.langfuse.com",
            "pk-1",
            "sk-1",
            {"type": "GENERATION", "limit": "10"},
        )

    request = mock_urlopen.call_args.args[0]
    assert request.full_url == (
        "https://cloud.langfuse.com/api/public/v2/observations"
        "?type=GENERATION&limit=10"
    )


def test_fetch_observations_page_returns_parsed_payload():
    body = json.dumps({"data": [{"id": "obs-1"}], "meta": {"cursor": "abc"}}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        payload = fetch_observations_page(
            "https://cloud.langfuse.com", "pk-1", "sk-1", {}
        )

    assert payload == {"data": [{"id": "obs-1"}], "meta": {"cursor": "abc"}}


def test_fetch_observations_page_raises_on_http_error():
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://cloud.langfuse.com/api/public/v2/observations",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        with pytest.raises(LangfuseAPIError, match="401"):
            fetch_observations_page("https://cloud.langfuse.com", "pk-1", "sk-1", {})


def test_fetch_observations_page_raises_on_network_error():
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        with pytest.raises(LangfuseAPIError, match="connection refused"):
            fetch_observations_page("https://cloud.langfuse.com", "pk-1", "sk-1", {})


def test_fetch_observations_page_raises_on_malformed_json():
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, b"not json")
        with pytest.raises(LangfuseAPIError, match="invalid JSON"):
            fetch_observations_page("https://cloud.langfuse.com", "pk-1", "sk-1", {})


def test_fetch_observations_page_raises_when_response_missing_data_or_meta():
    body = json.dumps({"unexpected": "shape"}).encode()
    with patch(
        "metergraphrelay.providers.langfuse.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(200, body)
        with pytest.raises(LangfuseAPIError, match="v4"):
            fetch_observations_page("https://cloud.langfuse.com", "pk-1", "sk-1", {})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/providers/test_langfuse.py -v -k fetch_observations_page`
Expected: FAIL with `ImportError: cannot import name 'LangfuseAPIError'`.

- [ ] **Step 3: Implement the HTTP fetch layer**

```python
# src/metergraphrelay/providers/langfuse.py (additions — add these imports at the top
# alongside the existing `import json` / `from typing import Any`)
import base64
import urllib.error
import urllib.parse
import urllib.request


class LangfuseAPIError(Exception):
    """Raised when Langfuse's API returns an error response or an unusable body."""


def _auth_header(public_key: str, secret_key: str) -> str:
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return f"Basic {token}"


def fetch_observations_page(
    base_url: str,
    public_key: str,
    secret_key: str,
    params: dict[str, str],
) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{base_url.rstrip('/')}{OBSERVATIONS_PATH}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": _auth_header(public_key, secret_key),
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise LangfuseAPIError(
            f"Langfuse API request failed: HTTP {exc.code} {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LangfuseAPIError(f"Langfuse API request failed: {exc.reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LangfuseAPIError(f"Langfuse API returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or "data" not in payload or "meta" not in payload:
        raise LangfuseAPIError(
            "Langfuse API response missing 'data'/'meta' — unsupported deployment "
            "or unexpected response shape (self-hosted v4+ with the v2 "
            "Observations API is required)"
        )
    return payload
```

- [ ] **Step 4: Run all tests in this task to verify they pass**

Run: `pytest tests/providers/test_langfuse.py -v -k fetch_observations_page`
Expected: 7 passed.

- [ ] **Step 5: Run the full test file to confirm no regressions**

Run: `pytest tests/providers/test_langfuse.py -v`
Expected: 14 passed (7 from Task 1 + 7 from this task).

- [ ] **Step 6: Commit**

```bash
git add src/metergraphrelay/providers/langfuse.py tests/providers/test_langfuse.py
git commit -m "feat(langfuse): add HTTP fetch layer with Basic Auth and error handling"
```

---

### Task 3: Provider inference

**Files:**
- Modify: `src/metergraphrelay/providers/langfuse.py`
- Test: `tests/providers/test_langfuse.py`

**Interfaces:**
- Produces: `infer_provider(observation: dict[str, Any]) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
from metergraphrelay.providers.langfuse import infer_provider


def test_infer_provider_uses_explicit_metadata_when_present():
    observation = {
        "metadata": {"provider": "openai"},
        "providedModelName": "claude-3-opus",
    }
    assert infer_provider(observation) == "openai"


def test_infer_provider_falls_back_to_model_family_prefix_openai():
    assert infer_provider({"providedModelName": "gpt-4o-mini"}) == "openai"


def test_infer_provider_falls_back_to_model_family_prefix_anthropic():
    assert infer_provider({"providedModelName": "claude-3-opus"}) == "anthropic"


def test_infer_provider_returns_unknown_when_no_match():
    assert infer_provider({"providedModelName": "some-custom-model"}) == "unknown"


def test_infer_provider_returns_unknown_when_model_name_missing():
    assert infer_provider({}) == "unknown"


def test_infer_provider_ignores_non_dict_metadata():
    assert infer_provider({"metadata": "not-a-dict", "providedModelName": "gpt-4o"}) == "openai"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/providers/test_langfuse.py -v -k infer_provider`
Expected: FAIL with `ImportError: cannot import name 'infer_provider'`.

- [ ] **Step 3: Implement `infer_provider`**

```python
# src/metergraphrelay/providers/langfuse.py (additions)
# Illustrative, not exhaustive — per the design spec's Mapping section, the
# concrete prefix table is an implementation-time task, not invented wholesale.
_PROVIDER_MODEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("chatgpt-", "openai"),
    ("claude-", "anthropic"),
    ("gemini-", "google"),
)


def infer_provider(observation: dict[str, Any]) -> str:
    metadata = observation.get("metadata")
    if isinstance(metadata, dict):
        explicit = metadata.get("provider")
        if explicit:
            return explicit
    model_name = (observation.get("providedModelName") or "").lower()
    for prefix, provider in _PROVIDER_MODEL_PREFIXES:
        if model_name.startswith(prefix):
            return provider
    return "unknown"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/providers/test_langfuse.py -v -k infer_provider`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/providers/langfuse.py tests/providers/test_langfuse.py
git commit -m "feat(langfuse): add conservative provider inference"
```

---

### Task 4: Content mapping helpers

**Files:**
- Modify: `src/metergraphrelay/providers/langfuse.py`
- Test: `tests/providers/test_langfuse.py`

**Interfaces:**
- Produces: `_map_content(input_value: Any) -> tuple[str | None, str | None]` (returns `(request_json, request_text)`); `_response_text(output_value: Any) -> str | None`.

- [ ] **Step 1: Write the failing tests**

```python
from metergraphrelay.providers.langfuse import _map_content, _response_text


def test_map_content_chat_message_list_becomes_request_json():
    result = _map_content([{"role": "user", "content": "hi"}])
    assert result == (json.dumps([{"role": "user", "content": "hi"}]), None)


def test_map_content_string_becomes_request_text():
    assert _map_content("plain prompt text") == (None, "plain prompt text")


def test_map_content_none_stays_none():
    assert _map_content(None) == (None, None)


def test_map_content_arbitrary_dict_becomes_request_text_as_json():
    result = _map_content({"foo": "bar"})
    assert result == (None, json.dumps({"foo": "bar"}))


def test_map_content_non_message_list_becomes_request_text_as_json():
    result = _map_content([1, 2, 3])
    assert result == (None, json.dumps([1, 2, 3]))


def test_response_text_passes_through_string():
    assert _response_text("the reply") == "the reply"


def test_response_text_serializes_non_string():
    assert _response_text({"foo": "bar"}) == json.dumps({"foo": "bar"})


def test_response_text_none_stays_none():
    assert _response_text(None) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/providers/test_langfuse.py -v -k "map_content or response_text"`
Expected: FAIL with `ImportError: cannot import name '_map_content'`.

- [ ] **Step 3: Implement the content mapping helpers**

```python
# src/metergraphrelay/providers/langfuse.py (additions)
def _is_chat_message_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and "role" in item and "content" in item
        for item in value
    )


def _map_content(input_value: Any) -> tuple[str | None, str | None]:
    if input_value is None:
        return None, None
    if _is_chat_message_list(input_value):
        return json.dumps(input_value), None
    if isinstance(input_value, str):
        return None, input_value
    return None, json.dumps(input_value)


def _response_text(output_value: Any) -> str | None:
    if output_value is None:
        return None
    if isinstance(output_value, str):
        return output_value
    return json.dumps(output_value)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/providers/test_langfuse.py -v -k "map_content or response_text"`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/metergraphrelay/providers/langfuse.py tests/providers/test_langfuse.py
git commit -m "feat(langfuse): add lossless request/response content mapping"
```

---

### Task 5: Full row normalization

**Files:**
- Modify: `src/metergraphrelay/providers/langfuse.py`
- Test: `tests/providers/test_langfuse.py`

**Interfaces:**
- Consumes: `infer_provider` (Task 3), `_map_content`/`_response_text` (Task 4).
- Produces: `normalize_observation(observation: dict[str, Any], *, route_override: str | None) -> dict`.

This task also resolves three fields the design spec's Mapping table does not explicitly assign, using the codebase's own established "omit rather than fabricate" precedent (`docs/superpowers/specs/2026-07-31-metergraphrelay-rebrand-push-design.md`, "Row translation happens at pull time" section) and `pull_openai`'s existing behavior as the reference:

- `endpoint`: omitted entirely (not sent as `null`) — GENERATION observations have no "endpoint" concept the way `chat.completions` does for OpenAI, and the established convention is to omit fields with no source data rather than invent one.
- `content_opted_in`: fixed `True` — unlike `pull openai`, there is no `--include-content` gate for Langfuse (design spec: Explicit content-transfer warning), so every row's content is, factually, always opted-in.
- `request_id`: set to the observation's own `id` (the same value as `span_id`) — the closest analog to `pull_openai`'s `request_id = completion.id`, i.e. "this call's unique identifier."

**Route/tags interaction** (design spec: Mapping, `route` and `(name metadata)` rows): if `--route` was passed, the trace/observation name is *not* consumed by `route` and is instead preserved under `tags["name"]` so it isn't silently dropped; if `--route` was not passed, the name fallback *becomes* `route` and is not duplicated into `tags`.

- [ ] **Step 1: Write the failing tests**

```python
from metergraphrelay import __version__
from metergraphrelay.providers.langfuse import normalize_observation


def make_observation(**overrides):
    defaults = dict(
        id="obs-1",
        traceId="trace-1",
        type="GENERATION",
        startTime="2026-08-07T12:00:00+00:00",
        level="DEFAULT",
        statusMessage=None,
        parentObservationId=None,
        sessionId=None,
        providedModelName="gpt-4o-mini",
        input=[{"role": "user", "content": "hi"}],
        output="hello",
        usageDetails={"input": 12, "output": 34},
        totalCost=0.0012,
        metadata={},
        traceName="support-bot",
        tags=["prod", "tier-1"],
        environment="production",
        name="chat-completion",
    )
    defaults.update(overrides)
    return defaults


def test_normalize_observation_full_row():
    observation = make_observation()

    row = normalize_observation(observation, route_override=None)

    assert row == {
        "ts": "2026-08-07T12:00:00+00:00",
        "source": "langfuse",
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
        "provider": "openai",
        "model": "gpt-4o-mini",
        "status": "success",
        "input_tokens": 12,
        "output_tokens": 34,
        "cost_usd": 0.0012,
        "error": False,
        "error_type": None,
        "request_id": "obs-1",
        "tags": {"langfuse_tags": ["prod", "tier-1"]},
        "route": "support-bot",
        "content_opted_in": True,
        "request_json": json.dumps([{"role": "user", "content": "hi"}]),
        "request_text": None,
        "response_text": "hello",
        "trace_id": "trace-1",
        "span_id": "obs-1",
        "parent_span_id": None,
        "session_id": None,
        "environment": "production",
    }


def test_normalize_observation_route_override_preserves_name_in_tags():
    observation = make_observation(tags=[])

    row = normalize_observation(observation, route_override="my-app/custom-route")

    assert row["route"] == "my-app/custom-route"
    assert row["tags"] == {"name": "support-bot"}


def test_normalize_observation_falls_back_to_observation_name_when_trace_has_none():
    observation = make_observation(traceName=None, name="raw-generation", tags=[])

    row = normalize_observation(observation, route_override=None)

    assert row["route"] == "raw-generation"
    assert row["tags"] == {}


def test_normalize_observation_error_level_sets_error_and_status():
    observation = make_observation(level="ERROR", statusMessage="rate limited")

    row = normalize_observation(observation, route_override=None)

    assert row["status"] == "error"
    assert row["error"] is True
    assert row["error_type"] == "rate limited"


def test_normalize_observation_missing_usage_details_yields_none_tokens():
    observation = make_observation(usageDetails={})

    row = normalize_observation(observation, route_override=None)

    assert row["input_tokens"] is None
    assert row["output_tokens"] is None


def test_normalize_observation_missing_required_field_raises_key_error():
    observation = make_observation()
    del observation["startTime"]

    with pytest.raises(KeyError):
        normalize_observation(observation, route_override=None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/providers/test_langfuse.py -v -k normalize_observation`
Expected: FAIL with `ImportError: cannot import name 'normalize_observation'`.

- [ ] **Step 3: Implement `normalize_observation`**

```python
# src/metergraphrelay/providers/langfuse.py (additions — add
# `from .. import __version__` to the existing imports at the top of the file)
def normalize_observation(
    observation: dict[str, Any], *, route_override: str | None
) -> dict:
    trace_name = observation.get("traceName") or None
    own_name = observation.get("name") or None
    name_fallback = trace_name or own_name

    if route_override:
        route = route_override
        name_consumed = False
    else:
        route = name_fallback or ""
        name_consumed = True

    tags: dict[str, Any] = {}
    langfuse_tags = observation.get("tags")
    if langfuse_tags:
        tags["langfuse_tags"] = list(langfuse_tags)
    if not name_consumed and name_fallback:
        tags["name"] = name_fallback

    error = observation.get("level") == "ERROR"
    error_type = observation.get("statusMessage") if error else None

    usage_details = observation.get("usageDetails") or {}
    request_json, request_text = _map_content(observation.get("input"))
    response_text = _response_text(observation.get("output"))

    return {
        "ts": observation["startTime"],
        "source": "langfuse",
        "sdk": "metergraphrelay",
        "sdk_version": __version__,
        "provider": infer_provider(observation),
        "model": observation.get("providedModelName"),
        "status": "error" if error else "success",
        "input_tokens": usage_details.get("input"),
        "output_tokens": usage_details.get("output"),
        "cost_usd": observation.get("totalCost"),
        "error": error,
        "error_type": error_type,
        "request_id": observation["id"],
        "tags": tags,
        "route": route,
        "content_opted_in": True,
        "request_json": request_json,
        "request_text": request_text,
        "response_text": response_text,
        "trace_id": observation["traceId"],
        "span_id": observation["id"],
        "parent_span_id": observation.get("parentObservationId"),
        "session_id": observation.get("sessionId"),
        "environment": observation.get("environment"),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/providers/test_langfuse.py -v -k normalize_observation`
Expected: 6 passed.

- [ ] **Step 5: Run the full test file to confirm no regressions**

Run: `pytest tests/providers/test_langfuse.py -v`
Expected: 34 passed (7 + 7 + 6 + 8 + 6 across Tasks 1–5).

- [ ] **Step 6: Commit**

```bash
git add src/metergraphrelay/providers/langfuse.py tests/providers/test_langfuse.py
git commit -m "feat(langfuse): normalize GENERATION observations into metergraph rows"
```

---

### Task 6: Pagination and orchestration

**Files:**
- Modify: `src/metergraphrelay/providers/langfuse.py`
- Test: `tests/providers/test_langfuse.py`

**Interfaces:**
- Consumes: `build_base_params` (Task 1), `fetch_observations_page`/`LangfuseAPIError` (Task 2), `normalize_observation` (Task 5).
- Produces: `pull_langfuse(*, base_url: str, public_key: str, secret_key: str, count: int, since: str | None, until: str, trace_names: list[str], tags: list[str], environment: str | None, route: str | None, output_path: str) -> tuple[int, int]` (returns `(imported, skipped)`).

- [ ] **Step 1: Write the failing tests for single-page and count-cap behavior**

```python
from metergraphrelay.providers.langfuse import pull_langfuse


def _call_pull_langfuse(output_path, **overrides):
    kwargs = dict(
        base_url="https://cloud.langfuse.com",
        public_key="pk-1",
        secret_key="sk-1",
        count=10,
        since=None,
        until="2026-08-07T00:00:00+00:00",
        trace_names=[],
        tags=[],
        environment=None,
        route=None,
        output_path=str(output_path),
    )
    kwargs.update(overrides)
    return pull_langfuse(**kwargs)


def test_pull_langfuse_single_page_under_count(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    observations = [
        make_observation(id=f"obs-{i}", traceId=f"trace-{i}") for i in range(3)
    ]
    payload = {"data": observations, "meta": {"cursor": None}}

    with patch(
        "metergraphrelay.providers.langfuse.fetch_observations_page",
        return_value=payload,
    ) as mock_fetch:
        imported, skipped = _call_pull_langfuse(output_path)

    assert imported == 3
    assert skipped == 0
    mock_fetch.assert_called_once()
    assert len(output_path.read_text().splitlines()) == 3


def test_pull_langfuse_stops_at_count_cap_mid_page(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    observations = [
        make_observation(id=f"obs-{i}", traceId=f"trace-{i}") for i in range(5)
    ]
    payload = {"data": observations, "meta": {"cursor": None}}

    with patch(
        "metergraphrelay.providers.langfuse.fetch_observations_page",
        return_value=payload,
    ):
        imported, skipped = _call_pull_langfuse(output_path, count=2)

    assert imported == 2
    assert len(output_path.read_text().splitlines()) == 2


def test_pull_langfuse_stops_when_page_is_empty(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    payload = {"data": [], "meta": {"cursor": None}}

    with patch(
        "metergraphrelay.providers.langfuse.fetch_observations_page",
        return_value=payload,
    ) as mock_fetch:
        imported, skipped = _call_pull_langfuse(output_path)

    assert imported == 0
    assert mock_fetch.call_count == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/providers/test_langfuse.py -v -k pull_langfuse`
Expected: FAIL with `ImportError: cannot import name 'pull_langfuse'`.

- [ ] **Step 3: Implement `pull_langfuse` (core loop, no cursor yet)**

```python
# src/metergraphrelay/providers/langfuse.py (additions — add `import os` and
# `import sys` to the existing imports at the top of the file)
def pull_langfuse(
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    count: int,
    since: str | None,
    until: str,
    trace_names: list[str],
    tags: list[str],
    environment: str | None,
    route: str | None,
    output_path: str,
) -> tuple[int, int]:
    base_params = build_base_params(
        until=until,
        since=since,
        trace_names=trace_names,
        tags=tags,
        environment=environment,
    )
    imported = 0
    skipped = 0
    rows: list[str] = []
    cursor: str | None = None

    while imported < count:
        page_params = dict(base_params)
        page_params["limit"] = str(min(PAGE_LIMIT, count - imported))
        if cursor:
            page_params["cursor"] = cursor
        payload = fetch_observations_page(base_url, public_key, secret_key, page_params)
        observations = payload["data"]
        if not observations:
            break
        for observation in observations:
            if imported >= count:
                break
            try:
                row = normalize_observation(observation, route_override=route)
            except (KeyError, TypeError, AttributeError) as exc:
                skipped += 1
                obs_id = (
                    observation.get("id", "<unknown>")
                    if isinstance(observation, dict)
                    else "<unknown>"
                )
                print(
                    f"Warning: skipping malformed observation {obs_id}: {exc}",
                    file=sys.stderr,
                )
                continue
            rows.append(json.dumps(row))
            imported += 1
        cursor = payload.get("meta", {}).get("cursor")
        if not cursor:
            break

    tmp_path = f"{output_path}.tmp"
    with open(tmp_path, "w") as f:
        for line in rows:
            f.write(line + "\n")
    os.replace(tmp_path, output_path)
    return imported, skipped
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/providers/test_langfuse.py -v -k pull_langfuse`
Expected: 3 passed.

- [ ] **Step 5: Write the failing tests for cursor pagination, selector propagation, malformed-row skip, and atomicity**

```python
def test_pull_langfuse_follows_cursor_across_pages(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    page_1 = {
        "data": [make_observation(id="obs-1", traceId="trace-1")],
        "meta": {"cursor": "next-page-token"},
    }
    page_2 = {
        "data": [make_observation(id="obs-2", traceId="trace-2")],
        "meta": {"cursor": None},
    }

    with patch(
        "metergraphrelay.providers.langfuse.fetch_observations_page",
        side_effect=[page_1, page_2],
    ) as mock_fetch:
        imported, skipped = _call_pull_langfuse(output_path)

    assert imported == 2
    assert mock_fetch.call_count == 2
    second_call_params = mock_fetch.call_args_list[1].args[3]
    assert second_call_params["cursor"] == "next-page-token"


def test_pull_langfuse_passes_selectors_into_a_single_combined_request(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    payload = {"data": [], "meta": {"cursor": None}}

    with patch(
        "metergraphrelay.providers.langfuse.fetch_observations_page",
        return_value=payload,
    ) as mock_fetch:
        _call_pull_langfuse(
            output_path,
            since="2026-08-01T00:00:00+00:00",
            trace_names=["support-bot"],
            tags=["prod"],
            environment="production",
        )

    params = mock_fetch.call_args.args[3]
    assert params["fromStartTime"] == "2026-08-01T00:00:00+00:00"
    assert params["toStartTime"] == "2026-08-07T00:00:00+00:00"
    assert params["environment"] == "production"
    assert json.loads(params["filter"]) == [
        {
            "type": "stringOptions",
            "column": "traceName",
            "operator": "any of",
            "value": ["support-bot"],
        },
        {
            "type": "arrayOptions",
            "column": "tags",
            "operator": "all of",
            "value": ["prod"],
        },
    ]


def test_pull_langfuse_skips_malformed_observation_and_continues(tmp_path, capsys):
    output_path = tmp_path / "traces.jsonl"
    good = make_observation(id="obs-good", traceId="trace-1")
    bad = make_observation(id="obs-bad", traceId="trace-2")
    del bad["startTime"]
    payload = {"data": [bad, good], "meta": {"cursor": None}}

    with patch(
        "metergraphrelay.providers.langfuse.fetch_observations_page",
        return_value=payload,
    ):
        imported, skipped = _call_pull_langfuse(output_path)

    assert imported == 1
    assert skipped == 1
    captured = capsys.readouterr()
    assert "obs-bad" in captured.err
    assert len(output_path.read_text().splitlines()) == 1


def test_pull_langfuse_writes_nothing_when_a_page_request_fails(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    output_path.write_text("sentinel-content\n")

    with patch(
        "metergraphrelay.providers.langfuse.fetch_observations_page",
        side_effect=LangfuseAPIError("boom"),
    ):
        with pytest.raises(LangfuseAPIError):
            _call_pull_langfuse(output_path)

    assert output_path.read_text() == "sentinel-content\n"


def test_pull_langfuse_writes_via_temp_file_and_leaves_no_leftover(tmp_path):
    output_path = tmp_path / "traces.jsonl"
    payload = {"data": [make_observation()], "meta": {"cursor": None}}

    with patch(
        "metergraphrelay.providers.langfuse.fetch_observations_page",
        return_value=payload,
    ):
        _call_pull_langfuse(output_path)

    assert output_path.exists()
    assert not (tmp_path / "traces.jsonl.tmp").exists()
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `pytest tests/providers/test_langfuse.py -v -k "cursor or combined_request or skips_malformed or writes_nothing or leftover"`
Expected: FAIL — `test_pull_langfuse_follows_cursor_across_pages` fails because the implementation from Step 3 does not yet exist in a state that's been verified against a second-page assertion (run this before Step 3's cursor logic is trusted, to confirm the test harness itself is exercising real behavior, not a false pass). If Step 3 already made these pass, skip re-verifying and proceed directly to Step 7 — but confirm by temporarily commenting out the `page_params["cursor"] = cursor` line and re-running to see the test go red, then restore it.

- [ ] **Step 7: Run the full test file to confirm everything passes**

Run: `pytest tests/providers/test_langfuse.py -v`
Expected: 39 passed (34 from Tasks 1–5 + 5 new in this task; the 3 from Step 1 of this task already counted separately — total after this task is 3 + 5 = 8 new, 34 + 8 = 42 passed). Re-run with plain `pytest tests/providers/test_langfuse.py` and confirm the exact printed count matches the number of test functions in the file (`grep -c "^def test_" tests/providers/test_langfuse.py`).

- [ ] **Step 8: Commit**

```bash
git add src/metergraphrelay/providers/langfuse.py tests/providers/test_langfuse.py
git commit -m "feat(langfuse): add cursor pagination, count cap, and atomic output write"
```

---

### Task 7: CLI wiring

**Files:**
- Modify: `src/metergraphrelay/cli.py`
- Modify: `tests/conftest.py:8-10`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `pull_langfuse`, `LangfuseAPIError`, `DEFAULT_LANGFUSE_HOST` from `metergraphrelay.providers.langfuse` (Tasks 1, 2, 6).
- Produces: `_resolve_langfuse_credentials(args: argparse.Namespace) -> tuple[str, str]`; `_run_pull_langfuse(args: argparse.Namespace) -> int` in `cli.py`.

- [ ] **Step 1: Add `LANGFUSE_BASE_URL` to the cleared-env-vars fixture**

`tests/conftest.py:8-10` currently reads:

```python
ENV_VARS_READ_BY_CLI = sorted(
    {name for names in CREDENTIAL_SPECS.values() for name in names}
    | {"METERGRAPH_INGEST_URL"}
)
```

Change to:

```python
ENV_VARS_READ_BY_CLI = sorted(
    {name for names in CREDENTIAL_SPECS.values() for name in names}
    | {"METERGRAPH_INGEST_URL", "LANGFUSE_BASE_URL"}
)
```

This is required before any new CLI test below can be trusted — `LANGFUSE_BASE_URL` is read directly via `os.environ.get`, outside `CREDENTIAL_SPECS`, so without this change a leftover value from one test's `.env` file could leak into the next test via `load_dotenv(..., override=True)`.

- [ ] **Step 2: Write the failing test replacing the obsolete "not implemented" langfuse test**

`tests/test_cli.py` currently has (lines 107–118):

```python
def test_main_pull_langfuse_reports_not_implemented_when_credentials_present(
    tmp_path, capsys
):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    exit_code = main(["pull", "langfuse", "--env-file", str(env_file)])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "not implemented" in captured.err.lower()
    assert "langfuse" in captured.err.lower()
```

Replace it with:

```python
def test_main_pull_langfuse_dispatches_to_pull_langfuse(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")
    output_path = tmp_path / "out.jsonl"

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(5, 1)
    ) as mock_pull:
        exit_code = main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "-n",
                "5",
                "--output",
                str(output_path),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert exit_code == 0
    mock_pull.assert_called_once_with(
        base_url="https://cloud.langfuse.com",
        public_key="pk-1",
        secret_key="sk-1",
        count=5,
        since=None,
        until="2026-08-07T00:00:00+00:00",
        trace_names=[],
        tags=[],
        environment=None,
        route=None,
        output_path=str(output_path),
    )
```

Also add, at the top of `tests/test_cli.py`, alongside the existing `from unittest.mock import patch`:

```python
from datetime import datetime

from metergraphrelay.cli import build_parser, main
from metergraphrelay.providers.langfuse import LangfuseAPIError
```

(`build_parser` and `LangfuseAPIError` are needed by tests added in this task and Task 8; `main` was already imported — keep that one line, just add `build_parser` to it: `from metergraphrelay.cli import build_parser, main`.)

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_cli.py -v -k test_main_pull_langfuse_dispatches_to_pull_langfuse`
Expected: FAIL — `ImportError: cannot import name 'pull_langfuse' from 'metergraphrelay.cli'` (not wired up yet).

- [ ] **Step 4: Wire up the CLI — imports, parser flags, dispatch**

In `src/metergraphrelay/cli.py`, change the top imports from:

```python
from __future__ import annotations

import argparse
import os
import sys

from openai import OpenAI

from .config import ConfigError, require_credentials
from .demo import run_demo
from .providers.openai import pull_openai
from .push import push_file
```

to:

```python
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from openai import OpenAI

from .config import ConfigError, require_credentials
from .demo import run_demo
from .providers.langfuse import DEFAULT_LANGFUSE_HOST, LangfuseAPIError, pull_langfuse
from .providers.openai import pull_openai
from .push import push_file
```

Replace the current langfuse stub parser (lines 44–46):

```python
    pull_langfuse_parser = pull_subparsers.add_parser("langfuse")
    pull_langfuse_parser.add_argument("--output", default="./traces.jsonl")
    pull_langfuse_parser.add_argument("--env-file", default=".env")
```

with:

```python
    pull_langfuse_parser = pull_subparsers.add_parser("langfuse")
    pull_langfuse_parser.add_argument("-n", "--count", type=int, default=100)
    pull_langfuse_parser.add_argument("--since", default=None)
    pull_langfuse_parser.add_argument("--until", default=None)
    pull_langfuse_parser.add_argument("--trace-name", action="append", default=None)
    pull_langfuse_parser.add_argument("--tag", action="append", default=None)
    pull_langfuse_parser.add_argument("--environment", default=None)
    pull_langfuse_parser.add_argument("--route", default=None)
    pull_langfuse_parser.add_argument("--base-url", default=None)
    pull_langfuse_parser.add_argument("--output", default="./traces.jsonl")
    pull_langfuse_parser.add_argument("--env-file", default=".env")
    pull_langfuse_parser.add_argument("--langfuse-public-key", default=None)
    pull_langfuse_parser.add_argument("--langfuse-secret-key", default=None)
```

(`--help` text for each of these is added in Task 8 — this step only wires up parsing and dispatch, keeping this task focused per the write-test/implement/pass rhythm.)

Replace the dispatch branch (lines 100–105):

```python
    if args.command == "pull" and args.provider in {"anthropic", "langfuse"}:
        try:
            require_credentials(args.provider, args.env_file)
        except ConfigError as exc:
            return _config_error(exc)
        return _not_implemented(args.provider)
```

with:

```python
    if args.command == "pull" and args.provider == "anthropic":
        try:
            require_credentials(args.provider, args.env_file)
        except ConfigError as exc:
            return _config_error(exc)
        return _not_implemented(args.provider)

    if args.command == "pull" and args.provider == "langfuse":
        return _run_pull_langfuse(args)
```

Add two new helper functions right after `_not_implemented` (before `def main`):

```python
def _resolve_langfuse_credentials(args: argparse.Namespace) -> tuple[str, str]:
    if args.langfuse_public_key and args.langfuse_secret_key:
        return args.langfuse_public_key, args.langfuse_secret_key
    creds = require_credentials("langfuse", args.env_file)
    return creds["LANGFUSE_PUBLIC_KEY"], creds["LANGFUSE_SECRET_KEY"]


def _run_pull_langfuse(args: argparse.Namespace) -> int:
    try:
        public_key, secret_key = _resolve_langfuse_credentials(args)
    except ConfigError as exc:
        return _config_error(exc)
    base_url = args.base_url or os.environ.get("LANGFUSE_BASE_URL") or DEFAULT_LANGFUSE_HOST
    until = args.until or datetime.now(timezone.utc).isoformat()
    try:
        imported, skipped = pull_langfuse(
            base_url=base_url,
            public_key=public_key,
            secret_key=secret_key,
            count=args.count,
            since=args.since,
            until=until,
            trace_names=args.trace_name or [],
            tags=args.tag or [],
            environment=args.environment,
            route=args.route,
            output_path=args.output,
        )
    except (LangfuseAPIError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {imported} trace(s), skipped {skipped}, to {args.output}")
    return 0
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_cli.py -v -k test_main_pull_langfuse_dispatches_to_pull_langfuse`
Expected: 1 passed.

- [ ] **Step 6: Write the remaining failing CLI tests**

The existing `test_main_pull_langfuse_missing_credential_returns_error` (lines 96–104, unmodified) already covers the missing-credential path and continues to pass unchanged under `_resolve_langfuse_credentials` — do not add a duplicate of it. Add these new test functions to `tests/test_cli.py`:

```python
def test_main_pull_langfuse_credential_flags_override_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--langfuse-public-key",
                "pk-cli",
                "--langfuse-secret-key",
                "sk-cli",
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["public_key"] == "pk-cli"
    assert mock_pull.call_args.kwargs["secret_key"] == "sk-cli"


def test_main_pull_langfuse_base_url_flag_takes_precedence_over_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n"
        "LANGFUSE_BASE_URL=https://env-host.example.com\n"
    )

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--base-url",
                "https://cli-host.example.com",
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["base_url"] == "https://cli-host.example.com"


def test_main_pull_langfuse_base_url_falls_back_to_langfuse_host_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n"
        "LANGFUSE_BASE_URL=https://env-host.example.com\n"
    )

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["base_url"] == "https://env-host.example.com"


def test_main_pull_langfuse_base_url_defaults_to_langfuse_cloud(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["base_url"] == "https://cloud.langfuse.com"


def test_main_pull_langfuse_until_defaults_to_command_start_time(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(["pull", "langfuse", "--env-file", str(env_file)])

    until_value = mock_pull.call_args.kwargs["until"]
    assert until_value is not None
    datetime.fromisoformat(until_value)


def test_main_pull_langfuse_repeatable_trace_name_and_tag(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--trace-name",
                "support-bot",
                "--trace-name",
                "billing-bot",
                "--tag",
                "prod",
                "--tag",
                "tier-1",
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["trace_names"] == ["support-bot", "billing-bot"]
    assert mock_pull.call_args.kwargs["tags"] == ["prod", "tier-1"]


def test_main_pull_langfuse_default_count_is_100(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse", return_value=(0, 0)
    ) as mock_pull:
        main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert mock_pull.call_args.kwargs["count"] == 100


def test_main_pull_langfuse_prints_imported_and_skipped_summary(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch("metergraphrelay.cli.pull_langfuse", return_value=(7, 2)):
        exit_code = main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "7" in captured.out
    assert "2" in captured.out


def test_main_pull_langfuse_api_error_returns_clean_exit_code(tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("LANGFUSE_PUBLIC_KEY=pk-1\nLANGFUSE_SECRET_KEY=sk-1\n")

    with patch(
        "metergraphrelay.cli.pull_langfuse",
        side_effect=LangfuseAPIError(
            "Langfuse API request failed: HTTP 400 Bad Request"
        ),
    ):
        exit_code = main(
            [
                "pull",
                "langfuse",
                "--env-file",
                str(env_file),
                "--until",
                "2026-08-07T00:00:00+00:00",
            ]
        )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: ")
    assert "400" in captured.err
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k pull_langfuse`
Expected: several FAIL (e.g. `AssertionError: expected call not found` for kwargs-shape mismatches, or pass trivially where behavior already happens to match — inspect each failure individually rather than assuming a blanket red).

- [ ] **Step 8: Fix any mismatches found in Step 7**

The implementation from Step 4 should already satisfy all of these tests (the CLI wiring was written to this exact contract). If any test fails, the mismatch is between the test's expectation and the Step 4 code — reconcile by matching Step 4's actual behavior (do not weaken a test to match a bug; fix the code in `cli.py` if the test correctly reflects a Global Constraint above).

- [ ] **Step 9: Run the full CLI test file to confirm everything passes and nothing regressed**

Run: `pytest tests/test_cli.py -v`
Expected: all tests pass, including the pre-existing `test_main_pull_openai_*`, `test_main_pull_anthropic_*`, `test_main_sync_openai_*`, and `test_main_push_*` tests (unmodified, must still be green).

- [ ] **Step 10: Commit**

```bash
git add src/metergraphrelay/cli.py tests/test_cli.py tests/conftest.py
git commit -m "feat(cli): wire up \`pull langfuse\` with selectors, credentials, and base-url resolution"
```

---

### Task 8: CLI `--help` text completeness

**Files:**
- Modify: `src/metergraphrelay/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `build_parser` (existing), the flags added in Task 7.

- [ ] **Step 1: Write the failing tests**

```python
def test_pull_langfuse_help_documents_every_flag_and_default(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["pull", "langfuse", "--help"])

    help_text = capsys.readouterr().out

    for expected in [
        "--count",
        "default: 100",
        "--since",
        "no lower bound",
        "--until",
        "captured once",
        "--trace-name",
        "OR'd together",
        "--tag",
        "ALL given tags",
        "--environment",
        "--route",
        "Not a selector",
        "--base-url",
        "LANGFUSE_BASE_URL",
        "--output",
        "./traces.jsonl",
        "--env-file",
        ".env",
        "--langfuse-public-key",
        "LANGFUSE_PUBLIC_KEY",
        "--langfuse-secret-key",
        "LANGFUSE_SECRET_KEY",
    ]:
        assert expected in help_text, f"missing {expected!r} in --help output"


def test_pull_help_lists_langfuse_subcommand(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["pull", "--help"])

    assert "langfuse" in capsys.readouterr().out
```

Add `import pytest` to `tests/test_cli.py` if not already present (check the file — it currently only imports `patch` and, after Task 7, `build_parser`/`main`/`LangfuseAPIError`/`datetime`; `pytest` is needed for `pytest.raises` in the tests above).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_cli.py -v -k "help_documents or help_lists_langfuse"`
Expected: FAIL — `AssertionError: missing '--count' in --help output` (no `help=` text has been added to any `pull_langfuse_parser` argument yet, so argparse's default help output won't contain these substrings).

- [ ] **Step 3: Add complete help text**

In `src/metergraphrelay/cli.py`, replace the `pull_langfuse_parser` block added in Task 7 with:

```python
    pull_langfuse_parser = pull_subparsers.add_parser(
        "langfuse",
        description=(
            "Pull Langfuse GENERATION observations (LLM call records) into a "
            "local JSONL file shaped for metergraph's ingest API. SPAN/EVENT "
            "observations and scores/evals are not imported. Requires Langfuse "
            "Cloud or self-hosted v4+ (the v2 Observations API). With no "
            "--trace-name/--tag/--environment/--since/--until given, imports "
            "the latest --count GENERATION observations overall. WARNING: "
            "generation input/output content is transferred from Langfuse "
            "into the local output file, and from there into metergraph via "
            "`push`, with no opt-in gate."
        ),
        help=(
            "Pull GENERATION call records from Langfuse (v2 Observations "
            "API, Cloud/self-hosted v4+); no evals/spans/events"
        ),
    )
    pull_langfuse_parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=100,
        help=(
            "Maximum number of GENERATION observations to import (never a "
            "count of distinct traces). (default: 100)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--since",
        default=None,
        help=(
            "Only import observations at or after this ISO 8601 timestamp "
            "(Langfuse fromStartTime, inclusive). (default: no lower bound)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--until",
        default=None,
        help=(
            "Only import observations before this ISO 8601 timestamp "
            "(Langfuse toStartTime, exclusive). (default: the time this "
            "command started running, captured once for the whole pull)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--trace-name",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "Only import generations whose trace name is NAME. Repeatable: "
            "multiple --trace-name values are OR'd together (any match). "
            "Combines with --tag/--environment/--since/--until using AND. "
            "(default: no filter, all trace names)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--tag",
        action="append",
        default=None,
        metavar="TAG",
        help=(
            "Only import generations whose trace has tag TAG. Repeatable: "
            "multiple --tag values require ALL given tags to be present "
            "(AND). Combines with --trace-name/--environment/--since/--until "
            "using AND. Only matches tags that already exist on the data; "
            'omitting --tag means no tag filter, not "untagged only". '
            "(default: no filter, all tags)"
        ),
    )
    pull_langfuse_parser.add_argument(
        "--environment",
        default=None,
        help="Filter to a single Langfuse environment value. (default: no filter, all environments)",
    )
    pull_langfuse_parser.add_argument(
        "--route",
        default=None,
        help=(
            "Override the metergraph route field for every imported row. "
            "(default: the Langfuse trace name, or the generation's own "
            "name if the trace has none) Not a selector — see --trace-name "
            "for filtering which generations are pulled."
        ),
    )
    pull_langfuse_parser.add_argument(
        "--base-url",
        default=None,
        help="Langfuse API base URL. (default: $LANGFUSE_BASE_URL if set, else Langfuse Cloud)",
    )
    pull_langfuse_parser.add_argument(
        "--output",
        default="./traces.jsonl",
        help="Path to write the resulting JSONL file. (default: ./traces.jsonl)",
    )
    pull_langfuse_parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to a .env file to load credentials from. (default: .env)",
    )
    pull_langfuse_parser.add_argument(
        "--langfuse-public-key",
        default=None,
        metavar="KEY",
        help=(
            "Langfuse public key (Basic Auth username). Overrides "
            "$LANGFUSE_PUBLIC_KEY / .env if given; env/.env is the preferred path."
        ),
    )
    pull_langfuse_parser.add_argument(
        "--langfuse-secret-key",
        default=None,
        metavar="KEY",
        help=(
            "Langfuse secret key (Basic Auth password). Overrides "
            "$LANGFUSE_SECRET_KEY / .env if given; env/.env is the preferred path."
        ),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_cli.py -v -k "help_documents or help_lists_langfuse"`
Expected: 2 passed.

- [ ] **Step 5: Run the full test suite to confirm no regressions**

Run: `pytest -v`
Expected: all tests pass (this exercises every file touched in Tasks 1–8 together for the first time).

- [ ] **Step 6: Commit**

```bash
git add src/metergraphrelay/cli.py tests/test_cli.py
git commit -m "docs(cli): add complete --help text for every pull langfuse option"
```

---

### Task 9: README, `.env.example`, and doc-consistency test

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `build_parser` (existing/Task 7/8).

- [ ] **Step 1: Add the README section**

In `README.md`, insert a new `## Pull from Langfuse` section immediately after the existing `## Trace record shape` section (i.e. right before `## Development`, which currently starts at line 114):

```markdown
## Pull from Langfuse

Import Langfuse **GENERATION** observations (the LLM call records
Langfuse captures) into the same metergraph-native JSONL shape as
`pull openai`. Only `GENERATION` observations are imported — Langfuse
`SPAN`/`EVENT` observations and Scores/evals are never imported.
Requires Langfuse Cloud or **self-hosted v4+** (the version serving the
v2 Observations API); older self-hosted deployments are not supported.

**Setup:** add your Langfuse keys to `.env`:

    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...

By default this talks to Langfuse Cloud. For a self-hosted instance,
set `LANGFUSE_BASE_URL` in `.env` (or pass `--base-url` per-command):

    LANGFUSE_BASE_URL=https://your-langfuse-instance.example.com

**Quickstart:**

    metergraphrelay pull langfuse -n 25 --output traces.jsonl
    metergraphrelay push traces.jsonl

With no other flags, this imports the latest 100 `GENERATION`
observations overall (not 100 distinct traces).

**Narrowing what gets pulled**, beyond `-n`/`--count`:

    metergraphrelay pull langfuse --since 2026-08-01T00:00:00Z --until 2026-08-07T00:00:00Z
    metergraphrelay pull langfuse --trace-name support-bot-reply --trace-name billing-bot-reply --tag prod --tag tier-1

- `--trace-name` matches Langfuse's trace name — the closest Langfuse
  concept to a workflow or use case (e.g. `"support-bot-reply"`). It's
  repeatable; multiple `--trace-name` values are **OR'd** together (any
  match).
- `--tag` matches Langfuse trace tags — commonly used as customer-defined
  categories (a tenant, an experiment cohort, a priority tier); this is
  a convention, not something Langfuse enforces. It's repeatable;
  multiple `--tag` values require **all** of them to be present (AND).
  `--tag` only matches tags that already exist on your historical
  data — it can't require a tag that was never set, and if you don't
  pass `--tag` at all, there's no tag-based narrowing (not "untagged
  only").
- `--trace-name`, `--tag`, `--environment`, and `--since`/`--until` all
  combine with each other using AND.
- `--count` is always a cap on the number of **GENERATION observations**
  imported, never a count of distinct traces.

**Before running this against your own data:** `pull langfuse`
transfers every matched generation's prompt/response content from
Langfuse into your local JSONL file, and from there into metergraph via
`push`, with no separate opt-in step — unlike `pull openai`'s
`--include-content` flag, there is no way to pull Langfuse generations
without their content.

Full flag reference: `metergraphrelay pull langfuse --help`.
```

- [ ] **Step 2: Update `.env.example`**

`.env.example` currently reads:

```
OPENAI_API_KEY=sk-your-key-here
ANTHROPIC_API_KEY=sk-ant-your-key-here
LANGFUSE_PUBLIC_KEY=pk-lf-your-key-here
LANGFUSE_SECRET_KEY=sk-lf-your-key-here
METERGRAPH_APP_TOKEN=your-metergraph-token-here
```

Change to:

```
OPENAI_API_KEY=sk-your-key-here
ANTHROPIC_API_KEY=sk-ant-your-key-here
LANGFUSE_PUBLIC_KEY=pk-lf-your-key-here
LANGFUSE_SECRET_KEY=sk-lf-your-key-here
# LANGFUSE_BASE_URL=https://cloud.langfuse.com   # optional; only needed for self-hosted Langfuse
METERGRAPH_APP_TOKEN=your-metergraph-token-here
```

- [ ] **Step 3: Write the failing doc-consistency test**

```python
from pathlib import Path


def test_readme_pull_langfuse_examples_parse_successfully():
    readme_text = (Path(__file__).parent.parent / "README.md").read_text()

    assert "metergraphrelay pull langfuse -n 25 --output traces.jsonl" in readme_text
    assert (
        "metergraphrelay pull langfuse --since 2026-08-01T00:00:00Z "
        "--until 2026-08-07T00:00:00Z"
    ) in readme_text
    assert (
        "metergraphrelay pull langfuse --trace-name support-bot-reply "
        "--trace-name billing-bot-reply --tag prod --tag tier-1"
    ) in readme_text

    build_parser().parse_args(
        ["pull", "langfuse", "-n", "25", "--output", "traces.jsonl"]
    )
    build_parser().parse_args(
        [
            "pull",
            "langfuse",
            "--since",
            "2026-08-01T00:00:00Z",
            "--until",
            "2026-08-07T00:00:00Z",
        ]
    )
    build_parser().parse_args(
        [
            "pull",
            "langfuse",
            "--trace-name",
            "support-bot-reply",
            "--trace-name",
            "billing-bot-reply",
            "--tag",
            "prod",
            "--tag",
            "tier-1",
        ]
    )
```

This test should be written *before* Step 1/2 are applied, per TDD — but since Step 1/2 are non-code documentation edits with no separate "implementation" phase, the practical sequencing here is: write this test first, run it (it fails because the README doesn't yet contain the exact strings), then apply Steps 1–2, then rerun. Do it in that order now: if you already applied Steps 1–2 above before writing this test, temporarily revert `README.md`'s new section, run the test to confirm it fails, then reapply Steps 1–2.

- [ ] **Step 4: Run the test to verify it fails (or passes if Steps 1–2 were already applied — confirm by temporarily reverting)**

Run: `pytest tests/test_cli.py -v -k readme_pull_langfuse_examples`
Expected: FAIL with `AssertionError` if `README.md` doesn't yet contain the exact example strings.

- [ ] **Step 5: Apply Steps 1–2 (if not already applied) and rerun**

Run: `pytest tests/test_cli.py -v -k readme_pull_langfuse_examples`
Expected: 1 passed.

- [ ] **Step 6: Run the full test suite to confirm no regressions**

Run: `pytest -v`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add README.md .env.example tests/test_cli.py
git commit -m "docs: add Langfuse pull section to README and LANGFUSE_BASE_URL to .env.example"
```

---

### Task 10: Final full verification

**Files:** none (verification only).

- [ ] **Step 1: Run the complete test suite**

Run: `pytest -v`
Expected: every test across `tests/providers/test_langfuse.py`, `tests/test_cli.py`, `tests/test_config.py`, `tests/test_demo.py`, `tests/test_push.py`, and `tests/providers/test_openai.py` passes, with zero failures and zero errors.

- [ ] **Step 2: Confirm no lint/type-check tool is configured (so none is skipped by mistake)**

Run: `grep -i "ruff\|flake8\|mypy\|black\|pylint" pyproject.toml`
Expected: no output — this repo has no lint/type-check tool configured (`[project.optional-dependencies] dev = ["pytest>=8.0"]` is the entire dev toolchain), so there is no separate lint step to run here; this step exists to make that explicit rather than silently skip a step a reviewer might expect.

- [ ] **Step 3: Manually inspect `--help` output**

Run: `python -m metergraphrelay.cli pull langfuse --help`
Expected: the full help text from Task 8 renders correctly — every flag, default, and env-var relationship is visible and readable (not just substring-matched by the automated test).

Run: `python -m metergraphrelay.cli pull --help`
Expected: the subcommand listing shows `openai`, `anthropic`, and `langfuse`, each with its one-line `help=` description.

- [ ] **Step 4: Confirm the package still builds cleanly**

Run: `python -m build`
Expected: builds successfully with no errors; the new `src/metergraphrelay/providers/langfuse.py` module is included in the wheel (consistent with `[tool.setuptools.packages.find] where = ["src"]` already covering everything under `src/metergraphrelay/`).

Run: `rm -rf dist build`
(clean up build artifacts — both directories are already gitignored, this just keeps the working tree tidy, consistent with the existing hygiene spec's own convention.)

- [ ] **Step 5: Confirm pre-existing untracked files are untouched**

Run: `git status --short`
Expected: `metergraphrelay.cdx.json` and `requirements.txt` (both pre-existing, untracked, unrelated to this feature) still show as untracked (`??`) with no modifications; no other unexpected untracked/modified files appear beyond what Tasks 1–9 committed.

- [ ] **Step 6: Confirm the acceptance criteria from the design spec are met**

Cross-check against `docs/superpowers/specs/2026-08-07-langfuse-trace-import-design.md`'s "Acceptance criteria / implementation handoff" section:
- `pull langfuse` implemented per Architecture/CLI/Targeting & filtering/Mapping/Failure semantics — done (Tasks 1–7).
- Default behavior with no selectors verified unchanged (latest `--count` overall, not distinct traces) — done (`test_pull_langfuse_single_page_under_count`, `test_main_pull_langfuse_default_count_is_100`).
- Tests exist per the Testing section, including documentation-specific and targeting-specific items — done (Tasks 1–9).
- README has the required `pull langfuse` section — done (Task 9).
- `--help` text is complete — done (Task 8).
- `.env.example` addresses `LANGFUSE_BASE_URL` — done (Task 9).

No further action needed for this plan; version bump and any PyPI republish are separate follow-up chores outside this plan's scope, per this repo's existing convention of committing version bumps separately from feature work.
