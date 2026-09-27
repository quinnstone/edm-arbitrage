# Ticket arbitrage scanner

CrowdVolt discovery unions recursive sitemaps, homepage event links, and the public catalog paginated separately for each city ID exposed by the homepage. `data/catalog_coverage.json` records discovery completeness and each fetched event's status. Main and seated scans route the same discovered catalog by venue/ticket metadata rather than URL keywords.

## Product comparisons

CrowdVolt ticket-type IDs and metadata are retained. Main forward and reverse comparisons require a listing-level admission label, positive all-in price, and known lot quantity. Ferry passes, parking, shuttles and other add-ons are excluded from admission prices. Distinct days, pass durations, premium tiers, entry restrictions and sections are not collapsed into event-wide GA prices. Unknown source tiers do not generate verified alerts.

TickPick attempts to read its browser listing response. StubHub accepts explicit section, quantity and all-in price from JSON responses tied to the event request. Gametime uses its listing-level section data. Aggregate-only SeatGeek, VividSeats and Resident Advisor results currently lack sufficient metadata for verified main-scan alerts. Public scraper access is not guaranteed: the September 20 live checks timed out on TickPick and encountered a StubHub challenge. These adapters must not be described as universally reliable or as an authenticated inventory integration.

Matching is deliberately conservative: unknown notes and differing labels can reject valid upgrades; unknown split rules only support a full source lot. Known splits can be compared at their supported quantities. Gametime is restricted to the quantity for which its returned price was quoted. CrowdVolt bids and reverse sourcing require a matching lot quantity. Summary-derived bid payouts are excluded when estimated. Missing event date, venue or city prevents verified main-scan comparisons. Alerts identify the product and quantity; prices and profit are per ticket. This is metadata validation, not a checkout, transfer-eligibility check or reservation of either side.

`data/comparison_coverage.json` records main-scan outcomes, including `verified_products`, `tier_metadata_unavailable`, `identity_unverified`, `not_configured`, `timeout`, `error`, and `scan_budget_exhausted`. `no_results_or_fetch_failure` does not mean verified absence of supply. Aggregate-only candidates remain in diagnostic logs with no verified profits. The opportunity log's legacy `alert` field denotes a speculative candidate, not confirmed notification delivery.

Main provider searches run in separate processes with deadlines; on macOS/Linux, timeout kills the process group, including browser children. The comparison stage has a 30-minute budget, with skipped comparisons recorded explicitly. Auxiliary scans retain their existing timeout behavior.

## Reverse opportunities

Only **TickPick and StubHub** are reverse destinations. Sellers-only CrowdVolt events are included. Other providers are queried only for eligible forward events. DICE remains reverse-only in the main scanner, now including StubHub. Existing seated reverse scans remain disabled.

Reverse comparisons use the cheapest equivalent destination product/lot, not its most expensive listing. The existing $15 minimum estimated profit and three-ask cushion remain; the three asks must now match the product and lot. The former minimum of two CrowdVolt bids is removed. Destination sale prices remain speculative and fees may be estimated. `REVERSE_PLATFORMS`, `SPEC_MIN_MATCHING_ASKS`, and `EVENT_SCAN_WORKERS` are in `config.py`.

The separate primary-ticket, social-signal and position-monitor modules retain their existing policies; this change does not establish listing-level verification for all of those auxiliary feeds.

## Validation

Run the offline regression suite without network calls or alerts:

```sh
python3 -B -m unittest test_catalog_products test_pipeline_review -v
```

The older `test_matching.py` also contains live tests; do not run it wholesale as an offline test. Its known artist-name negative-case failures are documented in `AUDIT.md` and are outside this change.

`python3 -B main.py --dry` performs real comparisons and writes diagnostic logs, but sends no notifications and skips auxiliary scans and position/history updates. `scripts/review_catalog.py` and `scripts/review_smoke.py` perform live read-only checks with application output isolated in temporary directories. See [REVIEW_VALIDATION.md](REVIEW_VALIDATION.md) for tested behavior and remaining coverage limits.
