# Implementation review — September 20, 2026

## Conclusion

The internal comparison and notification plumbing passes the targeted regression suite. CrowdVolt metadata was available for every active event in a full live discovery check. This does **not** establish complete arbitrage coverage: TickPick returned no matched live inventory and StubHub event pages hit CAPTCHA during the live smoke test. Successful reverse-market inventory ingestion remains unproven.

## Evidence

| Check | Result | Scope |
| --- | --- | --- |
| Offline regression suite | 46 tests pass | Parsing, product/quantity comparisons, routing, logging, mocked notification delivery, worker serialization, failures and deadlines |
| Older offline matching cases | 34/38 pass | Four existing name-matching failures remain: Ultra extraction, Adam Beyer/Adam Ten, Baby Jane/Baby J & Belters Only, Two Friends/Three Friends |
| Full live CrowdVolt discovery | 453 URLs: 253 active, 200 empty | 451 sitemap URLs; 443 catalog entries across all five exposed cities; homepage union added coverage; no discovery/fetch/parse failures |
| Live CrowdVolt metadata | 253/253 active events | All had ticket-type metadata and embedded full books |
| Seated routing | 10 events | Includes both Fisher Forest Hills dates whose slugs use `forest-hill` |
| Live two-event main dry run | Completed without notifications | Portola plus DICE Fisher Factory Town; isolated temporary application state |
| Gametime live comparison | Verified product comparisons | Portola two-day and Saturday GA at the quoted quantity of two; no profitable forward opportunity in the sample |
| DICE provider selection | TickPick and StubHub only | Confirmed in both mocked pipeline and live smoke run |
| Static checks | Python syntax and diff whitespace checked | No deployment or commit performed |

The tests exercise actual comparison and logging functions with mocked marketplace responses and notification transports. Separate subprocess tests verify populated result serialization and timeout cleanup, including a child process holding stdout open. Live checks did not purchase, reserve, list, or send any messages. Catalog counts are a point-in-time observation, not a fixed expected total.

## Defects found and corrected during review

- Pagination without a city silently defaulted to NYC. Discovery now enumerates exposed city IDs and records partial failures.
- Embedded JSON could span stream chunks or exceed a fixed extraction window. Parsing now handles complete decoded objects; an empty authoritative book cannot resurrect stale summary inventory.
- Blocked, pending, reserved and malformed inventory rows could contaminate availability. Usable quantities and verified prices now control comparisons.
- Day labels, entry restrictions, rows and notes could lose meaning. Explicit ticket-day metadata takes priority, restrictions are preserved, and seated comparisons also reject unknown restrictions.
- Lot size and price quantity could differ. Supported splits are retained; Gametime comparisons use only its quoted lot quantity. Nonfinite prices, invalid quantities and explicit non-USD prices are rejected.
- Several listings from one seller could masquerade as independent reverse supply. Known seller/listing identifiers now control depth counts.
- Timeout wrappers could stop waiting while scrapers continued. Main provider searches now use killable processes and an overall comparison budget.
- A later provider failure could discard earlier results. Failures are isolated; coverage statuses distinguish missing metadata, configuration, timeout and exhausted budget.
- Logging could overwrite product comparisons, and notification selection could favor a cheap but less profitable different tier. Full product comparisons are retained; best alert selection uses total lot profit; Discord fields are bounded and delivery counts reflect successful sends.

## Design debate

**Keep broad discovery.** The live union found two URLs beyond the sitemap, and explicit city pagination fixes a concrete omission. Counterargument: a complete crawl of exposed sources still cannot prove CrowdVolt has no hidden or unlisted events. Coverage reports therefore describe observed source completeness.

**Keep strict tier and quantity verification.** Comparing a ferry pass, one-day ticket or restricted row against unrestricted admission creates misleading profits. Counterargument: exact labels and conservative split rules miss valid equivalent inventory and upgrades. The chosen tradeoff favors fewer unsupported alerts; aggregate prices remain diagnostic rather than disappearing entirely.

**Keep reverse destinations limited to TickPick and StubHub.** This follows the requested scope, including sellers-only and DICE events. Counterargument: restricting providers is not useful coverage if both feeds are blocked. The allowlist and downstream calculations are tested, but usable live feeds must still be demonstrated before relying on reverse results.

**Keep process deadlines, with limits.** They bound browser failures and prevent a provider from holding the scan indefinitely. Counterargument: process startup adds cost and the global budget can leave later events unsearched. Skips are reported; a full-catalog, all-provider runtime/load test has not been completed. Auxiliary scanner timeouts were not converted.

## Remaining limits

- StubHub's listing-response schema handling has synthetic coverage, not a successful live inventory fixture from this review. TickPick access and listing schema must also be confirmed with usable live inventory.
- SeatGeek is unconfigured in the checked environment. Aggregate-only VividSeats and Resident Advisor results cannot produce verified tier alerts.
- Four pre-existing matching checks still fail. Required identity fields and tier checks reduce some errors but do not prove artist identity matching is correct.
- Exact ticket labels can create false negatives. Seated reverse scans remain disabled. Auxiliary feeds are outside the verified product pipeline.
- No checkout, transfer eligibility, stale-price revalidation or destination sale is tested. Reverse estimates remain speculative; fee estimates and inventory changes can invalidate apparent profit.
- The legacy opportunity-log `alert` field records a candidate, including dry runs, rather than delivery. Scan notification counts report actual successful calls.

## Reproduce

```sh
python3 -B -m unittest test_catalog_products test_pipeline_review -v
python3 -B scripts/review_catalog.py
python3 -B scripts/review_smoke.py
```

The first command is offline. The scripts make live read-only marketplace requests and print their temporary output directory. Do not run the older `test_matching.py` wholesale as an offline check; it contains live/notification behavior.
