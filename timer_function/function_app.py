import logging
import os
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import azure.functions as func

app = func.FunctionApp()
REFRESH_START_HOUR = 6
REFRESH_END_HOUR = 20
REFRESH_TIME_ZONE = os.environ.get('AUTO_REFRESH_TIME_ZONE', 'Europe/Berlin')


def is_refresh_hour(now=None):
    now = datetime.now(ZoneInfo(REFRESH_TIME_ZONE)) if now is None else now
    return REFRESH_START_HOUR <= now.hour < REFRESH_END_HOUR


@app.timer_trigger(
    schedule='0 0 * * * *',
    arg_name='timer',
    run_on_startup=False,
    use_monitor=True,
)
def refresh_listings(timer: func.TimerRequest) -> None:
    now = datetime.now(ZoneInfo(REFRESH_TIME_ZONE))
    if not is_refresh_hour(now):
        logging.info('Automatic listing refresh skipped at %s.', now.isoformat())
        return

    refresh_url = os.environ['BACKEND_REFRESH_URL']
    refresh_token = os.environ['INTERNAL_REFRESH_TOKEN']
    request = Request(
        refresh_url,
        headers={'Authorization': f'Bearer {refresh_token}'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=30) as response:
            logging.info(
                'Automatic listing refresh accepted with HTTP %s.',
                response.status,
            )
    except HTTPError:
        logging.exception('Backend rejected the automatic listing refresh.')
        raise
    except URLError:
        logging.exception('Backend was unreachable for the automatic listing refresh.')
        raise
