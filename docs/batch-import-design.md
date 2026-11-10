# Portkey Anthropic batch import correction

The approved correction separates batch lifecycle operations from individual
inferences. The relay owns provider conversion; the hosted application owns
durable identity, catalog pricing, record repair, and analytics synchronization.

## Approaches

Filtering model dropdown labels leaves incorrect call counts and lost usage.
Renaming lifecycle records to a model also invents inferences and cannot handle
mixed-model batches. Normalize the actual result envelopes instead, excluding
management operations and preserving per-result model and native usage.

## Conversion contract

Add `normalize_portkey_rows(row, *, import_context=None, batch_requests=None)`
returning a list. Ordinary records retain existing conversion. Verified Anthropic
batch management returns an empty list. Result envelopes (one object, a list, or
JSONL) return individual outcomes. Successes require a native message with a model,
usage, and message ID. Stable event identity uses that message ID, independent of
the Portkey download ID. Failed/canceled/expired outcomes retain explicit error
status and use batch/custom-ID identity when available, never an endpoint as model.
Malformed recognized result data fails the import rather than becoming an empty
successful call. Every batch row sets `batch=True`, `endpoint=messages`, omits the
download's latency and cost, and preserves native cache accounting. Requests are
correlated by custom ID where available; missing requests remain uncaptured.

The existing singular converter retains its ordinary-row return type. Export
conversion uses the plural entry point. A result's download-level cost must never
override catalog batch pricing or be multiplied by result count.

## Repair

The hosted command previews changes by exact workspace and bounded batch selection,
checks shapes and expected counts, preserves recoverability, reprices via the same
worker/catalog path, repairs identities, and queues and drains changed analytics
hours. It must refuse ambiguous or incomplete data and be idempotent on retry.
Repair may use matching creation requests already stored privately; public fixtures
and source contain synthetic data only. The hosted repair must preserve its
original records transactionally under tenant isolation and tenant-purge rules.

## Validation

Test ordinary-message parity, lifecycle exclusion, nested message/model/usage/cache
extraction, multi-result JSONL, repeated downloads, malformed results, error outcomes,
mixed models, and absence of envelope-level costs/latency. Validate hosted conversion
and repair with local integration metrics, then compare production dry-run before
and after applying the authorized repair.
