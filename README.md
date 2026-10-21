# Eco Retreat — Telegram-controlled GitHub Actions monitor

No permanent server required. GitHub Actions runs on a best-effort schedule around every 10 minutes. Only the private Telegram chat whose numeric ID equals `TG_CHAT_ID` can change settings. Changes take effect when the next workflow executes, not immediately. Only one process should poll the bot; remove a pre-existing webhook if necessary.

## Setup
1. Extract files preserving the `.github/workflows/monitor.yml` path, push to a GitHub repository (public repositories generally have free standard hosted-runner minutes).
2. Add **Actions secrets** `TG_TOKEN` and `TG_CHAT_ID` under repository Settings → Secrets and variables → Actions. Do not commit tokens or state files.
3. Open **Actions → Eco Retreat Monitor → Run workflow** to test. Send `/help` or `/status` to your bot, then run the workflow once. The monitor starts **STOPPED**, so setup commands do not initiate API scans.
4. Subsequent executions are scheduled every ten minutes at minute 03/13/23/33/43/53 (UTC). Scheduled GitHub runs can be delayed or skipped; public repository schedules can be disabled after 60 days without activity.

## Telegram commands (send to YOUR OWN bot)
```
/help
/status
/start
/stop
/range 2026-11-01 2027-03-01
/dates 2026-12-22 2026-12-23
/add 2026-12-24
/remove 2026-12-23
/hotels
/hoteladd 55 New Eco Retreat Location
/hotelselect 37 55
/hotelremove 55
/pause
/resume
/check
```

`/start` turns on periodic scanning; `/stop` turns it off while leaving Telegram command polling active. New installations begin in STOPPED mode. `/check` requests one limited scan batch, without changing STOPPED status. `/pause` and `/resume` are aliases for `/stop` and `/start`.

`/hoteladd` creates a new numeric ID → hotel name mapping and enables it. `/hotelselect` replaces the current enabled set; every ID must first be registered. `/hotelremove` removes an ID from both the mapping and enabled set. `/range` is inclusive, capped at 366 days; `/dates` replaces the selection. `/add` and `/remove` convert an existing range to an explicit date selection. Past check-in dates are automatically skipped. `/check` requests a scan *within the scheduled run that reads the command* (not an instant invocation).

The initial selection is Konark (37), Satkosia (41), Sonapur (43), from 2026-11-01 to 2027-03-01. Changing hotel ID/name does not validate its existence at Book Odisha; an incorrect ID will lead to unknown checks.

## Persistence and reliability
`state.json` stores date selection, hotel mapping, enabled IDs, Telegram update cursor, last-known availability, pending alerts and counters. The workflow restores it from the latest GitHub Actions artifact and uploads a new artifact at the end. Never delete state artifacts unless you intend to reset monitoring. Artifact retention is 30 days; running workflow regularly refreshes it. A GitHub outage or failure to upload state can cause missed configuration changes or repeated notifications. In-flight crashes between successful Telegram delivery and uploading state can produce duplicate alerts. This architecture is best-effort rather than transactional.

The monitor treats empty `data` lists or unexpected server responses as **unknown**, not proof that rooms are unavailable. `AVAILABLE_STATUS=1` remains an **unverified assumption**; validate against known available/unavailable examples. If the API changes its response format, update parsing before relying on the alerts.

An all-date scan can require hundreds of third-party requests. Respect rate limits and the website's terms. `RUN_BUDGET_SECONDS` prevents a scheduled invocation from running indefinitely and scan rotation covers remaining dates in later runs. Available categories are counted only among successfully checked hotel/date requests.

## Controlling load and timing
The scheduled workflow polls Telegram first; if stopped, it performs no hotel/date API requests. When running, it scans up to `MAX_CHECKS_PER_RUN=30` hotel/date pairs per run with a time budget of 240 seconds, rotating among the full selection across runs. Thus with 363 pairs, **a full cycle takes at least 13 scheduled runs (about 130 minutes, possibly longer)**. Choose a small range if you need every selected date scanned close to every 10 minutes. Periodic Telegram status is sent only during running or `/check`; `/status` responds while stopped. Daily summary is sent only while scanning is enabled.

The persisted state from an older installation may have `paused=false`. To stop that existing installation, send `/stop`, then run the workflow manually to apply it. If the existing state is not recoverable, fresh state starts stopped.
