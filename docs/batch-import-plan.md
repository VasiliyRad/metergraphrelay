# Portkey batch import implementation plan

Goal: convert Anthropic batch results to real inference records and repair existing
hosted records through the standard pricing and analytics paths.

Spec: batch-import-design.md. Existing Python converter and hosted operator tools
remain the architecture. Public artifacts use synthetic data only.

1. Write regression tests for lifecycle exclusion, nested successful results,
   native usage/cache counts, stable identity and ignored envelope cost/latency.
   Run them and confirm failures against 0.7.5.
2. Implement additive `normalize_portkey_rows` and a focused batch helper module;
   ordinary rows delegate to `normalize_portkey_row`. Export conversion expands
   the returned list. Add a second-pass request index only where the export
   contains creation records; never require a creation row to extract success.
3. Cover lists/JSONL, missing request, error/canceled/expired outcomes, mixed
   models, malformed recognized batches and duplicate downloads; run the relay
   suite. Document the additive API and bump the patch release.
4. Internal implementation is delegated through the internal repository's
   prescribed Claude bridge: integrate plural conversion, add guarded repair
   preview/apply, pricing and analytics resync, local tests and metrics.
5. Independently review both changes. Release the relay, update hosted lock,
   validate and deploy the importer using existing repository workflows.
6. Preview the exact production repair, check actual model/token/cache counts
   and catalog batch cost, retain recovery evidence, apply and verify both
   source records and served analytics. A second preview must be a no-op.

Review focus: download-level cost leakage; duplicate message identity across
downloads; corrupted JSONL causing silent loss; missing requests falsely marked
captured; historical request correlation across mixed-model result ordering.
