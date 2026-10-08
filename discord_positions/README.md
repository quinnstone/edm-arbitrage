# Discord position commands (no database)

This optional service edits the existing `positions.json` file in GitHub. Git history is the durable record. Nothing runs from local RAM alone, and no positions are stored in the evictable Actions cache. Existing scraper/provider code is unchanged.

## User commands

```
/position add event:<exact CrowdVolt title> date:2026-11-28 venue:<exact CrowdVolt venue> platform:StubHub net_payout:137.00
/position list
/position list status:closed page:1
/position update position:<copy ID from list> net_payout:129.00
/position stop position:<copy ID from list>
/position update position:<ID> venue:<correct CrowdVolt venue>
/position resume position:<ID>
```

Choose StubHub or TickPick. One unrestricted GA ticket per position. `net_payout` is USD received after seller fees; no additional deduction is applied. This uses the existing GA risk calculation, not section/VIP-specific inventory. Do not use it for reserved seats or restricted entry products.

Add saves configuration, not a verified opportunity. The response explicitly says that event/supply verification is pending. Exact title (including Saturday/Afters qualifiers), venue and the existing nightlife date must resolve uniquely during a scan. No fuzzy name fallback is used for Discord additions. An unresolved position triggers the existing unresolved behavior after consecutive misses. Old positions retain their previous resolver.

The existing `event|date|platform|price` ID format remains. Updating the payout freezes the prior ID in `position_id`; old rows without that property keep their current IDs. Stop closes only the chosen entry. `update` accepts one or more of `net_payout`, `event`, `date`, and `venue`; the ID remains unchanged even if its original text contains the old name/date/payout. Updating a stopped position keeps it closed. Use `resume` explicitly to restart it. Resume rejects sold or past positions and duplicates of another active position on the same platform. Legacy positions need a venue before resuming; supply it with `update`.

Each effective correction or resume increments an internal monitoring revision. The monitor discards the previous severity/cooldown/miss count for that revision and assesses the position afresh. Repeating an identical update or resuming an already-open position does not reset its cooldown. Other positions are unaffected. Closed IDs are not silently reopened by `add`.

`list` defaults to open positions and page 1. Select `status:closed` to find IDs to resume, or `status:all` for history (including sold positions). Responses show the current page, page count, and next command. Entire entries stay together; IDs are never truncated. Read another page if you change positions between list requests. There is no bulk stop, free-text message listener, automatic ticket listing, purchase, or delisting.

## Files and deployment

- `worker.mjs`: Cloudflare Worker HTTP interaction endpoint, WebCrypto signature verification, user/server/channel allowlist, GitHub Contents API.
- `register.mjs`: explicit one-time guild command registration. POST only `/position`, without replacing unrelated commands.
- `wrangler.toml`: Worker configuration; deployment is an operator setup step.
- `position_store.py`: opt-in fresh GitHub reader for the Python scanner.
- `test_discord_positions.py`, `worker.test.mjs`: offline integration and failure tests.

### Required setup (not performed by adding these files)

1. Create a Discord application in the Developer Portal. Record application ID and public key. Create a bot token for the registration step; never commit it.
2. Create/deploy the Worker in your Cloudflare account. Set these Worker variables: `DISCORD_APPLICATION_ID`, `DISCORD_PUBLIC_KEY`, `DISCORD_USER_ID`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`, `GITHUB_REPO=quinnstone/edm-arbitrage`, `GITHUB_BRANCH=main`.
3. Store `GITHUB_TOKEN` as a Worker secret: a fine-grained GitHub token scoped to this repository, Contents read/write only. Do not give it Actions, administration, or other repository permissions. Branch rules may prevent direct writes; verify in a test branch before enabling commands. No token belongs in source code or a Discord message.
4. Deploy using Wrangler from this directory. Configure the deployed URL as the Discord application's Interactions Endpoint URL; Discord checks signatures and PING behavior.
5. Install the app in your server with the `applications.commands` scope. Set local environment variables `DISCORD_APPLICATION_ID`, `DISCORD_GUILD_ID`, `DISCORD_BOT_TOKEN`, and run `node discord_positions/register.mjs` from the repo root. Registration is disabled by default for ordinary members; grant your user access in Server Settings → Integrations. The endpoint independently checks your exact user, server and channel IDs.
6. Validate in a private test channel against a separate test repository/branch. The app and reader must use the same branch. Run add/list/update/stop/resume, test multi-page lists, repeat commands, and verify GitHub records and scanner behavior. No real listing is required.
7. Only after validation, enable repository Actions variable `POSITION_REMOTE_REPO=quinnstone/edm-arbitrage` and optionally `POSITION_REMOTE_BRANCH=main`. The existing workflow passes its GitHub token as a read credential. Ensure that token has Contents read permission. Local/manual scanners require `POSITION_READ_TOKEN` if remote mode is enabled. A separate write token is never passed to the scanner.

Deploy the Worker and register its current command schema before this feature is live. After changing command options or adding `resume`, rerun `register.mjs` as well as redeploying the Worker. The workflow integration is already committed but is opt-in. Existing runs are unaffected while `POSITION_REMOTE_REPO` is unset.

## Failure behavior and boundaries

- Discord receives an immediate deferred private response. GitHub operations have 3-second request limits and up to three conflict attempts; completion edits the response after a confirmed save. A command service shutdown or network failure may interrupt completion. If confirmation is absent, `/position list` is authoritative; don't assume the operation failed or succeeded.
- Writes use file SHA conflict detection and refetch/reapply before retry. Interaction receipts in the same committed file prevent duplicate delivery from making duplicate positions. The last 200 receipts are retained. Very old replay deliveries are rejected by timestamp verification; separate duplicate add commands conflict with existing position identity.
- Stopping or updating during a scan is rechecked before risk notification. An already submitted alert may still arrive. Different platform positions are independent.
- Remote storage errors do not fall back to stale checkout positions or imply no open positions. Position monitoring reports an error and returns; other arbitrage scans continue. Its alert uses the existing problem webhook. No credentials are printed.
- No change to scanner cadence: hourly underwater reminders remain conditional on scans executing. The command service does not promise immediate price checks. `/position list` is saved configuration, not a live price/status report.
- The current main workflow still owns risk-alert state. GitHub cache behavior and general price-fetch accuracy are outside this command integration.
- Clearing the remote variable restores local-file mode on later runs. First sync the latest committed positions, or an old checkout can resurrect closed rows. Never roll back position data along with application code unintentionally.

## Validation completed locally

```
node --test discord_positions/worker.test.mjs
python3 -B -m unittest test_discord_positions test_spec_platforms -v
```

29 JavaScript tests and 22 Python tests pass. Tests cover signatures, stale/tampered requests, authorization, defer, payload validation, conflict retries, duplicate delivery, payout/ID preservation, platform-specific closure, exact and ambiguous event resolution, remote outages, mid-scan stop/update, unchanged alerts, margin boundaries, existing provider routing, closed corrections/resume, revision-specific cooldown reset, stop/resume during a scan, complete ID pagination, and actual JavaScript-to-Python command output. No real Discord/GitHub mutations occur in tests.

Not yet validated: live Cloudflare deployment, real Discord registration/permissions, real GitHub credential/branch policy, live completion timing, and the full hosted add-to-alert-to-stop flow. These require account setup and are mandatory activation checks.
