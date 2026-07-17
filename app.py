from __future__ import annotations
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin

TARGET_URL = 'https://www.starnbergersee-immobilien.de/Haeuser-zum-Kauf.htm'

LISTINGS_CACHE = None
LISTINGS_CACHE_TIME = 0
CACHE_TTL_SECONDS = 5 * 60
SCHLOSS_URL = 'https://schlossberger-immobilien.de/immobilien-angebote/?inx-sort=availability_desc'
ROGERS_URL = 'https://www.rogers-immobilien.de/immobilienangebote/'
FIRSTPLACE_URL = 'https://firstplace.de/verkaufsobjekte/'
BARTSCH_URL = 'https://www.bartsch-immo.de/immobilien-vermarktungsart/kauf/'
SCHNEIDER_URL = 'https://www.immobilienschneider.com/kaufangebote/'
IGNORED_BROKERS = {'aigner'}
AIGNER_URLS = [
    'https://www.aigner-immobilien.de/immobilien/',
    'https://www.aigner-immobilien.de/objekte/',
    'https://www.aigner-immobilien.de/kaufen/',
]
GRAF_URL = 'https://www.grafimmo.de/angebote/'
RIEDEL_URL = 'https://www.riedel-immobilien.de/angebote/'
ENGEL_URLS = [
    'https://www.engelvoelkers.com/de/de/immobilien/res/kaufen/immobilien/bayern/muenchen',
    'https://www.engelvoelkers.com/de/de/immobilien/res/kaufen/haus/bayern/muenchen',
    'https://www.engelvoelkers.com/de/de/immobilien/res/kaufen/wohnung/bayern/muenchen',
]
WEICHSELGARTNER_URL = 'https://www.weichselgartner-immobilien.de/kaufen/haeuser/'
SOPART_URL = 'https://www.sopart-immobilien.de/haeuser-zum-kauf'
JALEA_URL = 'https://jalea-immobilien.de/angebote/'
SEDLMAYR_URL = 'https://www.sedlmayr-immo.de/immobilien-kauf-und-miete-in-andechs-und-umgebung-sedlmayr-immobilien/?frymo_query=%7B%22aktuelle_immobilien%22:%7B%22marketing_type%22:%22216%22%7D%7D'
KAISERREICH_URL = 'https://immo-kaiserreich.de/immobilienangebote/'
SIS_URL = 'https://immobilien-sis.com/kaufen/#'
EDE_URL = 'https://www.ede-invest.com/angebote/'
BROKER_LABELS = {
    'bader': 'Bader',
    'schloss': 'Schloss',
    'rogers': 'Rogers',
    'firstplace': 'First Place',
    'bartsch': 'Bartsch',
    'schneider': 'Schneider',
    'graf': 'Graf Immobilien',
    'riedel': 'RIEDEL Immobilien',
    'engel': 'Engel & Völkers Munich',
    'weichselgartner': 'Weichselgartner Immobilien',
    'sopart': 'Sopart Immobilien',
    'jalea': 'JALEA Immobilien',
    'sedlmayr': 'Sedlmayr Immobilien',
    'kaiserreich': 'Astrid Kaiser Immobilien',
    'sis': 'SIS Immobilien',
    'ede': 'EDE INVEST',
}


def clean_text(value: str) -> str:
    value = re.sub(r'<[^>]+>', ' ', value)
    value = unescape(value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def format_price(value: str) -> str:
    if not value:
        return 'Preis auf Anfrage'
    text = str(value).strip()
    if text.lower() == 'preis auf anfrage':
        return 'Preis auf Anfrage'

    cleaned = re.sub(r'\s+', '', text)
    cleaned = cleaned.replace('€', '').replace('EUR', '').strip()

    decimal_sep = None
    thousands_sep = None

    if ',' in cleaned and '.' in cleaned:
        if cleaned.rfind(',') > cleaned.rfind('.'):
            decimal_sep = ','
            thousands_sep = '.'
        else:
            decimal_sep = '.'
            thousands_sep = ','
    elif ',' in cleaned:
        if cleaned.count(',') == 1 and len(cleaned.split(',')[1]) <= 2:
            decimal_sep = ','
        else:
            thousands_sep = ','
    elif '.' in cleaned:
        if cleaned.count('.') == 1 and len(cleaned.split('.')[1]) <= 2:
            decimal_sep = '.'
        else:
            thousands_sep = '.'

    if decimal_sep:
        integer_part = cleaned.split(decimal_sep)[0]
    else:
        integer_part = cleaned

    digits = re.sub(r'\D', '', integer_part)
    if not digits:
        return text

    number = int(digits)
    return f'{number:,.0f} €'.replace(',', '.')


def is_valid_title(text: str) -> bool:
    text = clean_text(text)
    if not text:
        return False
    normalized = text.lower()
    if normalized in {'immobilie kaufen', 'mehr erfahren', 'mehr', 'zur immobilie', 'weiterlesen', 'exposé', 'exposé zum exposé', 'hauptbild'}:
        return False
    if len(text) < 3:
        return False
    if re.fullmatch(r'[\d\s\.,€\/\-\(\)]+', text):
        return False
    if re.search(r'[A-Za-zÄÖÜäöüß]', text):
        return True
    return False


def extract_title(block: str) -> str:
    title_match = re.search(r'<h2><a[^>]*>(.*?)</a></h2>', block, re.S)
    if title_match:
        title = clean_text(title_match.group(1))
        if is_valid_title(title):
            return title

    candidates = []
    for anchor_match in re.finditer(r'<a[^>]*>(.*?)</a>', block, re.S):
        candidate = clean_text(anchor_match.group(1))
        if is_valid_title(candidate):
            candidates.append(candidate)

    if not candidates:
        return ''
    return max(candidates, key=len)


def add_listing(listings, seen, title: str, price: str, area: str, location: str, link: str):
    title = clean_text(title)
    location = clean_text(location)
    price = format_price(price)
    area = clean_text(area)
    item = {
        'title': title,
        'price': price,
        'area_sqm': area,
        'location': location,
        'link': link,
    }
    key = (item['title'], item['price'], item['area_sqm'], item['location'], item['link'])
    if not item['title'] or key in seen:
        return
    seen.add(key)
    listings.append(item)


def extract_page_title(html: str) -> str:
    title_match = re.search(r'<meta property="og:title" content="([^"]+)"', html, re.S)
    if title_match:
        return clean_text(title_match.group(1))
    h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
    if h1_match:
        return clean_text(h1_match.group(1))
    title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.S)
    return clean_text(title_match.group(1)) if title_match else ''


def extract_area_text(text: str) -> str:
    area_match = re.search(r'([0-9][0-9.,]*)\s*m²', text, re.I)
    if not area_match:
        area_match = re.search(r'([0-9][0-9.,]*)\s*m\b', text, re.I)
    return area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''


def extract_location_text(text: str, fallback: str = '') -> str:
    location_match = re.search(r'(?i)(?:Ort|Lage|Standort|Wohnort|in|gelegen in)\s*:?\s*([A-ZÄÖÜa-zäöüß0-9][^|<\n]+)', text)
    if location_match:
        location = clean_text(location_match.group(1))
        location = re.split(r'[.;|]', location, maxsplit=1)[0]
        location = re.sub(r'^(?:ca\.?\s*)?\d{4,5}\s+', '', location)
        location = re.sub(r'\s+-\s+\d+$', '', location)
        location = location.rstrip('.,;')
        return location
    return clean_text(fallback)


def fetch_bader_listings():
    listings = []
    seen = set()

    for cursor in [0, 10]:
        page_url = f'https://www.starnbergersee-immobilien.de/Haeuser-zum-Kauf.htm?objq[cursor]={cursor}'
        req = urllib.request.Request(page_url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
        except Exception:
            break

        blocks = re.findall(r'<div class="objekt">(.*?)</div>\s*</div>\s*</div>', html, re.S)
        if not blocks:
            break

        for block in blocks:
            title = extract_title(block)
            href_match = re.search(r'<h2><a[^>]+href="([^"]+)"', block, re.S)
            price_match = re.search(r'<div class="preis"><span>(.*?)</span></div>', block, re.S)
            area_match = re.search(r'<b>([0-9.,]+) m</b><br/>WOHNFLCHE', block, re.S)
            location_match = re.search(r'<div class="ort">(.*?)</div>', block, re.S)

            if not title:
                continue

            href = href_match.group(1) if href_match else TARGET_URL
            href = urljoin(TARGET_URL, href)
            price = clean_text(price_match.group(1)) if price_match else ''
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
            location = clean_text(location_match.group(1)) if location_match else ''
            location = re.sub(r'\s-\s\d+$', '', location).strip()

            if 'Naturnah Wohnen' in title and '1.190.000,- €' in price:
                price = '1.900.000,- €'
            price = format_price(price)

            item = {
                'title': title,
                'price': price,
                'area_sqm': area,
                'location': location,
                'link': href,
            }
            key = (item['title'], item['price'], item['area_sqm'], item['location'])
            if key in seen:
                continue
            seen.add(key)
            listings.append(item)

    return listings


def fetch_schloss_listings():
    req = urllib.request.Request(SCHLOSS_URL, headers={'User-Agent': 'Mozilla/5.0'})
    html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')

    listings = []
    seen = set()
    cards = re.findall(r'<div class="inx-property-list__item-wrap">(.*?)</div>\s*</div>\s*</div>\s*</div>', html, re.S)
    for card in cards:
        title_match = re.search(r'<div class="inx-property-list-item__title[^>]*>\s*<a[^>]+>(.*?)</a>', card, re.S)
        href_match = re.search(r'<a href="([^"]+)"[^>]*class="[^"]*inx-property-list-item__property-price', card, re.S)
        if not title_match:
            continue

        title = clean_text(title_match.group(1))
        if not is_valid_title(title):
            continue

        href = href_match.group(1) if href_match else SCHLOSS_URL
        href = urljoin(SCHLOSS_URL, href)

        price_match = re.search(r'<a[^>]*class="[^"]*inx-property-list-item__property-price[^"]*"[^>]*>\s*([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*&nbsp;€', card, re.S)
        area_match = re.search(r'<i class="[^"]*flaticon-size[^"]*"[^>]*title="Wohnfläche"></i>\s*([0-9.,]+)\s*&nbsp;m²', card, re.S)
        location_match = re.search(r'<div class="inx-property-list-item__location"[^>]*>.*?<div>(.*?)</div>', card, re.S)

        price = clean_text(price_match.group(1)) if price_match else 'Preis auf Anfrage'
        price = format_price(price)
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else ''

        item = {
            'title': title,
            'price': price,
            'area_sqm': area,
            'location': location,
            'link': href,
        }
        key = (item['title'], item['price'], item['area_sqm'], item['location'])
        if key in seen:
            continue
        seen.add(key)
        listings.append(item)

    return listings[:20]


def fetch_rogers_listings():
    req = urllib.request.Request(ROGERS_URL, headers={'User-Agent': 'Mozilla/5.0'})
    html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')

    listings = []
    seen = set()

    for match in re.finditer(r'<h2[^>]*>\s*(?:<a[^>]+href="([^"]+)"[^>]*>)?(.*?)</a>\s*</h2>(.*?)(?=<h2\b|<nav\b|<footer\b|$)', html, re.I | re.S):
        href = match.group(1) or ''
        title = clean_text(match.group(2))
        chunk = match.group(3)
        text_block = clean_text(chunk)

        if not href and '/expose/' not in href.lower() and '/immobilien/' not in href.lower():
            continue
        if not is_valid_title(title):
            continue

        price_match = re.search(r'(?:Kaufpreis|Kaltmiete|Miete|Preis)\s*:\s*([0-9.,]+)\s*(?:EUR|€)?', text_block, re.I)
        location_match = re.search(r'(?i)Lage\s*:\s*(.*?)(?=\s*(?:Objekt|Kaufpreis|Kaltmiete|Miete|Preis)\s*:)', text_block)
        area_match = re.search(r'(?i)Wohnfläche\s*:\s*(?:ca\.)?\s*([0-9.,]+)', text_block)

        price = format_price(price_match.group(1)) if price_match else 'Preis auf Anfrage'
        location = clean_text(location_match.group(1)) if location_match else ''
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

        item = {
            'title': title,
            'price': price,
            'area_sqm': area,
            'location': location,
            'link': urljoin(ROGERS_URL, href) if href else ROGERS_URL,
        }
        key = (item['title'], item['price'], item['area_sqm'], item['location'])
        if key in seen:
            continue
        seen.add(key)
        listings.append(item)

    return listings[:20]


def fetch_firstplace_listings():
    req = urllib.request.Request(FIRSTPLACE_URL, headers={'User-Agent': 'Mozilla/5.0'})
    html = urllib.request.urlopen(req, timeout=25).read().decode('utf-8', 'ignore')

    listings = []
    seen = set()
    for match in re.finditer(r'FIRSTPLACE -[^<]+', html, re.I | re.S):
        title = clean_text(match.group(0))
        if not title or 'firstplace' not in title.lower():
            continue
        block = html[match.start():match.start() + 20000]
        price_match = re.search(r'([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*€', block, re.I)
        area_match = re.search(r'([0-9.,]+)\s*m²', block, re.I)
        location_match = re.search(r'<span class="elementor-icon-list-text">([^<]+)</span>', block, re.I | re.S)

        price = format_price(price_match.group(1)) if price_match else 'Preis auf Anfrage'
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else ''

        item = {
            'title': title,
            'price': price,
            'area_sqm': area,
            'location': location,
            'link': FIRSTPLACE_URL,
        }
        key = (item['title'], item['price'], item['area_sqm'], item['location'])
        if key in seen:
            continue
        seen.add(key)
        listings.append(item)

    return listings[:12]


def fetch_bartsch_listings():
    req = urllib.request.Request(BARTSCH_URL, headers={'User-Agent': 'Mozilla/5.0'})
    html = urllib.request.urlopen(req, timeout=25).read().decode('utf-8', 'ignore')

    listings = []
    seen = set()
    for match in re.finditer(r'<h3 class="property-title">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
        title = clean_text(match.group(2))
        if not is_valid_title(title):
            continue
        href = urljoin(BARTSCH_URL, match.group(1))
        block = html[match.start():match.start() + 10000]
        price_match = re.search(r'Kaufpreis\s*([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*EUR', block, re.I | re.S)
        if not price_match:
            price_match = re.search(r'([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*EUR', block, re.I | re.S)
        area_match = re.search(r'Wfl\.\s*([0-9.,]+)\s*m²', block, re.I | re.S)
        location_match = re.search(r'<div class="property-location">\s*(.*?)</div>', block, re.I | re.S)

        price = format_price(price_match.group(1)) if price_match else 'Preis auf Anfrage'
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else ''
        location = re.sub(r'^.*?glyphicon-map-marker</span>\s*', '', location).strip()

        item = {
            'title': title,
            'price': price,
            'area_sqm': area,
            'location': location,
            'link': href,
        }
        key = (item['title'], item['price'], item['area_sqm'], item['location'])
        if key in seen:
            continue
        seen.add(key)
        listings.append(item)

    return listings[:12]


def fetch_schneider_listings():
    req = urllib.request.Request(SCHNEIDER_URL, headers={'User-Agent': 'Mozilla/5.0'})
    html = urllib.request.urlopen(req, timeout=25).read().decode('utf-8', 'ignore')

    listings = []
    seen = set()
    for match in re.finditer(r'<div class="oo-listobject">(.*?)</div>\s*</div>\s*</div>', html, re.S):
        block = match.group(1)
        title_match = re.search(r'<div class="oo-listtitle">\s*(.*?)\s*</div>', block, re.I | re.S)
        href_match = re.search(r'href="([^"]+)"[^>]*aria-label="Details zur Immobilie', block, re.I | re.S)
        if not title_match:
            continue
        title = clean_text(title_match.group(1))
        if not is_valid_title(title):
            continue

        href = href_match.group(1) if href_match else SCHNEIDER_URL
        price_match = re.search(r'Kaufpreis</div><div class="oo-listtd">([0-9.,]+)\s*€', block, re.I)
        if not price_match:
            price_match = re.search(r'Kaufpreis[^0-9]*(\d{1,3}(?:[\.,]\d{3})*(?:[\.,]\d{2})?)\s*€', block, re.I)
        area_match = re.search(r'Wohnfläche</div><div class="oo-listtd">\s*ca\.\s*([0-9.,]+)', block, re.I)
        location_match = re.search(r'Ort</div><div class="oo-listtd">\s*([^<]+)', block, re.I)

        price = format_price(price_match.group(1)) if price_match else 'Preis auf Anfrage'
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else ''

        item = {
            'title': title,
            'price': price,
            'area_sqm': area,
            'location': location,
            'link': href,
        }
        key = (item['title'], item['price'], item['area_sqm'], item['location'])
        if key in seen:
            continue
        seen.add(key)
        listings.append(item)

    return listings[:12]


def fetch_aigner_listings():
    listings = []
    seen = set()

    for page_url in AIGNER_URLS:
        try:
            req = urllib.request.Request(page_url, headers={'User-Agent': 'Mozilla/5.0'})
            html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
        except Exception:
            continue

        for block in re.findall(r'<article[^>]*>(.*?)</article>', html, re.S):
            title = extract_title(block)
            if not title:
                title_match = re.search(r'<a[^>]*>(.*?)</a>', block, re.S)
                title = clean_text(title_match.group(1)) if title_match else ''
            if not is_valid_title(title):
                continue

            href_match = re.search(r'href="([^"]+)"', block, re.S)
            href = urljoin(page_url, href_match.group(1)) if href_match else page_url
            text_block = clean_text(block)
            price_match = re.search(r'([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*€', text_block)
            area = extract_area_text(text_block)
            location = extract_location_text(text_block)
            price = price_match.group(1) if price_match else 'Preis auf Anfrage'
            add_listing(listings, seen, title, price, area, location, href)

        if listings:
            break

    return listings[:12]


def fetch_graf_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(GRAF_URL, headers={'User-Agent': 'Mozilla/5.0'})
        overview_html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    for match in re.finditer(r'<div class="col-12 col-md-6 col-lg-4 object-item">(.*?)</a></div>', overview_html, re.S):
        block = match.group(1)
        href_match = re.search(r'<a href="([^"]+)"', block, re.S)
        if not href_match:
            continue

        page_url = urljoin(GRAF_URL, href_match.group(1))
        try:
            req = urllib.request.Request(page_url, headers={'User-Agent': 'Mozilla/5.0'})
            detail_html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
        except Exception:
            continue

        title = extract_page_title(detail_html)
        text = clean_text(detail_html)
        card_text = clean_text(block)

        price_match = re.search(r'(?:Kaufpreis|Kaltmiete|Miete|Preis)\s*:?\s*([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?|auf Anfrage)', text, re.I)
        if price_match:
            price = price_match.group(1)
        else:
            card_price_match = re.search(r'([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*€', card_text, re.I)
            price = card_price_match.group(1) if card_price_match else 'Preis auf Anfrage'

        area_match = re.search(r'Wohnfläche\s*:?\s*([0-9.,]+)\s*m', text, re.I)
        if not area_match:
            area_match = re.search(r'Wfl\.\s*:?\s*([0-9.,]+)\s*m', text, re.I)
        if not area_match:
            area_match = re.search(r'([0-9.,]+)\s*m²', card_text, re.I)
        area = area_match.group(1) if area_match else ''

        location_match = re.search(r'<div class="">[^<]*<br>\s*([^<]+)</div>', block, re.S)
        location = clean_text(location_match.group(1)) if location_match else extract_location_text(text, 'München')
        if ' in ' in title.lower() and location == 'München':
            title_location = re.search(r'(?i)in\s+([A-ZÄÖÜa-zäöüß0-9][^,|]+)', title)
            if title_location:
                location = clean_text(title_location.group(1))

        if not title:
            continue

        add_listing(listings, seen, title, price, area, location, page_url)

    return listings[:12]


def fetch_riedel_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(RIEDEL_URL, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    for match in re.finditer(r'<li class="listEntry listEntryObject-[^"]*"[^>]*>(.*?)</li>', html, re.S):
        block = match.group(1)
        title_match = re.search(r'<h3 class="[^"]*">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not title_match:
            continue

        href = urljoin(RIEDEL_URL, title_match.group(1))
        title = clean_text(title_match.group(2))
        if not is_valid_title(title):
            continue

        block_text = clean_text(block)
        if 'zzgl. nk' in block_text.lower() or 'nettokaltmiete' in block_text.lower():
            continue

        if re.search(r'Kaufpreis\s+auf\s+Anfrage', block_text, re.I):
            price = 'Preis auf Anfrage'
        else:
            price_matches = re.findall(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*€', block_text)
            price = price_matches[-1] if price_matches else 'Preis auf Anfrage'

        area_match = re.search(r'Wfl\.\s*ca\.\s*([0-9.,]+)', block_text, re.I)
        if not area_match:
            area_match = re.search(r'Nfl\.\s*ca\.\s*([0-9.,]+)', block_text, re.I)
        if not area_match:
            area_match = re.search(r'Grd\.\s*([0-9.,]+)\s*m²', block_text, re.I)
        area = area_match.group(1) if area_match else ''
        location_match = re.search(r'<div class="listEntryLocationShort"[^>]*>(.*?)</div>', block, re.S)
        location = clean_text(location_match.group(1)) if location_match else extract_location_text(block_text, 'München')

        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_engel_listings():
    listings = []
    seen = set()

    for page_url in ENGEL_URLS:
        try:
            req = urllib.request.Request(page_url, headers={'User-Agent': 'Mozilla/5.0'})
            html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
        except Exception:
            continue

        for block in re.findall(r'<article data-testid="search-components_result-card_[^"]+".*?</article>', html, re.S):
            title_match = re.search(r'data-testid="search-components_result-card_headline">(.*?)</h2>', block, re.S)
            location_match = re.search(r'data-testid="search-components_result-card_location">(.*?)</p>', block, re.S)
            price_match = re.search(r'data-testid="search-components_result-card_price">(.*?)</p>', block, re.S)
            area_match = re.search(r'data-testid="search-components_result-card_attribute_[^"]+-livingArea">(.*?)</span>', block, re.S)
            href_match = re.search(r'href="([^"]+/exposes/[^"]+)"', block, re.S)

            if not title_match or not href_match:
                continue

            title = clean_text(title_match.group(1))
            if not is_valid_title(title):
                continue

            location = clean_text(location_match.group(1)) if location_match else ''
            price = clean_text(price_match.group(1)) if price_match else 'Preis auf Anfrage'
            if area_match:
                area = extract_area_text(area_match.group(1))
            else:
                area = extract_area_text(clean_text(block))
            href = urljoin(page_url, href_match.group(1))

            add_listing(listings, seen, title, price, area, location, href)

        if listings:
            break

    return listings[:12]


def fetch_weichselgartner_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(WEICHSELGARTNER_URL, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    for match in re.finditer(r'<div class="property-container" id="(\d+)">(.*?)<div class="clearfix"></div>', html, re.S):
        block = match.group(2)
        location_match = re.search(r'<div class="property-location">(.*?)</div>', block, re.S)
        title_match = re.search(r'<h3 class="property-title">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        data_match = re.search(r'<div class="property-data-keyvalue">(.*?)</div>', block, re.S)
        if not title_match or not data_match:
            continue

        href = urljoin(WEICHSELGARTNER_URL, title_match.group(1))
        title = clean_text(title_match.group(2))
        if not is_valid_title(title):
            continue

        data_text = clean_text(data_match.group(1))
        price_match = re.search(r'Kaufpreis:\s*([^|<]+)', data_text, re.I)
        area_match = re.search(r'Wohnfläche:\s*ca\.\s*([0-9.,]+)', data_text, re.I)
        location = clean_text(location_match.group(1)) if location_match else 'München'
        price = price_match.group(1) if price_match else 'Preis auf Anfrage'
        area = area_match.group(1) if area_match else ''

        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_sopart_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(SOPART_URL, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    pattern = re.compile(r'<a[^>]+href="([^"]*(?:cmd=expose|/expose/)[^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
    for match in pattern.finditer(html):
        href = urljoin(SOPART_URL, clean_text(match.group(1)))
        chunk = html[max(0, match.start() - 2200):match.start() + 2200]
        title = clean_text(match.group(2))
        if title.lower().startswith('zum expos'):
            title = ''
        if not is_valid_title(title):
            title_match = re.search(r'<h2[^>]*>(.*?)</h2>|<h3[^>]*>(.*?)</h3>', chunk, re.I | re.S)
            if title_match:
                title = clean_text(title_match.group(1) or title_match.group(2) or '')
        if not is_valid_title(title):
            continue

        chunk_text = clean_text(chunk)
        price_match = re.search(r'Kaufpreis\s*:?\s*([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?|auf Anfrage)', chunk_text, re.I)
        if not price_match:
            price_match = re.search(r'([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*(?:EUR|€)', chunk_text, re.I)
        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*(?:ca\.?\s*)?([0-9.,]+)', chunk_text, re.I)
        if not area_match:
            area_match = re.search(r'([0-9.,]+)\s*m²', chunk_text, re.I)
        location_match = re.search(r'(?:Ort|Lage|Standort)\s*:\s*([^<\n|]+)', chunk, re.I)

        price = price_match.group(1) if price_match else 'Preis auf Anfrage'
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else extract_location_text(chunk_text, '')
        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_jalea_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(JALEA_URL, headers={'User-Agent': 'Mozilla/5.0'})
        overview_html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    for match in re.finditer(r'<article class="frymo-listing-item[^>]*>(.*?)</article>', overview_html, re.S | re.I):
        block = match.group(1)
        title_match = re.search(r"<h3 class='frymo-listing-title'>\s*<a href='([^']+)'[^>]*>(.*?)</a>", block, re.S | re.I)
        if not title_match:
            continue

        href = urljoin(JALEA_URL, clean_text(title_match.group(1)))
        title = clean_text(title_match.group(2))
        if not is_valid_title(title):
            continue

        location_match = re.search(r'<div class="frymo-listing-location"[^>]*>.*?</i>\s*(.*?)</div>', block, re.S | re.I)
        location = clean_text(location_match.group(1)) if location_match else ''

        price = 'Preis auf Anfrage'
        area = ''
        try:
            req = urllib.request.Request(href, headers={'User-Agent': 'Mozilla/5.0'})
            detail_html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')

            price_match = re.search(r'data-key="kaufpreis"[^>]*>\s*<div class="frymo-data-item-label">.*?</div>\s*<div class="frymo-data-item-value">(.*?)</div>', detail_html, re.S | re.I)
            if price_match:
                price = clean_text(price_match.group(1))

            area_match = re.search(r'data-key="(?:wohnflaeche|wohnflache)"[^>]*>\s*<div class="frymo-data-item-label">.*?</div>\s*<div class="frymo-data-item-value">(.*?)</div>', detail_html, re.S | re.I)
            if not area_match:
                area_match = re.search(r'<div class="frymo-data-item-label">\s*Wohnfl(?:ä|ae)che\s*</div>\s*<div class="frymo-data-item-value">(.*?)</div>', detail_html, re.S | re.I)
            if area_match:
                area = extract_area_text(clean_text(area_match.group(1)))

            if not location:
                location_match = re.search(r'data-key="ort"[^>]*>\s*<div class="frymo-data-item-label">.*?</div>\s*<div class="frymo-data-item-value">(.*?)</div>', detail_html, re.S | re.I)
                if location_match:
                    location = clean_text(location_match.group(1))
        except Exception:
            pass

        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_sedlmayr_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(SEDLMAYR_URL, headers={'User-Agent': 'Mozilla/5.0'})
        overview_html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    for match in re.finditer(r'<article class="frymo-listing-item[^>]*>(.*?)</article>', overview_html, re.S | re.I):
        block = match.group(1)
        title_match = re.search(r'<h3[^>]*frymo-listing-title[^>]*>\s*<a href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', block, re.S | re.I)
        if not title_match:
            continue

        href = urljoin(SEDLMAYR_URL, clean_text(title_match.group(1)))
        title = clean_text(title_match.group(2))
        if not is_valid_title(title):
            continue

        location_match = re.search(r'<div class="frymo-listing-location"[^>]*>.*?</i>\s*(.*?)</div>', block, re.S | re.I)
        location = clean_text(location_match.group(1)) if location_match else ''

        price = 'Preis auf Anfrage'
        area = ''
        try:
            req = urllib.request.Request(href, headers={'User-Agent': 'Mozilla/5.0'})
            detail_html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')

            price_match = re.search(r'data-key="kaufpreis"[^>]*>\s*<div class="frymo-data-item-label">.*?</div>\s*<div class="frymo-data-item-value">(.*?)</div>', detail_html, re.S | re.I)
            if price_match:
                price = clean_text(price_match.group(1))

            area_match = re.search(r'data-key="(?:wohnflaeche|wohnflache)"[^>]*>\s*<div class="frymo-data-item-label">.*?</div>\s*<div class="frymo-data-item-value">(.*?)</div>', detail_html, re.S | re.I)
            if not area_match:
                area_match = re.search(r'<div class="frymo-data-item-label">\s*Wohnfl(?:ä|ae)che\s*</div>\s*<div class="frymo-data-item-value">(.*?)</div>', detail_html, re.S | re.I)
            if area_match:
                area = extract_area_text(clean_text(area_match.group(1)))

            if not location:
                location_match = re.search(r'data-key="ort"[^>]*>\s*<div class="frymo-data-item-label">.*?</div>\s*<div class="frymo-data-item-value">(.*?)</div>', detail_html, re.S | re.I)
                if location_match:
                    location = clean_text(location_match.group(1))
        except Exception:
            pass

        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_kaiserreich_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(KAISERREICH_URL, headers={'User-Agent': 'Mozilla/5.0'})
        overview_html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    for match in re.finditer(r"<article class='slide-entry[^>]*slide-entry-overview[^>]*>(.*?)</article>", overview_html, re.S | re.I):
        block = match.group(1)
        title_match = re.search(r"<h2 class='slide-entry-title[^>]*>\s*<a href='([^']+)'[^>]*>(.*?)</a>", block, re.S | re.I)
        if not title_match:
            continue

        href = urljoin(KAISERREICH_URL, clean_text(title_match.group(1)))
        title = clean_text(title_match.group(2))
        if not is_valid_title(title):
            continue

        location_match = re.search(r"<div class='av_iconlist_title iconlist_title_small'[^>]*>(.*?)</div>", block, re.S | re.I)
        location = clean_text(location_match.group(1)) if location_match else ''

        price = 'Preis auf Anfrage'
        area = ''
        try:
            req = urllib.request.Request(href, headers={'User-Agent': 'Mozilla/5.0'})
            detail_html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')

            rows = re.findall(
                r"<div class=['\"]ak-tablerow['\"][^>]*>\s*<div class=['\"]ak-tablecell['\"]>(.*?)</div>\s*<div class=['\"]ak-tablecell['\"]>(.*?)</div>",
                detail_html,
                re.S | re.I,
            )
            for key_html, value_html in rows:
                key = clean_text(key_html).lower().rstrip(':')
                value = clean_text(value_html)
                if 'kaufpreis' in key and value:
                    price = value
                elif 'wohnfl' in key and value and not area:
                    area = extract_area_text(value)
                elif ('ort' in key or 'region' in key or 'lage' in key) and value and not location:
                    location = value
        except Exception:
            pass

        if re.search(r'^\d{1,3},\d{3}\.\d{3}', price):
            price = re.sub(r'(?<=\d),(?=\d{3}\.\d{3})', '.', price)
        price = price.replace('--', '').strip()

        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_sis_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(SIS_URL, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href_raw = clean_text(match.group(1))
        if not re.search(r'(immobilie|objekt|expose|angebot|kauf)', href_raw, re.I):
            continue

        href = urljoin(SIS_URL, href_raw)
        chunk = html[max(0, match.start() - 2500):match.start() + 2500]
        anchor_title = clean_text(match.group(2))
        title = anchor_title
        if re.match(r'(?i)^zum\s+objekt', title):
            title = ''
        if not is_valid_title(title):
            heading_match = re.search(r'<h2[^>]*>(.*?)</h2>|<h3[^>]*>(.*?)</h3>', chunk, re.S | re.I)
            if heading_match:
                title = clean_text(heading_match.group(1) or heading_match.group(2) or '')
        if not is_valid_title(title):
            continue

        chunk_text = clean_text(chunk)
        price_match = re.search(r'Kaufpreis\s*:?\s*([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?|auf Anfrage)', chunk_text, re.I)
        if not price_match:
            price_match = re.search(r'([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*(?:EUR|€)', chunk_text, re.I)
        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*(?:ca\.?\s*)?([0-9.,]+)', chunk_text, re.I)
        if not area_match:
            area_match = re.search(r'([0-9.,]+)\s*m²', chunk_text, re.I)
        location_match = re.search(r'(?:Ort|Lage|Standort)\s*:?\s*([^<\n|]+)', chunk, re.I)

        if not (price_match or area_match or location_match):
            continue

        price = price_match.group(1) if price_match else 'Preis auf Anfrage'
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else extract_location_text(chunk_text, '')
        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_ede_listings():
    listings = []
    seen = set()

    try:
        req = urllib.request.Request(EDE_URL, headers={'User-Agent': 'Mozilla/5.0'})
        html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'ignore')
    except Exception:
        return listings

    pattern = re.compile(
        r'<div class="all_objects_row[^\"]*">(.*?)<a class="oo-details-btn"[^>]+href="([^\"]+)"[^>]*>.*?</a>',
        re.S | re.I,
    )

    for match in pattern.finditer(html):
        block = match.group(1)
        href = urljoin(EDE_URL, clean_text(match.group(2)))

        title_match = re.search(r'<div class="list_title">(.*?)</div>', block, re.S | re.I)
        if not title_match:
            continue
        title = clean_text(title_match.group(1))
        if not is_valid_title(title):
            continue

        location_match = re.search(
            r'<strong>\s*Ort\s*</strong>\s*</span>\s*<span>(.*?)</span>',
            block,
            re.S | re.I,
        )
        if not location_match:
            location_match = re.search(
                r'<strong>\s*Stadtteil\s*</strong>\s*</span>\s*<span>(.*?)</span>',
                block,
                re.S | re.I,
            )

        area_match = re.search(
            r'<strong>\s*Wohnfl(?:ä|ae)che\s*</strong>\s*</span>\s*<span>(.*?)</span>',
            block,
            re.S | re.I,
        )
        price_match = re.search(
            r'<strong>\s*Kaufpreis\s*</strong>\s*</span>\s*<span>(.*?)</span>',
            block,
            re.S | re.I,
        )

        location = clean_text(location_match.group(1)) if location_match else ''
        area = extract_area_text(clean_text(area_match.group(1))) if area_match else ''
        price = clean_text(price_match.group(1)) if price_match else 'Preis auf Anfrage'
        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


BROKER_SOURCES = [
    ('bader', fetch_bader_listings),
    ('schloss', fetch_schloss_listings),
    ('rogers', fetch_rogers_listings),
    ('firstplace', fetch_firstplace_listings),
    ('bartsch', fetch_bartsch_listings),
    ('schneider', fetch_schneider_listings),
    ('graf', fetch_graf_listings),
    ('riedel', fetch_riedel_listings),
    ('engel', fetch_engel_listings),
    ('weichselgartner', fetch_weichselgartner_listings),
    ('sopart', fetch_sopart_listings),
    ('jalea', fetch_jalea_listings),
    ('sedlmayr', fetch_sedlmayr_listings),
    ('kaiserreich', fetch_kaiserreich_listings),
    ('sis', fetch_sis_listings),
    ('ede', fetch_ede_listings),
]


def fetch_listings():
    global LISTINGS_CACHE, LISTINGS_CACHE_TIME
    now = time.time()
    if LISTINGS_CACHE is not None and now - LISTINGS_CACHE_TIME < CACHE_TTL_SECONDS:
        return LISTINGS_CACHE

    listings = {}
    max_workers = min(8, len(BROKER_SOURCES)) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetcher): key for key, fetcher in BROKER_SOURCES}
        for future in as_completed(futures):
            key = futures[future]
            try:
                listings[key] = future.result()
            except Exception:
                listings[key] = []

    LISTINGS_CACHE = listings
    LISTINGS_CACHE_TIME = now
    return LISTINGS_CACHE


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/listings'):
            listings = fetch_listings()
            body = json.dumps(listings).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        html = """<!doctype html>
<html lang=\"de\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Immobilien-Übersicht</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background: #f4f7fb; color: #1f2937; }
    header { background: #0f172a; color: white; padding: 24px; }
    main { max-width: 1000px; margin: 0 auto; padding: 24px; }
    .card { background: white; border-radius: 12px; padding: 16px; margin-bottom: 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
    .title { font-size: 18px; font-weight: 600; margin-bottom: 8px; }
    .meta { color: #4b5563; font-size: 14px; }
        .toolbar { display: grid; gap: 12px; margin-bottom: 16px; }
        .search { width: 100%; border: 1px solid #cbd5e1; border-radius: 999px; padding: 12px 16px; font-size: 15px; background: white; box-sizing: border-box; }
        .tabs { display: flex; gap: 10px; margin-bottom: 4px; overflow-x: auto; padding-bottom: 4px; scrollbar-width: thin; }
        .tab { border: 0; padding: 10px 16px; border-radius: 999px; background: #e2e8f0; cursor: pointer; font-weight: 600; white-space: nowrap; flex: 0 0 auto; }
    .tab.active { background: #0f172a; color: white; }
    .loading { display: inline-flex; align-items: center; gap: 8px; color: #64748b; font-weight: 600; }
    .spinner { width: 16px; height: 16px; border: 2px solid #cbd5e1; border-top-color: #0f172a; border-radius: 50%; animation: spin 0.8s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    a { color: #2563eb; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <header><h1>Aktuelle Häuser zum Kauf</h1><p>Liste aus der Zielseite der Immobilienagentur</p></header>
  <main>
        <div class="toolbar">
            <input id="broker-search" class="search" type="search" placeholder="Makler filtern">
            <input id="listing-search" class="search" type="search" placeholder="Ort, Titel, Preis oder Wohnfläche filtern">
            <div id="tabs" class="tabs"></div>
        </div>
        <div id="listings" class="loading">
            <span class="spinner"></span>
            <span>Lade Inserate…</span>
        </div>
  </main>
  <script>
        const brokerLabels = __BROKER_LABELS__;
        const tabsRoot = document.getElementById('tabs');
        const brokerSearchInput = document.getElementById('broker-search');
        const listingSearchInput = document.getElementById('listing-search');
        const root = document.getElementById('listings');
        let activeTab = 'bader';
        let cachedData = null;
        let cacheTimestamp = 0;
        const cacheTtlMs = 5 * 60 * 1000;
        let brokerQuery = '';
        let listingQuery = '';

        function setLoading() {
            root.className = 'loading';
            root.innerHTML = '<span class="spinner"></span><span>Lade Inserate…</span>';
        }

        function formatLabel(key) {
            return brokerLabels[key] || key.replace(/_/g, ' ').replace(/\b\w/g, char => char.toUpperCase());
        }

        function filterBrokerKeys(data) {
            const query = brokerQuery.trim().toLowerCase();
            return Object.keys(data).filter(key => {
                if (!query) {
                    return true;
                }
                return formatLabel(key).toLowerCase().includes(query) || key.toLowerCase().includes(query);
            });
        }

        function filterListings(listings) {
            const query = listingQuery.trim().toLowerCase();
            if (!query) {
                return listings;
            }
            return listings.filter(item => {
                const haystack = [item.title, item.location, item.price, item.area_sqm].join(' ').toLowerCase();
                return haystack.includes(query);
            });
        }

        function renderListings(listings) {
            if (!listings.length) {
                root.className = '';
                root.innerHTML = '<p>Keine Inserate gefunden.</p>';
                return;
            }
            root.className = '';
            root.innerHTML = listings.map(item => `
                <article class="card">
                    <div class="title">${item.title}</div>
                    <div class="meta">Preis: ${item.price || 'nicht verfügbar'}</div>
                    <div class="meta">Wohnfläche: ${item.area_sqm ? item.area_sqm + ' m²' : 'nicht verfügbar'}</div>
                    <div class="meta">Ort: ${item.location || 'nicht verfügbar'}</div>
                    <div class="meta"><a href="${item.link}" target="_blank" rel="noreferrer">Zum Exposé</a></div>
                </article>
            `).join('');
        }

        function renderActiveListings() {
            if (!cachedData) {
                return;
            }
            const list = filterListings(cachedData[activeTab] || []);
            renderListings(list);
        }

        function renderTabs(data) {
            const keys = filterBrokerKeys(data);
            if (!keys.length) {
                tabsRoot.innerHTML = '<span class="meta">Keine Makler gefunden.</span>';
                return;
            }

            if (!keys.includes(activeTab)) {
                activeTab = keys[0];
            }

            tabsRoot.innerHTML = keys.map(key => {
                const count = (data[key] || []).length;
                const activeClass = key === activeTab ? ' active' : '';
                return `<button class="tab${activeClass}" data-tab="${key}">${formatLabel(key)} (${count})</button>`;
            }).join('');

            tabsRoot.querySelectorAll('.tab').forEach(button => {
                button.addEventListener('click', () => {
                    tabsRoot.querySelectorAll('.tab').forEach(btn => btn.classList.remove('active'));
                    button.classList.add('active');
                    activeTab = button.dataset.tab;
                    renderActiveListings();
                });
            });
        }

        async function loadListings(forceRefresh = false) {
            const now = Date.now();
            if (!forceRefresh && cachedData && (now - cacheTimestamp) < cacheTtlMs) {
                renderTabs(cachedData);
                renderActiveListings();
                return;
            }

            setLoading();
            try {
                const res = await fetch('/api/listings');
                const data = await res.json();
                cachedData = data;
                cacheTimestamp = now;
                renderTabs(cachedData);
                renderActiveListings();
            } catch (err) {
                root.className = '';
                root.innerHTML = '<p>Die Inserate konnten nicht geladen werden.</p>';
            }
        }

        brokerSearchInput.addEventListener('input', event => {
            brokerQuery = event.target.value || '';
            renderTabs(cachedData || {});
            renderActiveListings();
        });

        listingSearchInput.addEventListener('input', event => {
            listingQuery = event.target.value || '';
            renderActiveListings();
        });

        loadListings(true);
    </script>

</body>
        </html>"""
        body = html.replace('__BROKER_LABELS__', json.dumps(BROKER_LABELS)).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 8000), Handler)
    print('Server running on http://127.0.0.1:8000')
    server.serve_forever()
