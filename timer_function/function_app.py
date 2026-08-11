import logging
import json
import os
import smtplib
import time
import uuid
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import azure.functions as func

app = func.FunctionApp()
REFRESH_HOURS = {12, 19}
REFRESH_TIME_ZONE = os.environ.get('AUTO_REFRESH_TIME_ZONE', 'Europe/Berlin')
BACKEND_REFRESH_REQUEST_TIMEOUT_SECONDS = int(
    os.environ.get('BACKEND_REFRESH_REQUEST_TIMEOUT_SECONDS', '90')
)
REFRESH_STATUS_TIMEOUT_SECONDS = 900


def is_refresh_hour(now=None):
    now = datetime.now(ZoneInfo(REFRESH_TIME_ZONE)) if now is None else now
    return now.hour in REFRESH_HOURS


def refresh_status_url(refresh_url):
    parsed = urlsplit(refresh_url)
    return urlunsplit((parsed.scheme, parsed.netloc, '/internal/refresh-status', '', ''))


def log_change_details(changes, stage):
    price_changed = changes.get('price_changed_listings', [])
    seen = set()
    for index, item in enumerate(price_changed, start=1):
        if not isinstance(item, dict):
            logging.warning(
                'Price-change report %s contains non-object item at index %s: %r',
                stage,
                index,
                item,
            )
            continue
        broker = item.get('broker', '')
        link = item.get('link', '')
        identity = (broker, link or item.get('title', ''))
        duplicate = identity in seen
        seen.add(identity)
        logging.info(
            'Price-change detail stage=%s index=%s duplicate=%s broker=%r title=%r '
            'link=%r old_price=%r new_price=%r communicated=%r communicated_price=%r.',
            stage,
            index,
            duplicate,
            broker,
            item.get('title', ''),
            link,
            item.get('old_price', ''),
            item.get('price', ''),
            item.get('price_change_communicated'),
            item.get('price_change_communicated_price'),
        )


def send_change_email(changes, subject_prefix=''):
    new_listings = changes.get('new_listings', [])
    price_changed = changes.get('price_changed_listings', [])
    if not new_listings and not price_changed:
        return False

    recipients = [item.strip() for item in os.environ.get('EMAIL_RECIPIENTS', '').split(',') if item.strip()]
    if not recipients:
        logging.warning('Listing changes found, but EMAIL_RECIPIENTS is empty.')
        return False

    logging.info(
        'Preparing listing-change email: new=%s price_changes=%s.',
        len(new_listings),
        len(price_changed),
    )
    log_change_details(changes, 'before-email')

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
            changes = payload.get('changes', {})
            log_change_details(changes, 'refresh-complete')
            return changes
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
            logging.info(
                'Price-change acknowledgement response attempt=%s ok=%s acknowledged=%s expected=%s.',
                attempt + 1,
                result.get('ok'),
                result.get('acknowledged'),
                expected_count,
            )
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


@app.timer_trigger(schedule='0 0 10,11,17,18 * * *', arg_name='timer')
def refresh_listings(timer: func.TimerRequest) -> None:
    run_id = uuid.uuid4().hex[:12]
    started_at = time.monotonic()
    now = datetime.now(ZoneInfo(REFRESH_TIME_ZONE))
    logging.info(
        'Automatic listing refresh started: run_id=%s scheduled_local=%s past_due=%s.',
        run_id,
        now.isoformat(),
        getattr(timer, 'past_due', None),
    )
    if not is_refresh_hour(now):
        logging.info(
            'Automatic listing refresh skipped: run_id=%s reason=outside_refresh_hours elapsed_ms=%s.',
            run_id,
            int((time.monotonic() - started_at) * 1000),
        )
        return

    refresh_url = os.environ['BACKEND_REFRESH_URL']
    refresh_token = os.environ['INTERNAL_REFRESH_TOKEN']
    refresh_host = urlsplit(refresh_url).netloc or '<invalid-url>'
    request = Request(
        refresh_url,
        headers={'Authorization': f'Bearer {refresh_token}'},
        method='POST',
    )
    try:
        request_started_at = time.monotonic()
        logging.info(
            'Backend refresh request started: run_id=%s host=%s timeout_seconds=%s.',
            run_id,
            refresh_host,
            BACKEND_REFRESH_REQUEST_TIMEOUT_SECONDS,
        )
        with urlopen(request, timeout=BACKEND_REFRESH_REQUEST_TIMEOUT_SECONDS) as response:
            refresh_response = json.loads(response.read().decode('utf-8'))
            logging.info(
                'Backend refresh request completed: run_id=%s http_status=%s elapsed_ms=%s started=%s.',
                run_id,
                response.status,
                int((time.monotonic() - request_started_at) * 1000),
                refresh_response.get('started'),
            )
        if not refresh_response.get('started'):
            logging.info(
                'Automatic listing refresh ended: run_id=%s reason=refresh_already_active elapsed_ms=%s.',
                run_id,
                int((time.monotonic() - started_at) * 1000),
            )
            return
        refresh_started_at = time.monotonic()
        changes = wait_for_refresh_changes(refresh_url)
        new_count = len(changes.get('new_listings', []))
        price_count = len(changes.get('price_changed_listings', []))
        logging.info(
            'Listing refresh polling completed: run_id=%s elapsed_ms=%s new=%s price_changes=%s.',
            run_id,
            int((time.monotonic() - refresh_started_at) * 1000),
            new_count,
            price_count,
        )
        if send_change_email(changes):
            acknowledged_count = acknowledge_sent_price_changes(refresh_url, changes)
            logging.info(
                'Listing change email sent: run_id=%s new=%s price_changes=%s acknowledged=%s total_elapsed_ms=%s.',
                run_id,
                new_count,
                price_count,
                acknowledged_count,
                int((time.monotonic() - started_at) * 1000),
            )
        else:
            logging.info(
                'No listing change email sent: run_id=%s new=%s price_changes=%s total_elapsed_ms=%s.',
                run_id,
                new_count,
                price_count,
                int((time.monotonic() - started_at) * 1000),
            )
    except smtplib.SMTPException:
        logging.exception(
            'Automatic listing refresh failed: run_id=%s phase=email_delivery elapsed_ms=%s.',
            run_id,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
    except HTTPError:
        logging.exception(
            'Automatic listing refresh failed: run_id=%s phase=backend_http elapsed_ms=%s.',
            run_id,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
    except URLError:
        logging.exception(
            'Automatic listing refresh failed: run_id=%s phase=backend_connection elapsed_ms=%s.',
            run_id,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
    except Exception:
        logging.exception(
            'Automatic listing refresh failed: run_id=%s phase=unexpected elapsed_ms=%s.',
            run_id,
            int((time.monotonic() - started_at) * 1000),
        )
        raise
