# First-wave reference names, checked 2026-11-02

`eec-heading-names-2026-11-02.json` stores names of 96 groups and 1228 headings from the current EEC index and its linked official PDFs. Each group includes the URL/SHA-256, and each heading its PDF page. This is platform-owned reference data; websites receive `reference_catalog.py --out ...`, rather than maintaining a separate taxonomy.

`parse_tnved.merge_sources` adds missing current 4-digit ancestors before building paths. `reference_catalog.py --repair-db` applies the same nine additions atomically to an existing SQLite database, reparents affected descendants, and fills a missing heading segment in their full paths. It is idempotent. Existing leaf descriptions, rates and eligibility flags remain intact; the tariff date remains 29.09.2026. `reference_names_as_of` describes only the names supplement.

Export: `python reference_catalog.py --out exports/reference-wave1.json` after the repair. Output contains the 96 groups / 1228 headings plus up to five example leaves per heading. Examples must appear both in the platform's active set and in the official tables. No rates are exported.

Verification: `python -m unittest discover -s tests -p test_reference_catalog.py`. The reference collection normalized line wraps, continued titles across page boundaries and preserved internal colons. All nine previously reported names agree with the earlier manual EEC transcription.

Separate finding outside this release's group/heading scope: platform's active leaf 8112924100 is absent from the current EEC table; it contains 8112924101 and 8112924109 instead. The exporter excludes 8112924100 from examples. This release does not rebuild the classifier/vector index or claim the entire platform tariff is current.
