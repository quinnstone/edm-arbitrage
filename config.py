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
SCAN_INTERVAL_MINUTES = 30

# Minimum profit threshold (dollars) to trigger an alert
MIN_PROFIT_THRESHOLD = 10

# Minimum profit margin (percentage) to trigger an alert
MIN_PROFIT_MARGIN_PCT = 5

# CrowdVolt base URL
CROWDVOLT_BASE_URL = "https://www.crowdvolt.com"

# Estimated buyer fee percentages per platform (decimal).
# Applied to base prices to approximate all-in cost.
# TickPick advertises no buyer fees; others charge 20-30%.
PLATFORM_FEES = {
    "SeatGeek": 0.22,
    "StubHub": 0.45,
    "VividSeats": 0.28,
    "TickPick": 0.0,
}

# Request settings
REQUEST_TIMEOUT = 15
REQUEST_DELAY_SECONDS = 1.5  # delay between CrowdVolt page fetches to be polite
