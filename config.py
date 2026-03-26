import os

# Discord webhook for arbitrage alerts
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# SeatGeek API — free, get your client_id at https://platform.seatgeek.com
SEATGEEK_CLIENT_ID = os.getenv("SEATGEEK_CLIENT_ID", "")

# Ticketmaster API — free, get your key at https://developer.ticketmaster.com
TICKETMASTER_API_KEY = os.getenv("TICKETMASTER_API_KEY", "")

# VividSeats — placeholder for future use (requires affiliate approval)
VIVIDSEATS_API_KEY = os.getenv("VIVIDSEATS_API_KEY", "")

# Scrape interval in minutes
SCAN_INTERVAL_MINUTES = 30

# Minimum profit threshold (dollars) to trigger an alert
MIN_PROFIT_THRESHOLD = 10

# Minimum profit margin (percentage) to trigger an alert
MIN_PROFIT_MARGIN_PCT = 5

# CrowdVolt base URL
CROWDVOLT_BASE_URL = "https://www.crowdvolt.com"

# Request settings
REQUEST_TIMEOUT = 15
REQUEST_DELAY_SECONDS = 1.5  # delay between CrowdVolt page fetches to be polite
