import os, json, logging, time, html
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

API = 'https://admin.bookodisha.com/api/auth/hotel_details'
HOTELS = {'37':'Konark','41':'Satkosia','43':'Sonapur'}
DATES = ('2026-12-22','2026-12-23')
STATE = Path('state.json')
IST = ZoneInfo('Asia/Kolkata')
TOKEN = os.environ['TG_TOKEN']
CHAT_ID = os.environ['TG_CHAT_ID']
# This mapping MUST be checked against a live known available/unavailable API response.
AVAILABLE_VALUE = os.getenv('AVAILABLE_STATUS', '1')
HEARTBEAT = os.getenv('HEARTBEAT_EVERY_RUN','1') == '1'
SUMMARY_HOUR = int(os.getenv('DAILY_SUMMARY_HOUR_IST','20'))
S = requests.Session()
S.headers.update({'User-Agent':'Mozilla/5.0','Origin':'https://www.bookodisha.com','Referer':'https://www.bookodisha.com/'})
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')

def send(message):
    url=f'https://api.telegram.org/bot{TOKEN}/sendMessage'
    for attempt in range(3):
        try:
            r=S.post(url,data={'chat_id':CHAT_ID,'text':message,'parse_mode':'HTML'},timeout=20)
            if r.status_code == 429:
                try: pause=int(r.json().get('parameters',{}).get('retry_after',5))
                except (ValueError,TypeError): pause=5
                time.sleep(min(60,max(1,pause))); continue
            r.raise_for_status()
            if r.json().get('ok') is True: return True
            logging.error('Telegram rejected message: %s',r.text[:400]); return False
        except (requests.RequestException,ValueError) as exc:
            logging.warning('Telegram send failed attempt %s: %s',attempt+1,exc)
            if attempt<2:time.sleep(2**attempt)
    return False

def load():
    if not STATE.exists():
        logging.warning('State missing: initializing. Verify previous artifact restoration if not first run.')
        return {'rooms':{},'pending':{},'last_summary':None,'runs':0,'failed_checks':0,'alerts':0,'last_run':None}
    data=json.loads(STATE.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or not isinstance(data.get('rooms'),dict):
        raise ValueError('Invalid state file')
    for k,v in {'pending':{},'last_summary':None,'runs':0,'failed_checks':0,'alerts':0,'last_run':None}.items():data.setdefault(k,v)
    return data

def save(data):
    tmp=STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')
    tmp.replace(STATE)

def available(value):
    if isinstance(value,bool): return value == (AVAILABLE_VALUE.lower() in ('1','true'))
    if str(value) == AVAILABLE_VALUE: return True
    if str(value).lower() in ('0','false') and AVAILABLE_VALUE == '1':return False
    if str(value).lower() in ('1','true') and AVAILABLE_VALUE == '0':return False
    raise ValueError(f'Unknown booking_status={value!r}')

def fetch(hotel,date):
    checkout=(datetime.strptime(date,'%Y-%m-%d')+timedelta(days=1)).strftime('%Y-%m-%d')
    payload={'request_type':'check_room_availability','requested_room':'all','hotelId':hotel,'roomRequest':'[{"adult":2,"child":0}]','checkinDate':date,'checkoutDate':checkout}
    for attempt in range(3):
        try:
            r=S.post(API,data=payload,timeout=20)
            r.raise_for_status()
            body=r.json()
            if not isinstance(body,dict) or not isinstance(body.get('data'),list):raise ValueError('Unexpected API response; expected data list')
            rooms=body['data']
            if not rooms:raise ValueError('Empty room list; treating as unknown, not unavailable')
            result={}
            for room in rooms:
                if not isinstance(room,dict) or room.get('id') is None:raise ValueError('Invalid room item')
                result[f'{hotel}|{date}|{room["id"]}']={'available':available(room.get('booking_status')),'hotel':hotel,'location':HOTELS[hotel],'date':date,'room':str(room.get('title','Unknown')),'price':str(room.get('price','Unknown')),'qty':str(room.get('quantity','Unknown'))}
            return result
        except (requests.RequestException,ValueError) as exc:
            logging.warning('Fetch %s %s attempt %s: %s',hotel,date,attempt+1,exc)
            if attempt<2:time.sleep(2**attempt)
    return None

def alert(room):
    return ('🚨 <b>ECO RETREAT: NEW AVAILABILITY</b>\n'
        f'📍 {html.escape(room["location"])}\n📅 {room["date"]}\n'
        f'🛏 {html.escape(room["room"])}\n💰 ₹{html.escape(room["price"])}\n'
        f'📦 Quantity: {html.escape(room["qty"])}\n'
        '🔗 https://www.bookodisha.com/\n'
        'Please confirm the room is bookable on the website.')

def main():
    state=load(); now=datetime.now(IST); success=0; failure=0; new_alerts=0; observed_available=0
    for hotel in HOTELS:
        for date in DATES:
            current=fetch(hotel,date)
            if current is None:
                failure+=1;continue
            success+=1
            # Only update rooms actually seen; failed or empty responses must not reset status.
            for key,room in current.items():
                previous=state['rooms'].get(key)
                is_avail=room['available']
                observed_available+=int(is_avail)
                # If an earlier Telegram delivery failed, retry on this run.
                should_alert=is_avail and (previous is not True or key in state['pending'])
                if should_alert:
                    state['pending'][key]=room
                    # Persist in memory before sending; workflow uploads state afterward.
                    if send(alert(room)):
                        state['pending'].pop(key,None)
                        new_alerts+=1;state['alerts']+=1
                else:
                    if not is_avail:state['pending'].pop(key,None)
                state['rooms'][key]=is_avail
            time.sleep(0.5)
    state['runs']+=1;state['failed_checks']+=failure;state['last_run']=now.isoformat()
    # Heartbeat after each 10-minute check, as requested. Disable via repository variable.
    if HEARTBEAT:
        send(f'⏳ <b>Eco Retreat 10-minute status</b>\n📅 {now:%d %b %Y %I:%M %p} IST\n✅ Successful checks: {success}/6\n⚠️ Failed checks: {failure}\n🛏 Available categories: {observed_available}\n🚨 New alerts: {new_alerts}')
    if now.hour>=SUMMARY_HOUR and state['last_summary']!=now.date().isoformat():
        if send(f'📊 <b>Eco Retreat daily summary</b>\n📅 {now:%d %b %Y}\nChecks executed (runs): {state["runs"]}\nFailed date/hotel queries (cumulative): {state["failed_checks"]}\nAvailability alerts delivered (cumulative): {state["alerts"]}\nLatest available categories: {observed_available}\nLatest failed checks: {failure}'):
            state['last_summary']=now.date().isoformat()
    save(state)
    logging.info('Success %d failure %d available %d alerts %d',success,failure,observed_available,new_alerts)

if __name__=='__main__':main()
