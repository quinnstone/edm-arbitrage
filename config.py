import os

# Discord webhook for arbitrage alerts
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# SeatGeek API — free, get your client_id at https://platform.seatgeek.com
SEATGEEK_CLIENT_ID = os.getenv("SEATGEEK_CLIENT_ID", "")

# VividSeats — placeholder for future use (requires affiliate approval)
VIVIDSEATS_API_KEY = os.getenv("VIVIDSEATS_API_KEY", "")

# GroupMe — scan group chat for ticket buy requests
GROUPME_TOKEN = os.getenv("GROUPME_TOKEN", "")
GROUPME_GROUP_ID = os.getenv("GROUPME_GROUP_ID", "")

# GroupMe — how far back to scan for buy requests (rolling window)
GROUPME_LOOKBACK_DAYS = 7

# Scrape interval in minutes
SCAN_INTERVAL_MINUTES = 15

# Minimum profit margin (percentage) to trigger an alert
MIN_PROFIT_MARGIN_PCT = 5

# Minimum estimated profit (dollars) for a CV→3P spec listing alert.
# Computed net of 3P seller fees (TickPick 10%, StubHub/VividSeats/
# SeatGeek 12.5%, Gametime 10%) — this is the actual take-home.
# $15+ ensures the operational overhead is worth it after all fees.
MIN_SPEC_PROFIT = 15

# Hours at which the CV→3P spec digest fires, in America/New_York.
# (13, 17) = 1pm and 5pm. Within each digest hour the message sends at
# most once thanks to per-event cooldown; events alerted at 1pm don't
# duplicate at 5pm, but the same opportunity persisting overnight
# refreshes in the following day's 1pm digest.
SPEC_DIGEST_HOURS = (13, 17)

# Ticket Radar — availability + code scanner. Runs every 6h via cron.
# Profit floor is the minimum after-fee net to surface; $1 = wide net.
TICKET_RADAR_PROFIT_FLOOR = 1

# Subreddit Ticket Radar scrapes for code candidates. Posts here often
# include presale and access codes for NYC EDM events.
TICKET_RADAR_SUBREDDIT = "avesNYC_tix"

# Manually-supplied codes. Populate from your private channels (newsletter
# emails, Discord drops, etc.) — these get tested against every event in
# the radar pool. Codes here bypass the noise filter and always validate.
KNOWN_CODES: list[str] = []

# CrowdVolt base URL
CROWDVOLT_BASE_URL = "https://www.crowdvolt.com"

# Estimated buyer fee percentages per platform (decimal).
# Applied to base prices to approximate all-in cost.
# TickPick advertises no buyer fees; others charge 20-30%.
PLATFORM_FEES = {
    "SeatGeek": 0.22,
    "StubHub": 0.28,
    "VividSeats": 0.28,
    "TickPick": 0.0,
    "Gametime": 0.0,  # Gametime shows all-in prices (no hidden fees)
    # RA is a listing platform pointing to external primaries (DICE,
    # Eventbrite, etc.). The cost string is the base price; the external
    # ticket vendor adds ~10-15% at checkout. Middle estimate.
    "ResidentAdvisor": 0.15,
}

# Request settings
REQUEST_TIMEOUT = 15
REQUEST_DELAY_SECONDS = 1.5  # delay between CrowdVolt page fetches to be polite
