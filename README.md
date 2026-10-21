# Eco Retreat Monitor: full date range

## Dates and booking intent
Monitors every **check-in date** from **2026-11-01 to 2027-03-01 inclusive** (121 dates), at Konark (37), Satkosia (41) and Sonapur (43). Each API request tests **one night**, with checkout the following day. On 2027-03-01, checkout is 2027-03-02. The monitor skips historical check-in dates after they pass.

## Installation
Upload `monitor.py`, `restore_state.py`, `requirements.txt`, and `.github/workflows/monitor.yml` to the default branch of your GitHub repository. Add repository *Actions secrets*: `TG_TOKEN` and `TG_CHAT_ID`. Navigate to Actions → Eco Retreat Monitor → Run workflow to test, then observe the subsequent scheduled runs.

The workflow triggers approximately every 10 minutes (GitHub can delay or omit scheduled runs). It retrieves `state.json` from the most recent previous run's artifact and uploads the updated state at the end; runs are serialized by the concurrency group. Public repositories using standard hosted runners can generally use Actions without per-minute charges, subject to current GitHub terms.

## Important constraints
- 121 days × 3 hotels = **363 API requests per complete scan**. Every 10 minutes would mean up to **52,272 requests/day**; this is a very high volume for a third-party booking API. Consider reducing the frequency or monitoring only priority dates until bookings approach opening. Respect service policies and rate limits.
- Run-time budget of 480 seconds rotates starting offset so unscanned days receive priority on subsequent runs. A partial scan is explicitly identified in Telegram.
- Empty API `data: []` means **unknown**, not unavailable; it will not reset previously available state or create false opening alerts. Only *explicit* unavailable results reset an available state. The API response structure and semantics of `booking_status=1` still need live verification.
- `quantity` is displayed as supplied by the API and is not assumed to mean units remaining.
- New availability alerts are emitted at first observation of available status and again when it changes from explicit unavailable to available. A successful Telegram response can still precede a failed artifact upload, causing a duplicate alert.
- 10-minute heartbeat is enabled; disable with `HEARTBEAT_EVERY_RUN: '0'` in workflow. Daily summary at first run after 20:00 IST; counts are cumulative, not daily.
- Do not commit credentials or copied API responses that contain sensitive information.
- GitHub scheduled workflows in inactive public repositories may be disabled after 60 days; inspect automation health periodically.
