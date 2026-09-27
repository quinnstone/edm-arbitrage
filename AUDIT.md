# CrowdVolt coverage and arbitrage audit — September 20, 2026

## Conclusion

The project scans a substantial portion of CrowdVolt, but does not discover the entire live catalog or exhaustively identify executable arbitrage. The largest issues are sitemap-only discovery, excluded event populations, ticket-product mismatches, unavailable source data, and stale prices at alert time. A successful GitHub Actions run is not evidence of complete market coverage.

This was an audit, not a trading run: application code was not changed and no alerts or transactions were initiated. Read-only CrowdVolt requests, existing production logs, and the three offline matching test functions were used. No exhaustive third-party crawl or checkout validation was performed, so the number or dollar value of missed executable opportunities remains unknown.

## How the project works

- `crowdvolt.py` discovers sitemap URLs and fetches pages with 20 workers. It uses embedded books, then the book API, then a one-level-per-type summary. Only events with bids or asks are returned.
- `main.py` removes past and selected seated events, searches six providers for events with non-premium bids, and sends forward-arbitrage alerts after the entire search loop. DICE events are excluded from forward alerts and StubHub searches.
- `matcher.py` compares artist, date, city, and venue, then compares an event-wide source minimum against an event-wide CrowdVolt bid/ask.
- `undercut.py` estimates resale-listing spreads, tracks books and listing persistence, and sends scheduled speculative digests. These are prospective resale spreads, not committed buyers on the destination marketplace.
- `seated_scanner.py` handles selected venues using TickPick section data. Its reverse direction is implemented but disabled in the active scan.
- `skydeck_scanner.py` compares Skydeck bids with Tao primary availability by product family. `ticket_radar.py` checks Eventbrite/DICE availability changes and codes. `groupme_scanner.py` produces a demand/supply digest and invokes the Reddit scanner. `position_monitor.py` monitors recorded resale positions.
- Four GitHub Actions workflows schedule the main scan, seated scan, radar, and GroupMe/Reddit scan. README documentation is effectively absent.

## Live coverage measurements

Snapshot from a fresh local CrowdVolt crawl; market counts can change between requests:

| Stage | Events |
|---|---:|
| Sitemap URLs | 456 |
| Returned with market data | 255 |
| Embedded full books | 200 |
| API books | 32 |
| Summary-only books | 23 |
| Removed by main seated-venue filter | 4 |
| Remaining main-scan population | 251 |
| Enter third-party comparison | 78 |
| Sellers-only events skipped by comparison | 173 |
| Compared DICE events excluded from forward alerts | 49 |
| Non-DICE events eligible for main forward alerts | 29 |

The 201 sitemap URLs not returned are not all proven failures: empty markets are intentionally omitted. This output does not preserve enough per-event status to certify why each was excluded.

The production main run https://github.com/quinnstone/edm-arbitrage/actions/runs/35533269201 closely reproduced these counts: 456 URLs, 255 active events, 78 searched, 23 without any cross-platform match, five qualifying source comparisons grouped into two forward alert events, and 14 speculative candidates. Its book-source split was 200 embedded / 35 API / 20 summary. Scan start was 19:44:48 UTC; forward filtering occurred at 20:03:17 UTC.

## Findings, ordered by impact

### 1. Ticket-product mismatches can create invalid profit signals

`crowdvolt.py:389` excludes a few premium keywords, then reduces all remaining ticket types to one minimum ask and maximum bid. `matcher.py:490` onward compares these with event-wide source prices. It does not establish equivalent admission, day/pass duration, entry cutoff, section, quantity, split rules, or delivery constraints.

Live example: fetching `experts-only-new-york-2026-randalls-island-park` returned six ferry-pass listings starting at $47 all-in and a GA Sunday listing at $368. The parser set `min_ask=47`. Ferry access is therefore treated as the admission sourcing cost. That event currently has no bids and is outside the main loop; this is a demonstrated parser defect, not a claim that a false alert was sent for it.

Portola similarly has a $347 two-day bid alongside $96 single-day bids. Source minimums must be matched to the particular pass, rather than to $347 indiscriminately. Live books also contain before-midnight, anytime-entry, after-7AM, floor, and day-specific products.

Fix: preserve product-specific bid/ask books and source listings; require explicit fulfillment compatibility and attainable quantity. Treat unknown compatibility as unverified. Do not discard all premium inventory if the intended scope is all opportunities.

### 2. Sitemap-only discovery misses live website markets

`crowdvolt.py:74` reads only event URLs from one sitemap. It does not reconcile website results or follow sitemap indexes.

The homepage linked two event URLs absent from the contemporaneous sitemap: `brunello-mellow-circus-new-york-sept-20-the-ruins` and `experts-only-new-york-2026-randalls-island-park`. Both fetched successfully with asks. Both had already started that afternoon, so the mismatch may reflect sitemap expiry rather than newly listed events; it still proves the sitemap is not identical to all markets the live site exposes. Whether to include in-progress markets should be an explicit policy.

Fix: reconcile the sitemap with the site's public catalog across cities and pagination, retain discovered events until an explicit eligibility/expiry rule applies, and record discovery/fetch/book status for every event. The homepage check was not an exhaustive catalog enumeration.

### 3. Sellers-only events never receive reverse-spread evaluation

`main.py:118` restricts every provider search to events with a non-premium maximum bid. `main.py:366` passes only that population into speculative analysis. Thus all 173 sellers-only events in the snapshot are ignored for reverse spreads.

`undercut.py:479` additionally requires two bid rows and three ask rows; only 43 main-loop events in this snapshot meet those depth gates and have a minimum ask. These are deliberate demand/supply heuristics, not necessary conditions for a resale spread. Rows can also represent different products or the same participant, so they do not establish independent executable demand and substitute supply.

Fix: discover forward and reverse candidates separately, then apply configurable execution/risk policies with visible exclusion reasons. A reverse spread remains speculative until a sale occurs.

### 4. The seated handoff demonstrably drops two Fisher events

The main scanner removes Forest Hills by venue metadata. The seated scanner discovers URLs using `forest-hills`, but the live Fisher URLs contain `forest-hill`:

- `fisher-fri-oct-16-forest-hill-new-york`: $62 maximum bid in the snapshot.
- `fisher-sat-oct-17-forest-hill-new-york`: $80 maximum bid.

Both are excluded by the main scanner and missed by seated discovery (`seated_scanner.py:740`). This proves unsearched demand, not that a profitable source listing exists.

The latest inspected seated run also received HTTP 403 for Sofi Tukker's TickPick listing requests and obtained no intercepted listing data: https://github.com/quinnstone/edm-arbitrage/actions/runs/35526420872.

Fix: route a shared catalog using venue/product metadata, not slug guesses, and expose blocked section-data requests as degraded coverage.

### 5. Production source availability is materially incomplete

The inspected main log repeatedly reports `SeatGeek: No API key configured`. This provider is not participating. VividSeats reported 18 unresolved challenge attempts, although it also supplied matches; it is partially working, not entirely unavailable. StubHub had repeated price-fetch failures. Provider failures commonly return empty lists and become indistinguishable from genuine no-match outcomes.

The inspected radar run found 14 eligible DICE events but resolved zero primary URLs; Reddit returned 403 and no global candidate codes were obtained: https://github.com/quinnstone/edm-arbitrage/actions/runs/35527872302. The workflow still reported success.

Radar additionally covers only Eventbrite/DICE within 14 days and primarily alerts on state transitions or validated codes. First-seen or unchanged profitable ordinary availability is not a general alert condition. AXS, Ticketmaster, TIXR, POSH, Shotgun, and other primary platforms lack comprehensive direct availability comparison.

Fix: restore missing provider configuration, use durable event-ID/primary-URL mappings, and distinguish success-empty, blocked, timeout, parse failure, and not-configured outcomes. Make provider health visible in summaries.

### 6. Alert latency and freshness undermine execution

All CrowdVolt books are captured before a sequential comparison loop. Alerts are sent only after that loop finishes, without refreshing both sides. In the inspected production run, forward filtering occurred about 18.5 minutes after scan start. Observed main-run start gaps in the recent run list included roughly 28 minutes, 94 minutes, and 167 minutes; configured cron frequency is not achieved coverage frequency.

Fix: use bounded parallel work, cached event mappings, progressive alerts, and immediate price/quantity revalidation before marking a candidate actionable. Track actual event revisit age.

### 7. Existing matching checks fail and missing metadata is permissive

The three offline test functions reported 34/38 cases passing. Three negative artist-name tests incorrectly pass the matcher threshold: Adam Beyer / Adam Ten, Baby Jane / Baby J & Belters Only, and Two Friends / Three Friends. The fourth failure is an Ultra name-extraction expectation. This does not prove all three name errors became production alerts; date/city/venue gates can still reject them.

Missing dates, cities, or venues allow matches through. Provider searches are also bounded: SeatGeek requests one page of 25, StubHub prices three candidates, and query loops stop at the first accepted result.

Fix: assert negative matching cases, require adequate event identity for actionable alerts, and separate candidate search from authoritative event mapping. Test pagination and same-artist/multiple-show cases.

### 8. Summary fallbacks, fees, and state handling need explicit uncertainty

Summary books lose real depth and estimate some bid payouts using an event-wide ratio. The same demand/supply gates then operate on synthesized rows. Exact books and summary books should not be treated as equally complete.

Buyer fees and reverse seller payouts use configured estimates; exact checkout and account-specific payouts were not verified in this audit. Estimated costs should not be represented as guaranteed executable profit.

Main, radar, and GroupMe workflows share the `tracking-` cache namespace and snapshot the entire data directory, while only main has workflow concurrency protection. Overlapping workflows can restore stale state and later overwrite each other's saved state snapshots. This can affect deduplication and histories.

## Recommended implementation order

1. Product-compatible pricing and executable quantity checks; reject add-ons as admission.
2. Unified catalog discovery and seated routing, with an event-by-event coverage ledger.
3. Separate forward and reverse candidate populations; make intentional exclusions configurable and visible.
4. Restore provider health and primary URL resolution; stop reporting degraded coverage as zero opportunity.
5. Reduce scan latency and refresh both sides before alerts.
6. Add focused regression tests and isolate workflow state storage.

Until these are addressed, treat output as a useful, incomplete opportunity feed requiring product and price verification.
