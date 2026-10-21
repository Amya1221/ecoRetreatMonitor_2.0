"""Eco Retreat availability monitor for GitHub Actions (Python 3.12+)."""
import html
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API = 'https://admin.bookodisha.com/api/auth/hotel_details'
DEFAULT_HOTELS = {'37': 'Konark', '41': 'Satkosia', '43': 'Sonapur'}
FIRST_DATE = date(2026, 11, 1)
LAST_DATE = date(2027, 3, 1)  # inclusive CHECK-IN dates
STATE_FILE = Path('state.json')
IST = ZoneInfo('Asia/Kolkata')
TOKEN = os.environ['TG_TOKEN']
CHAT_ID = os.environ['TG_CHAT_ID']
AVAILABLE_VALUE = os.getenv('AVAILABLE_STATUS', '1').strip().lower()
HEARTBEAT = os.getenv('HEARTBEAT_EVERY_RUN', '1') == '1'
SUMMARY_HOUR = int(os.getenv('DAILY_SUMMARY_HOUR_IST', '20'))
DELAY = float(os.getenv('REQUEST_DELAY_SECONDS', '0.25'))
RUN_BUDGET = float(os.getenv('RUN_BUDGET_SECONDS', '240'))
MAX_CHECKS = int(os.getenv('MAX_CHECKS_PER_RUN', '30'))
DEBUG_API = os.getenv('DEBUG_API_RESPONSE', '0') == '1'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0',
    'Origin': 'https://www.bookodisha.com',
    'Referer': 'https://www.bookodisha.com/',
})


def checkin_dates(state):
    today = datetime.now(IST).date()
    if state['date_mode'] == 'selected':
        for value in sorted(set(state['selected_dates'])):
            day = date.fromisoformat(value)
            if day >= today:
                yield day
    else:
        day = max(date.fromisoformat(state['range_start']), today)
        end = date.fromisoformat(state['range_end'])
        while day <= end:
            yield day
            day += timedelta(days=1)


def fresh_state():
    return {'rooms': {}, 'pending': {}, 'last_summary': None,
            'hotels': DEFAULT_HOTELS.copy(), 'selected_hotels': list(DEFAULT_HOTELS),
            'date_mode': 'range', 'range_start': FIRST_DATE.isoformat(),
            'range_end': LAST_DATE.isoformat(), 'selected_dates': [],
            'paused': True, 'one_shot': False, 'telegram_offset': 0,
            'runs': 0, 'failed_checks': 0, 'alerts': 0,
            'last_run': None, 'scan_offset': 0}


def load_state():
    if not STATE_FILE.exists():
        logging.warning('No previous state; starting new observation baseline')
        return fresh_state()
    data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('rooms'), dict) or not isinstance(data.get('pending', {}), dict):
        raise ValueError('Bad state.json; refusing to overwrite it')
    for k, v in fresh_state().items():
        data.setdefault(k, v)
    if not isinstance(data['hotels'], dict) or not isinstance(data['selected_hotels'], list):
        raise ValueError('Invalid persisted hotel configuration')
    return data


def save_state(data):
    temp = STATE_FILE.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    temp.replace(STATE_FILE)


def send(message):
    endpoint = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    for attempt in range(3):
        try:
            r = SESSION.post(endpoint, data={'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML'}, timeout=15)
            if r.status_code == 429:
                try:
                    pause = int(r.json().get('parameters', {}).get('retry_after', 5))
                except (TypeError, ValueError, KeyError):
                    pause = 5
                time.sleep(min(max(pause, 1), 60))
                continue
            r.raise_for_status()
            if r.json().get('ok') is True:
                return True
            logging.error('Telegram rejected message: HTTP %s', r.status_code)
            return False
        except (requests.RequestException, ValueError) as exc:
            logging.warning('Telegram send attempt %s failed: %s', attempt + 1, exc)
            if attempt < 2:
                time.sleep(2 ** attempt)
    return False


def status_is_available(value):
    # IMPORTANT: This is a configurable assumption, not verified API documentation.
    if isinstance(value, bool):
        normalized = '1' if value else '0'
    elif isinstance(value, (int, str)) and str(value).strip().lower() in ('0', '1', 'true', 'false'):
        normalized = str(value).strip().lower()
        normalized = '1' if normalized == 'true' else ('0' if normalized == 'false' else normalized)
    else:
        raise ValueError(f'Unrecognized booking_status={value!r}')
    if AVAILABLE_VALUE not in ('0', '1', 'true', 'false'):
        raise ValueError('AVAILABLE_STATUS must be 0, 1, true or false')
    expected = '1' if AVAILABLE_VALUE == 'true' else ('0' if AVAILABLE_VALUE == 'false' else AVAILABLE_VALUE)
    return normalized == expected


def fetch(hotel, location, day):
    date_text = day.isoformat()
    payload = {
        'request_type': 'check_room_availability',
        'requested_room': 'all',
        'hotelId': hotel,
        'roomRequest': '[{"adult":2,"child":0}]',
        'checkinDate': date_text,
        'checkoutDate': (day + timedelta(days=1)).isoformat(),
    }
    for attempt in range(2):
        try:
            r = SESSION.post(API, data=payload, timeout=(5, 10))
            if r.status_code in (403, 404, 429):
                logging.warning('API hotel=%s date=%s HTTP=%s', hotel, date_text, r.status_code)
                return None
            r.raise_for_status()
            body = r.json()
            if DEBUG_API:
                # Avoid full response/body logging: may contain sensitive fields.
                logging.info('API DEBUG hotel=%s date=%s http=%s keys=%s data_type=%s',
                             hotel, date_text, r.status_code,
                             list(body.keys()) if isinstance(body, dict) else type(body).__name__,
                             type(body.get('data')).__name__ if isinstance(body, dict) else 'n/a')
            if not isinstance(body, dict) or not isinstance(body.get('data'), list):
                logging.warning('Unknown API schema hotel=%s date=%s: keys=%s', hotel, date_text,
                                list(body.keys()) if isinstance(body, dict) else type(body).__name__)
                return None
            if not body['data']:
                logging.info('No inventory returned (UNKNOWN, not sold out): %s %s', hotel, date_text)
                return None
            results = {}
            for room in body['data']:
                if not isinstance(room, dict) or room.get('id') is None:
                    raise ValueError('Malformed room in response')
                key = f'{hotel}|{date_text}|{room["id"]}'
                results[key] = {
                    'available': status_is_available(room.get('booking_status')),
                    'location': location, 'date': date_text,
                    'room': str(room.get('title', 'Unknown')),
                    'price': str(room.get('price', 'Unknown')),
                    'qty': str(room.get('quantity', 'Unknown')),
                }
            return results
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
            logging.warning('Fetch %s %s attempt %s: %s', hotel, date_text, attempt + 1, exc)
            if attempt == 0:
                time.sleep(1)
        except (ValueError, TypeError) as exc:
            logging.warning('Invalid API response hotel=%s date=%s: %s', hotel, date_text, exc)
            return None
    return None


def alert(room):
    return ('🚨 <b>ECO RETREAT — NEW REPORTED AVAILABILITY</b>\n'
            f'📍 {html.escape(room["location"])}\n'
            f'📅 Check-in: {room["date"]}\n'
            f'🛏 {html.escape(room["room"])}\n'
            f'💰 ₹{html.escape(room["price"])}\n'
            f'📦 Quantity (API): {html.escape(room["qty"])}\n'
            '🔗 https://www.bookodisha.com/\n'
            'Verify on the booking website; API status is not a confirmed reservation.')


# Telegram is polled on each scheduled run. Only the configured private chat
# may change settings. Do not use getUpdates concurrently with a webhook.
def valid_date(value):
    parsed = date.fromisoformat(value)
    if not (date(2026, 1, 1) <= parsed <= date(2030, 12, 31)):
        raise ValueError('Date must be within 2026–2030')
    return parsed


def describe(state):
    hotels = ', '.join(f'{h}: {state["hotels"][h]}' for h in state['selected_hotels']) or '(none)'
    if state['date_mode'] == 'range':
        dates = f'{state["range_start"]} to {state["range_end"]}'
    else:
        dates = ', '.join(state['selected_dates']) or '(none)'
    return ('📋 <b>Monitor configuration</b>\n'
            f'Hotels: {html.escape(hotels)}\n'
            f'Dates: {html.escape(dates)}\n'
            f'Monitor: {"STOPPED" if state["paused"] else "RUNNING"}\n'
            'Changes are applied during the next scheduled workflow run.')


HELP = ('Commands:\n/start — start periodic availability monitoring\n/stop — stop checks; Telegram commands remain active\n/range 2026-11-01 2027-03-01 — inclusive range\n/dates 2026-12-22 2026-12-23 — replace with selected dates\n/add 2026-12-24 — add a date (switches to selected mode)\n/remove 2026-12-24 — remove date (switches to selected mode)\n/hotels — list known and active hotels\n/hoteladd 55 My Hotel — register ID and name, enable it\n/hotelremove 55 — disable and remove a hotel ID\n/hotelselect 37 41 55 — enable only these registered IDs\n/status — current settings\n/pause /resume — aliases for /stop and /start\n/check — perform one scan batch even if stopped, without starting periodic checks\n/help — show commands\nCommands are read by the next GitHub Actions run; no instant execution.')


def process_command(state, message):
    parts = message.strip().split()
    if not parts:
        return None
    cmd = parts[0].split('@')[0].lower()
    args = parts[1:]
    if cmd == '/help':
        return html.escape(HELP)
    if cmd == '/status':
        return describe(state)
    if cmd == '/hotels':
        return ('🏨 <b>Registered hotels</b>\n' + '\n'.join(
            f'{"✅" if h in state["selected_hotels"] else "▫️"} {html.escape(h)} — {html.escape(name)}'
            for h, name in sorted(state['hotels'].items())))
    if cmd == '/range':
        if len(args) != 2:
            raise ValueError('Use /range YYYY-MM-DD YYYY-MM-DD')
        start, end = map(valid_date, args)
        if start > end or (end - start).days > 365:
            raise ValueError('Range must be ordered and at most 366 days inclusive')
        state.update(date_mode='range', range_start=start.isoformat(), range_end=end.isoformat())
    elif cmd == '/dates':
        if not 1 <= len(args) <= 366:
            raise ValueError('Provide 1–366 dates: /dates YYYY-MM-DD ...')
        state.update(date_mode='selected', selected_dates=sorted({valid_date(a).isoformat() for a in args}))
    elif cmd in ('/add', '/remove'):
        if len(args) != 1:
            raise ValueError(f'Use {cmd} YYYY-MM-DD')
        value = valid_date(args[0]).isoformat()
        dates = set(state['selected_dates']) if state['date_mode'] == 'selected' else {
            (date.fromisoformat(state['range_start']) + timedelta(days=i)).isoformat()
            for i in range((date.fromisoformat(state['range_end']) - date.fromisoformat(state['range_start'])).days + 1)
        }
        if cmd == '/add':
            dates.add(value)
        else:
            dates.discard(value)
        state.update(date_mode='selected', selected_dates=sorted(dates))
    elif cmd == '/hoteladd':
        if len(args) < 2 or not args[0].isdigit() or not 1 <= len(args[0]) <= 12:
            raise ValueError('Use /hoteladd 55 Hotel Name (numeric ID + name)')
        hotel_id, name = args[0], ' '.join(args[1:]).strip()
        if len(name) > 80:
            raise ValueError('Hotel name maximum 80 characters')
        state['hotels'][hotel_id] = name
        if hotel_id not in state['selected_hotels']:
            state['selected_hotels'].append(hotel_id)
    elif cmd == '/hotelremove':
        if len(args) != 1 or args[0] not in state['hotels']:
            raise ValueError('Use /hotelremove ID with a registered hotel ID')
        hotel_id = args[0]
        state['hotels'].pop(hotel_id)
        state['selected_hotels'] = [h for h in state['selected_hotels'] if h != hotel_id]
    elif cmd == '/hotelselect':
        if not args or len(args) != len(set(args)):
            raise ValueError('Use /hotelselect 37 41 (distinct registered IDs)')
        unknown = [h for h in args if h not in state['hotels']]
        if unknown:
            raise ValueError('Unknown hotel ID(s): ' + ', '.join(unknown) + '. Register with /hoteladd ID Name')
        state['selected_hotels'] = args
    elif cmd in ('/stop', '/pause'):
        state['paused'] = True
        state['one_shot'] = False
    elif cmd in ('/start', '/resume'):
        state['paused'] = False
    elif cmd == '/check':
        state['one_shot'] = True
    else:
        return 'Unknown command. Send /help.'
    return '✅ Settings updated.\n' + describe(state)


def poll_commands(state):
    endpoint = f'https://api.telegram.org/bot{TOKEN}/getUpdates'
    try:
        response = SESSION.get(endpoint, params={
            'offset': int(state['telegram_offset']) + 1,
            'timeout': 0, 'limit': 100,
            'allowed_updates': json.dumps(['message']),
        }, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if not payload.get('ok') or not isinstance(payload.get('result'), list):
            raise ValueError('Unexpected Telegram getUpdates response')
    except (requests.RequestException, ValueError) as exc:
        logging.warning('Telegram command polling failed: %s', exc)
        return
    for update in payload['result']:
        uid = update.get('update_id')
        if not isinstance(uid, int):
            continue
        message = update.get('message') or {}
        chat_id = str((message.get('chat') or {}).get('id', ''))
        user_id = str((message.get('from') or {}).get('id', ''))
        text = message.get('text', '')
        # Only permit commands from the intended private owner chat.
        if chat_id == str(CHAT_ID) and user_id == str(CHAT_ID) and text.startswith('/'):
            try:
                reply = process_command(state, text)
            except (ValueError, OverflowError) as exc:
                reply = '❌ ' + html.escape(str(exc))
            save_state(state)  # persist updated config before acknowledging
            if reply:
                send(reply)
        state['telegram_offset'] = max(int(state['telegram_offset']), uid)
        save_state(state)

def main():
    state = load_state()
    poll_commands(state)
    start = time.monotonic()
    now = datetime.now(IST)
    one_shot = bool(state.pop('one_shot', False))
    save_state(state)
    scan_enabled = (not state['paused']) or one_shot
    jobs = ([] if not scan_enabled else [(h, state['hotels'][h], day)
            for h in state['selected_hotels'] for day in checkin_dates(state)
            if h in state['hotels']])
    jobs.sort(key=lambda x: (x[2], x[0]))
    total = len(jobs)
    success = failure = alerts = observed = checked = 0
    if total:
        offset = int(state.get('scan_offset', 0)) % total
        jobs = jobs[offset:] + jobs[:offset]
    else:
        offset = 0

    for hotel, location, day in jobs:
        if checked >= MAX_CHECKS:
            logging.info('Per-run cap reached: %s checks', MAX_CHECKS)
            break
        if time.monotonic() - start > RUN_BUDGET:
            logging.warning('Run budget reached; remaining dates will be prioritized next run')
            break
        current = fetch(hotel, location, day)
        checked += 1
        if current is None:
            failure += 1
        else:
            success += 1
            for key, room in current.items():
                previous = state['rooms'].get(key)
                is_available = room['available']
                observed += int(is_available)
                if not is_available:
                    state['pending'].pop(key, None)
                elif previous is not True or key in state['pending']:
                    state['pending'][key] = room
                    save_state(state)
                    if send(alert(room)):
                        state['pending'].pop(key, None)
                        alerts += 1
                        state['alerts'] += 1
                state['rooms'][key] = is_available
            save_state(state)
        if DELAY > 0:
            time.sleep(DELAY)

    if total:
        state['scan_offset'] = (offset + checked) % total
    state['runs'] += 1
    state['failed_checks'] += failure
    state['last_run'] = now.isoformat()
    save_state(state)

    status = ('⏳ <b>Eco Retreat 10-minute status</b>\n'
              f'📅 {now:%d %b %Y %I:%M %p} IST\n'
              f'✅ Successful: {success}/{checked} attempted ({total} planned)\n'
              f'⚠️ Failed/unknown: {failure}\n'
              f'🛏 Available categories observed: {observed}\n'
              f'🚨 New alerts: {alerts}\n'
              + ('⚠️ Partial scan; continuing on next run.' if checked < total else ''))
    if HEARTBEAT and scan_enabled:
        send(status + ('\n🔎 One-off scan (periodic monitoring remains stopped).' if state['paused'] else ''))
    if scan_enabled and now.hour >= SUMMARY_HOUR and state['last_summary'] != now.date().isoformat():
        summary = ('📊 <b>Eco Retreat daily summary</b>\n'
                   f'📅 {now:%d %b %Y}\n'
                   f'Runs recorded (all-time): {state["runs"]}\n'
                   f'Failed/unknown queries (all-time): {state["failed_checks"]}\n'
                   f'Availability alerts (all-time): {state["alerts"]}\n'
                   f'Latest successful: {success}/{checked}, planned: {total}\n'
                   'Note: Empty API inventory is unknown, not booked out.')
        if send(summary):
            state['last_summary'] = now.date().isoformat()
            save_state(state)
    logging.info('Finished: attempted=%s planned=%s success=%s failure=%s alerts=%s',
                 checked, total, success, failure, alerts)


if __name__ == '__main__':
    main()
