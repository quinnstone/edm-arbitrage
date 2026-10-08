# Discord position commands (no database)

This optional service edits the existing `positions.json` file in GitHub. Git history is the durable record. Nothing runs from local RAM alone, and no positions are stored in the evictable Actions cache. Existing scraper/provider code is unchanged.

## User commands

```
/position add event:<exact CrowdVolt title> date:2026-11-28 venue:<exact CrowdVolt venue> platform:StubHub listing_price:137.00
/position list
/position update position:<copy ID from list> listing_price:129.00
/position stop position:<copy ID from list>
```

Choose StubHub or TickPick. One unrestricted GA ticket per position. `listing_price` is the asking price you enter in the marketplace's seller form, before seller fees, in USD. It is not a buyer-facing total with extra buyer fees. The scanner fetches the CrowdVolt purchase cost and calculates margin automatically. This uses the existing GA risk calculation, not section/VIP-specific inventory.

### Seller fees and payout overrides

Default seller fee percentages are shared with the speculative digest in `seller_fees.json`:

- TickPick: 15%, its documented standard commission ([source](https://support.tickpick.com/hc/en-us/articles/23665791769367-What-is-FanLink)).
- StubHub: 12.5%, retained as an estimate only. StubHub does not publish a universal seller rate ([source](https://support.stubhub.com/articles/61000276392-quels-sont-les-frais-d-achat-de-stubhub-)).

Confirmations, lists, and risk alerts label calculated payouts/margins as estimates and show the fee assumption. For example, a $129 listing produces an estimated $109.65 TickPick payout or $112.88 StubHub payout. Rates may differ for your account or listing. No account-specific fee is inferred from CrowdVolt.

Both `add` and `update` accept one optional override:

```
/position update position:<ID> listing_price:129.00 seller_fee_pct:10
/position update position:<ID> listing_price:129.00 net_payout:116.10
```

`seller_fee_pct` saves your fee assumption for that position and is reused on later price changes; enter `10` for 10%. A supplied `net_payout` is used directly, with no additional fee deduction. Supply one override at a time. An exact payout override is cleared when the listing price changes or a fee override is supplied, so a stale payout cannot survive repricing. It is retained when the price is unchanged and no replacement is supplied. You can provide a new exact payout alongside a new listing price.

New rows use `price_basis: "listing"`, `listed_price`, `seller_fee_pct`, and `seller_fee_source`, with optional `net_payout_override`. Legacy rows without `price_basis` still store NET payout in `listed_price`; they are never automatically converted or charged fees again. Explicitly updating a legacy row with `listing_price` converts that one row to the new basis, preserving its ID.

Add saves configuration, not a verified opportunity. The response explicitly says that event/supply verification is pending. Exact title (including Saturday/Afters qualifiers), venue and the existing nightlife date must resolve uniquely during a scan. No fuzzy name fallback is used for Discord additions. An unresolved position triggers the existing unresolved behavior after consecutive misses. Old positions retain their previous resolver.

The existing `event|date|platform|price` ID format remains. Updating the price freezes the prior ID in `position_id`; old rows without that property keep their current IDs. Stop closes only the chosen entry. Closed IDs are not silently reopened. Commands change monitoring records, not marketplace listing prices. There is no bulk stop, free-text message listener, automatic ticket listing, purchase, or delisting.

## Files and deployment

- `worker.mjs`: Cloudflare Worker HTTP interaction endpoint, WebCrypto signature verification, user/server/channel allowlist, GitHub Contents API.
- `register.mjs`: explicit one-time guild command registration. POST only `/position`, without replacing unrelated commands.
- `wrangler.toml`: Worker configuration; deployment is an operator setup step.
- `position_store.py`: opt-in fresh GitHub reader for the Python scanner.
- `seller_fees.json`: shared fee percentage defaults, bundled into the Worker and read by the speculative scanner.
- `test_discord_positions.py`, `worker.test.mjs`: offline integration and failure tests.

### Required setup (not performed by adding these files)

1. Create a Discord application in the Developer Portal. Record application ID and public key. Create a bot token for the registration step; never commit it.
2. Create/deploy the Worker in your Cloudflare account. Set these Worker variables: `DISCORD_APPLICATION_ID`, `DISCORD_PUBLIC_KEY`, `DISCORD_USER_ID`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`, `GITHUB_REPO=quinnstone/edm-arbitrage`, `GITHUB_BRANCH=main`. Use your EXISTING channel's ID for `DISCORD_CHANNEL_ID`; no new channel is needed. Initially point `GITHUB_BRANCH` at a separate test branch.
3. Store `GITHUB_TOKEN` as a Worker secret: a fine-grained GitHub token scoped to this repository, Contents read/write only. Do not give it Actions, administration, or other repository permissions. Branch rules may prevent direct writes; verify in a test branch before enabling commands. No token belongs in source code or a Discord message.
4. Deploy using `npx wrangler@4 deploy --keep-vars` from this directory. Configure the deployed URL as the Discord application's Interactions Endpoint URL; Discord checks signatures and PING behavior.
5. Install the app in your server with the `applications.commands` scope. Set local environment variables `DISCORD_APPLICATION_ID`, `DISCORD_GUILD_ID`, `DISCORD_BOT_TOKEN`, and run `node discord_positions/register.mjs` from the repo root. Registration is disabled by default for ordinary members; grant your user access in Server Settings → Integrations. The endpoint independently checks your exact user, server and channel IDs.
6. Validate in your allowed existing channel against a separate test repository/branch. Command replies are private. Run add/list/update/stop, repeat commands, and verify GitHub records and scanner behavior. No real listing is required. Then point the Worker at `main`; the production app and reader must use the same branch.
7. Only after validation, enable repository Actions variable `POSITION_REMOTE_REPO=quinnstone/edm-arbitrage` and optionally `POSITION_REMOTE_BRANCH=main`. The existing workflow passes its GitHub token as a read credential. Ensure that token has Contents read permission. Local/manual scanners require `POSITION_READ_TOKEN` if remote mode is enabled. A separate write token is never passed to the scanner.

The workflow wiring is already committed. Worker deployment, command registration and repository variable activation are still required before this feature is live. Existing runs use local-file mode while `POSITION_REMOTE_REPO` is unset. Redeploy the Worker and re-run `register.mjs` for this listing-price command schema; older `net_payout`-only commands are rejected with a registration instruction.

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

29 JavaScript tests and 24 Python tests pass. Tests cover signatures, authorization, conflict retries, duplicate delivery, stable IDs, platform-specific closure, exact/ambiguous event resolution, remote outages, mid-scan stop/update, hourly reminders, fee math and cent boundaries, legacy net preservation, stale override removal, actual JavaScript-to-Python saved positions, estimated alert labels, the TickPick digest profit threshold, and existing provider routing. No real Discord/GitHub mutations occur in tests.

Not yet validated: live Cloudflare deployment, real Discord registration/permissions, real GitHub credential/branch policy, live completion timing, and the full hosted add-to-alert-to-stop flow. These require account setup and are mandatory activation checks.
