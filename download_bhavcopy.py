import os, sys, logging, requests
from datetime import date, timedelta
from io import BytesIO
from zipfile import ZipFile

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

SAVE_DIR = '/home/ubuntu/trading-bot/bhavcopy'
os.makedirs(SAVE_DIR, exist_ok=True)

def download_bhavcopy(target_date=None):
    if target_date is None:
        target_date = date.today()

    # NSE bhavcopy URL format
    dt = target_date
    day_str = dt.strftime('%d%b%Y').upper()
    mon_str = dt.strftime('%b').upper()
    year_str = dt.strftime('%Y')

    # Try multiple URL patterns
    urls = [
        f'https://nsearchives.nseindia.com/content/historical/EQUITIES/{year_str}/{mon_str}/cm{day_str}bhav.csv.zip',
        f'https://www1.nseindia.com/content/historical/EQUITIES/{year_str}/{mon_str}/cm{day_str}bhav.csv.zip',
    ]

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    session = requests.Session()
    # First visit NSE to get cookies
    try:
        session.get('https://www.nseindia.com', headers=headers, timeout=10)
    except:
        pass

    for url in urls:
        try:
            log.info(f'Trying: {url}')
            resp = session.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                zf = ZipFile(BytesIO(resp.content))
                csv_name = zf.namelist()
                save_path = os.path.join(SAVE_DIR, f'{dt.strftime("%Y-%m-%d")}.csv')
                with open(save_path, 'wb') as f:
                    f.write(zf.read(csv_name))
                log.info(f'Saved: {save_path}')
                return save_path
        except Exception as e:
            log.warning(f'Failed: {e}')
            continue

    log.error(f'Could not download bhavcopy for {dt}')
    return None

if __name__ == '__main__':
    # If arg provided, use that date, else today
    if len(sys.argv) > 1:
        dt = date.fromisoformat(sys.argv)
    else:
        dt = date.today()

    result = download_bhavcopy(dt)
    if result:
        print(f'SUCCESS: {result}')
    else:
        print('FAILED: Could not download')
