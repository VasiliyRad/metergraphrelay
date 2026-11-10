# Portkey Anthropic batch results

Portkey exports include batch creation, status polling, and results download
operations. These are transport operations, not individual model calls.

`convert_portkey_export` excludes Anthropic batch management records and expands
results into individual outcomes. It accepts a single result object, a list of
results, or JSONL result text. Successful results use `result.message.model`,
native input/output and cache usage, `batch=True`, and endpoint `messages`.
Download-level cost and latency are not copied to the completion. Metergraph
can price its native usage with the catalog's batch rates.

The additive `normalize_portkey_rows(row, *, import_context=None,
batch_requests=None)` API returns a list. Ordinary rows produce one record and
keep the existing `normalize_portkey_row` behavior. Management operations and
unsuccessful results fetches produce none. Invalid results in a successful 2xx
response raise `PortkeyConversionError` so a recurring
import cannot complete a window after silently discarding results.

Successful results use `anthropic-batch:<message.id>` as their request and import
event ID. Repeated downloads therefore deduplicate even when Portkey assigns
the downloads different IDs. Failed, canceled and expired results carry explicit
error status; their identity uses the provider batch ID and custom ID when a
`batch_id` is available on the export row, otherwise the export ID and custom ID.
The latter only guarantees deduplication of the same export record.

`batch_requests` optionally maps custom IDs to request parameters for this
specific batch. Without it, successful results retain response content and
usage, but no request is invented. Custom IDs are only unique within a batch,
so the converter never correlates arbitrary creation and result rows by custom
ID across an export. A caller performing a historical repair must first prove
which creation record belongs to the result batch.
