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
HOTELS = {'37': 'Konark', '41': 'Satkosia', '43': 'Sonapur'}
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
RUN_BUDGET = float(os.getenv('RUN_BUDGET_SECONDS', '480'))
DEBUG_API = os.getenv('DEBUG_API_RESPONSE', '0') == '1'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0',
    'Origin': 'https://www.bookodisha.com',
    'Referer': 'https://www.bookodisha.com/',
})


def checkin_dates():
    day = max(FIRST_DATE, datetime.now(IST).date())
    while day <= LAST_DATE:
        yield day
        day += timedelta(days=1)


def fresh_state():
    return {'rooms': {}, 'pending': {}, 'last_summary': None,
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


def fetch(hotel, day):
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
                    'location': HOTELS[hotel], 'date': date_text,
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


def main():
    state = load_state()
    start = time.monotonic()
    now = datetime.now(IST)
    jobs = [(h, day) for h in HOTELS for day in checkin_dates()]
    jobs.sort(key=lambda x: (x[1], x[0]))
    total = len(jobs)
    success = failure = alerts = observed = checked = 0
    if total:
        offset = int(state.get('scan_offset', 0)) % total
        jobs = jobs[offset:] + jobs[:offset]
    else:
        offset = 0

    for hotel, day in jobs:
        if time.monotonic() - start > RUN_BUDGET:
            logging.warning('Run budget reached; remaining dates will be prioritized next run')
            break
        current = fetch(hotel, day)
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
    if HEARTBEAT:
        send(status)
    if now.hour >= SUMMARY_HOUR and state['last_summary'] != now.date().isoformat():
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
