import logging
import json
import os
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import azure.functions as func

app = func.FunctionApp()
REFRESH_START_HOUR = 6
REFRESH_END_HOUR = 20
REFRESH_TIME_ZONE = os.environ.get('AUTO_REFRESH_TIME_ZONE', 'Europe/Berlin')
REFRESH_STATUS_TIMEOUT_SECONDS = 900


def is_refresh_hour(now=None):
    now = datetime.now(ZoneInfo(REFRESH_TIME_ZONE)) if now is None else now
    return REFRESH_START_HOUR <= now.hour < REFRESH_END_HOUR


def refresh_status_url(refresh_url):
    parsed = urlsplit(refresh_url)
    return urlunsplit((parsed.scheme, parsed.netloc, '/internal/refresh-status', '', ''))


def send_change_email(changes, subject_prefix=''):
    new_listings = changes.get('new_listings', [])
    price_changed = changes.get('price_changed_listings', [])
    if not new_listings and not price_changed:
        return False

    recipients = [item.strip() for item in os.environ.get('EMAIL_RECIPIENTS', '').split(',') if item.strip()]
    if not recipients:
        logging.warning('Listing changes found, but EMAIL_RECIPIENTS is empty.')
        return False

    def listing_line(item, price_label='Preis'):
        title = item.get('title') or 'Immobilie'
        link = item.get('link') or ''
        price = item.get('price') or 'Preis auf Anfrage'
        location = item.get('location') or ''
        area = item.get('area_sqm') or ''
        details = ', '.join(value for value in (location, f'{area} m²' if area else '') if value)
        suffix = f' ({details})' if details else ''
        old_price = f"; vorher {item['old_price']}" if item.get('old_price') else ''
        return f'- {title}{suffix}: {price_label} {price}{old_price}\n  {link}'

    sections = []
    if new_listings:
        sections.append('Neue Angebote:\n' + '\n'.join(listing_line(item) for item in new_listings))
    if price_changed:
        sections.append('Preisänderungen:\n' + '\n'.join(listing_line(item, 'Neuer Preis') for item in price_changed))
    body = 'Beim letzten Immobilien-Refresh wurden Änderungen gefunden.\n\n' + '\n\n'.join(sections)

    message = EmailMessage()
    sender = os.environ['EMAIL_FROM_ADDRESS']
    message['From'] = formataddr(('Makler Search', sender))
    message['To'] = ', '.join(recipients)
    subject = f"Immobilien-Update: {len(new_listings)} neue, {len(price_changed)} Preisänderungen"
    message['Subject'] = f'{subject_prefix}{subject}'
    message.set_content(body)

    host = os.environ['EMAIL_SMTP_HOST']
    port = int(os.environ.get('EMAIL_SMTP_PORT', '587'))
    username = os.environ.get('EMAIL_SMTP_USERNAME', '')
    password = os.environ.get('EMAIL_SMTP_PASSWORD', '')
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        if username:
            smtp.login(username, password)
        smtp.send_message(message, from_addr=sender, to_addrs=recipients)
    return True


def wait_for_refresh_changes(refresh_url):
    status_url = refresh_status_url(refresh_url)
    token = os.environ.get('INTERNAL_REFRESH_TOKEN', '')
    deadline = time.monotonic() + REFRESH_STATUS_TIMEOUT_SECONDS
    poll_count = 0
    while time.monotonic() < deadline:
        request = Request(status_url, headers={'Authorization': f'Bearer {token}'})
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode('utf-8'))
        poll_count += 1
        if poll_count == 1 or poll_count % 12 == 0 or not payload.get('active'):
            logging.info(
                'Listing refresh status: active=%s, error=%s, new=%s, price_changes=%s.',
                payload.get('active'),
                payload.get('error'),
                len(payload.get('changes', {}).get('new_listings', [])),
                len(payload.get('changes', {}).get('price_changed_listings', [])),
            )
        if not payload.get('active'):
            if payload.get('error'):
                raise RuntimeError(payload['error'])
            return payload.get('changes', {})
        time.sleep(5)
    raise TimeoutError(
        f'Timed out waiting {REFRESH_STATUS_TIMEOUT_SECONDS}s for the listing refresh to finish.'
    )


def acknowledge_sent_price_changes(refresh_url, changes):
    acknowledged_url = refresh_url.rsplit('/', 1)[0] + '/refresh-email-sent'
    token = os.environ['INTERNAL_REFRESH_TOKEN']
    expected_count = len({
        (item.get('broker', ''), item.get('link', '') or item.get('title', ''))
        for item in changes.get('price_changed_listings', [])
        if isinstance(item, dict)
    })
    payload = json.dumps({
        'price_changed_listings': changes.get('price_changed_listings', []),
    }).encode('utf-8')
    last_error = None
    for attempt in range(3):
        request = Request(
            acknowledged_url,
            data=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
            if not result.get('ok'):
                raise RuntimeError('Backend rejected the sent price-change acknowledgement.')
            acknowledged = result.get('acknowledged', 0)
            if acknowledged != expected_count:
                raise RuntimeError(
                    f'Backend acknowledged {acknowledged} of {expected_count} sent price changes.'
                )
            return acknowledged
        except (HTTPError, URLError, RuntimeError, ValueError) as error:
            last_error = error
            if attempt < 2:
                logging.warning(
                    'Price-change acknowledgement attempt %s failed: %s',
                    attempt + 1,
                    error,
                )
                time.sleep(2 ** attempt)
    raise last_error


@app.timer_trigger(schedule='0 0 * * * *', arg_name='timer')
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
            refresh_response = json.loads(response.read().decode('utf-8'))
            logging.info(
                'Automatic listing refresh accepted with HTTP %s.',
                response.status,
            )
        if not refresh_response.get('started'):
            logging.info('Listing refresh was already active; email delivery belongs to the active refresh.')
            return
        changes = wait_for_refresh_changes(refresh_url)
        new_count = len(changes.get('new_listings', []))
        price_count = len(changes.get('price_changed_listings', []))
        logging.info(
            'Automatic listing refresh completed with %s new listings and %s price changes.',
            new_count,
            price_count,
        )
        if send_change_email(changes):
            acknowledged_count = acknowledge_sent_price_changes(refresh_url, changes)
            logging.info('Acknowledged %s sent price changes.', acknowledged_count)
            logging.info('Listing change email sent.')
        else:
            logging.info('No listing change email sent because the change report was empty or recipients were missing.')
    except smtplib.SMTPException:
        logging.exception('Listing change email failed during SMTP delivery.')
        raise
    except HTTPError:
        logging.exception('Backend rejected the automatic listing refresh.')
        raise
    except URLError:
        logging.exception('Backend was unreachable for the automatic listing refresh.')
        raise
