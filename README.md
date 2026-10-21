# Eco Retreat GitHub Actions monitor

Runs six independent one-night room-availability API requests every 10 minutes and sends a Telegram heartbeat every run, an immediate alert for a newly available room category, and a daily summary at the first run after 8 PM IST.

## Setup
1. Make a **public GitHub repository** (standard GitHub-hosted Actions runners are free in public repositories; never include Telegram secrets in source). Or use private and carefully monitor free minutes.
2. Upload `monitor.py`, `restore_state.py`, `requirements.txt`, and `.github/workflows/monitor.yml` to the **default branch** (preserve `.github` folder).
3. Under repository **Settings → Secrets and variables → Actions → New repository secret**, set `TG_TOKEN` (Telegram bot token) and `TG_CHAT_ID` (your chat ID).
4. Go to **Actions → Eco Retreat Monitor → Run workflow** to test; check Logs and Telegram. Ensure the workflow has read permissions for Actions artifacts in repository settings.
5. Watch scheduled runs under **Actions**. No servers, cron daemon, or database service needed.

## Important caveats
- GitHub Actions scheduled jobs are **best effort**, not precise 10-minute timers; they can be delayed or dropped. Cron offset minute 3 reduces top-of-hour contention.
- Inactivity disables schedules in public repos after 60 days. The 2026-12-22 target is more than 60 days away, so **you must reactivate scheduling or push a repository change before that date**. Merely running the action may not count as repo activity.
- State is stored as a GitHub Actions artifact for 30 days, restored on the next run. Losing/expiring state may cause an initial repeated alert. Restore/API failures are fatal so they don't silently overwrite older state.
- The monitoring response schema and meaning of `booking_status=1` **have not been verified against the live API**. Check one known response before trusting alerts. Unknown statuses or an empty room list are treated as errors instead of evidence of no availability.
- The daily summary's aggregate counters are lifetime totals, not 24-hour period totals.
- Six API calls per run (~864/day), so respect service limits and terms; if throttled, reduce frequency. Alerts indicate reported availability, not a confirmed opening of the booking portal.
- For a two-night stay on December 22 and 23, change monitoring to a single multi-night query after verifying API support.
- Notifications are at-least-once in edge cases: if the Telegram API succeeds but artifact upload fails, a repeat notification can occur next run.
