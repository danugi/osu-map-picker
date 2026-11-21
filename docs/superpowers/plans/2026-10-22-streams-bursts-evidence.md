# Streams and Bursts: independent .osu sequence evidence

Status: implemented. Full suite: **301 passed**.

## Goal

Replace the density/speed proxies for `streams` and `bursts` with measurable
sequence evidence from the selected difficulty's public `.osu` file. A stream
is a continuous compact tap chain of at least 10 notes. A burst is a separate
compact tap chain of 5–9 notes. A long chain never counts as a burst, and a
short chain never counts as a stream.

## Non-negotiable behaviour

- Strict filters -> optional difficulty attributes -> `.osu` sequence analysis
  -> mandatory target gate -> ranking.
- `Streams & Stamina` targets only stream skills. `Bursts` targets only burst
  skills. Selecting one cannot activate the other.
- Missing `.osu` data stays `недостаточно данных`; no proxy, mapper tag, BPM,
  popularity or title word can pass a stream/burst gate.
- Mapper tags remain low-confidence hints and are never sequence evidence.
- `.osu` downloads are bounded by a visible per-search limit (default 20) and
  are cached next to the SQLite database.
- Local re-ranking uses only already cached `.osu` files; it never downloads.

## Tasks

1. Add `SequenceAnalysis`/`PatternChain` models, beat-length data to parsed
   hit objects, and a cached-only preview loader.
2. Write failing tests for the pure analyzer:
   - 10+ compact 1/4 notes -> stream only;
   - 5–9 compact 1/4 notes -> burst only;
   - a map with both -> both, with no overlap;
   - wide jumps -> neither;
   - 1/2 notes -> neither;
   - inconsistent gaps split chains.
3. Implement `patterns.py` with conservative thresholds and normalized
   stream/burst features.
4. Remove the density/speed `streams`/`bursts` proxies. Add sequence features
   with source `osu_sequence` and confidence `high` only when no proxy/tag
   feature is mixed in.
5. Rewrite stream and burst skillsets around separate sequence features; add
   `burst_control`; keep combo skills but never use a combo as a pure target.
6. Add the `Bursts` built-in preset and v9 migration. Update preset target
   lists so Aim Farm, Speed, Flow Aim, Rhythm Control and Stamina do not
   require sequence evidence.
7. Enrich discovery candidates after attributes and before the gate. Inject a
   loader for tests; default uses the public `.osu` endpoint and cache. Add
   `fetch_sequences_limit` to discovery settings and the search UI.
8. Update UI evidence text, catalog grouping, tests and README provenance.
9. Run the full suite and verify a stream-only map passes only Streams, a
   burst-only map passes only Bursts, and a map with both can pass either.

## Verification

- `pytest -q`
- Explicit analyzer tests for compact/wide and 1/4/1/2 timing.
- Discovery test with a stub sequence loader and no network.
- App test that preview stays inside the selected card (already added).
