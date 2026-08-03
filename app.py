from __future__ import annotations
import json
import re
import ssl
import time
import gzip
import zlib
import ipaddress
import logging
import threading
from email.utils import formatdate
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
import urllib.request
from urllib.error import HTTPError, URLError
import os
from html import unescape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlparse

LOGGER = logging.getLogger(__name__)

TARGET_URL = 'https://www.starnbergersee-immobilien.de/Haeuser-zum-Kauf.htm'

LISTINGS_CACHE = None
LISTINGS_CACHE_TIME = 0
LISTINGS_CACHE_UPDATED_AT = None
REFRESH_STATE_LOCK = threading.Lock()
REFRESH_STATE = {
    'active': False,
    'started_at': None,
    'updated_at': None,
    'brokers': {},
    'listings': None,
    'error': None,
}
CACHE_TTL_SECONDS = 5 * 60
LISTINGS_REFRESH_SECONDS = 3 * 60 * 60
LISTINGS_FETCH_TIMEOUT_SECONDS = 120
REFRESH_COOLDOWN_SECONDS = 0
LAST_REFRESH_REQUEST_TIME = 0
INTERNAL_REFRESH_TOKEN = os.environ.get('INTERNAL_REFRESH_TOKEN', '')
LISTINGS_BLOB_CONTAINER_URL = os.environ.get(
    'LISTINGS_BLOB_CONTAINER_URL',
    'https://maklerappstorageaccount.blob.core.windows.net/maklerapp',
)
LISTINGS_BLOB_NAME = os.environ.get('LISTINGS_BLOB_NAME', 'latest.json')
AZURE_STORAGE_SAS_TOKEN = os.environ.get('AZURE_STORAGE_SAS_TOKEN', '')
LISTINGS_BLOB_ENABLED = (
    bool(os.environ.get('WEBSITE_SITE_NAME'))
    or bool(AZURE_STORAGE_SAS_TOKEN)
    or os.environ.get('LISTINGS_BLOB_ENABLED', '').lower() == 'true'
)
SCHLOSS_URL = 'https://schlossberger-immobilien.de/immobilien-angebote/?inx-sort=availability_desc'
ROGERS_URL = 'https://www.rogers-immobilien.de/immobilienangebote/'
FIRSTPLACE_URL = 'https://firstplace.de/verkaufsobjekte/'
BARTSCH_URL = 'https://www.bartsch-immo.de/immobilien-vermarktungsart/kauf/'
SCHNEIDER_URL = 'https://www.immobilienschneider.com/kaufangebote/'
IGNORED_BROKERS = {'aigner'}
BLOCKED_BROKER_REASONS = {
    'neuesnest': 'Blocked',
    'bunzco': 'Blocked',
}


def listings_blob_url():
    container_url = LISTINGS_BLOB_CONTAINER_URL.rstrip('/')
    sas_suffix = AZURE_STORAGE_SAS_TOKEN
    if sas_suffix and not sas_suffix.startswith('?'):
        sas_suffix = '?' + sas_suffix
    return f'{container_url}/{LISTINGS_BLOB_NAME}{sas_suffix}'


def blob_request_headers():
    if AZURE_STORAGE_SAS_TOKEN:
        return {'x-ms-version': '2021-12-02'}

    identity_endpoint = os.environ.get('IDENTITY_ENDPOINT')
    identity_header = os.environ.get('IDENTITY_HEADER')
    if identity_endpoint or identity_header:
        if not identity_endpoint or not identity_header:
            raise RuntimeError(
                'App Service Managed Identity is incompletely configured: '
                'IDENTITY_ENDPOINT and IDENTITY_HEADER are both required.'
            )
        parsed_endpoint = urlparse(identity_endpoint)
        query = dict(parse_qsl(parsed_endpoint.query))
        query.update({
            'api-version': '2019-08-01',
            'resource': 'https://storage.azure.com/',
        })
        token_url = parsed_endpoint._replace(query=urlencode(query)).geturl()
        token_headers = {'X-IDENTITY-HEADER': identity_header}
    elif os.environ.get('WEBSITE_SITE_NAME'):
        raise RuntimeError(
            'App Service Managed Identity endpoint is unavailable. '
            'Enable the system-assigned identity and restart the App Service.'
        )
    else:
        token_url = (
            'http://169.254.169.254/metadata/identity/oauth2/token'
            '?api-version=2019-08-01&resource=https%3A%2F%2Fstorage.azure.com%2F'
        )
        token_headers = {'Metadata': 'true'}
    token_request = urllib.request.Request(token_url, headers=token_headers)
    with urllib.request.urlopen(token_request, timeout=10) as response:
        token = json.loads(response.read().decode('utf-8'))['access_token']
    return {
        'Authorization': f'Bearer {token}',
        'x-ms-version': '2021-12-02',
        'x-ms-date': formatdate(usegmt=True),
    }


def blob_urlopen(request, timeout=20):
    for attempt in range(3):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except URLError:
            if attempt == 2:
                raise
            time.sleep(1 << attempt)


def read_listings_blob(include_stale=False):
    if not LISTINGS_BLOB_ENABLED:
        return None
    request = urllib.request.Request(listings_blob_url(), headers=blob_request_headers())
    with blob_urlopen(request) as response:
        payload = json.loads(response.read().decode('utf-8'))
    generated_at = payload.get('generated_at', 0)
    if isinstance(generated_at, str):
        generated_at = 0
    if not include_stale and time.time() - generated_at >= LISTINGS_REFRESH_SECONDS:
        return None
    return payload.get('listings'), generated_at


def write_listings_blob(listings, generated_at=None):
    if not LISTINGS_BLOB_ENABLED:
        return
    payload = json.dumps({
        'generated_at': generated_at if generated_at is not None else time.time(),
        'listings': listings,
    }).encode('utf-8')
    headers = blob_request_headers()
    headers.update({
        'Content-Type': 'application/json; charset=utf-8',
        'Content-Length': str(len(payload)),
        'x-ms-blob-type': 'BlockBlob',
    })
    request = urllib.request.Request(
        listings_blob_url(),
        data=payload,
        headers=headers,
        method='PUT',
    )
    with blob_urlopen(request):
        return


def format_blob_error(error):
    if isinstance(error, HTTPError):
        error_code = error.headers.get('x-ms-error-code', '')
        details = f'Azure Blob HTTP {error.code}'
        if error_code:
            details += f' ({error_code})'
        return details
    return f'{type(error).__name__}: blob request failed'


def listing_identity(broker_key, listing):
    link = clean_text(str((listing or {}).get('link', ''))).rstrip('/').lower()
    return broker_key, link or clean_text(str((listing or {}).get('title', ''))).lower()


def enrich_listing_history(previous, current, broker_success, now=None):
    now = time.time() if now is None else now
    result = {}
    for broker_key, old_rows in (previous or {}).items():
        for row in old_rows or []:
            if not isinstance(row, dict):
                continue
            identity = listing_identity(broker_key, row)
            if identity[1]:
                result[identity] = dict(row)

    for broker_key, rows in (current or {}).items():
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            identity = listing_identity(broker_key, row)
            if not identity[1]:
                continue
            old = result.get(identity, {})
            first_seen_at = old.get('first_seen_at') or now
            item = dict(row)
            old_price = clean_text(str(old.get('price', '')))
            new_price = clean_text(str(item.get('price', '')))
            if old_price and new_price and old_price != new_price:
                item['old_price'] = old_price
            elif old.get('old_price') and old_price == new_price:
                item['old_price'] = old['old_price']
            else:
                item.pop('old_price', None)
            item.update({
                'first_seen_at': first_seen_at,
                'last_seen_at': now,
                'note': '',
            })
            result[identity] = item

    for broker_key, was_successful in (broker_success or {}).items():
        if not was_successful:
            continue
        current_ids = {
            listing_identity(broker_key, row)
            for row in (current or {}).get(broker_key, [])
            if isinstance(row, dict)
        }
        for identity, row in list(result.items()):
            if identity[0] != broker_key or identity in current_ids:
                continue
            row = dict(row)
            row['note'] = 'Gelöscht'
            row['is_deleted'] = True
            result[identity] = row

    history = {}
    for (broker_key, _identity), row in result.items():
        first_seen_at = row.get('first_seen_at') or now
        try:
            age_days = max(0, int((now - float(first_seen_at)) // 86400))
        except (TypeError, ValueError):
            first_seen_at = now
            age_days = 0
        row['first_seen_at'] = first_seen_at
        row['age_days'] = age_days
        row.setdefault('last_seen_at', first_seen_at)
        row.setdefault('note', '')
        row['is_deleted'] = row.get('note') == 'Gelöscht'
        history.setdefault(broker_key, []).append(row)
    for broker_key in set((previous or {}).keys()) | set((current or {}).keys()) | set((broker_success or {}).keys()):
        history.setdefault(broker_key, [])
    return history
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
NIKKI_URL = 'https://nikki-livings.de/immobilienportfolio/'
TSC_URLS = [
    'https://tsc-immobilien.de/category/kaufen/haeuser_und_villen/',
    'https://tsc-immobilien.de/category/kaufen/wohnungen/',
]
IMOTHEK_URL = 'https://www.imothek.de/kaufangebote-2/?filters%5btype%5d=Wohnung'
VR_URLS = [
    'https://immobilien.vr-starnberg-zugspitze.de/alle-immobilien/haeuser/',
    'https://immobilien.vr-starnberg-zugspitze.de/alle-immobilien/wohnungen/',
]
STOLZE_URL = 'https://www.stolze-immobilien.com/ff/immobilien/'
REALWERT_URL = 'https://realwert-bayern.de/angebote/'
EDEN_URL = 'https://eden-living.de/angebote/'
IMLIVING_URL = 'https://www.i-m-living.de/immobilien/haeuser-/-wohnungen/'
WEBAU_URL = 'https://www.webau-immobilien.de/index.php4?cmd=searchDetails&objq[cursor]=0&kaufartids=1&obercmd=search_alias_alle_objekte_nur_kauf&icmd=14483653113958'
MAR_URL = 'https://www.mar-immobilien.de/angebote'
SEEIMMO_URL = 'https://www.see-immo.de/aktuelle-immobilienangebote.html'
HEIDINGER_URL = 'https://www.heidinger-immobilien.de/kaufobjekte/'
FUNER_URL = 'https://funer-immobilien-starnberg.de/aktuelle-immobilien/'
WEIHERER_URL = 'https://www.weiherer-immobilien.de/kaufobjekte/'
MB_URL = 'https://mb-immobilien-gmbh.com/verkaufen/'
FISCHER_URL = 'https://www.fischer-immobilien-muenchen.de/immobilien/'
HEIMHUBER_URL = 'https://heimhuber-immobilien.de/immobilien-vermarktungsart/kauf/'
CITIGRUND_URL = 'https://citigrund.de/immobilienangebote/'
GEORGI_URL = 'https://georgi-immobilien.com/immobilien/'
AKURAT_URL = 'https://www.akurat.net/immobilienangebote/'
HEGERICH_URL = 'https://www.hegerich-immobilien.de/index.php4?cmd=searchResults&goto=1&alias=suchmaske&aktwaehrung=%80&kaufartids=1&kategorieids=&ortnamegenau=&flaecheKatAbhaengigMin=0.00&flaecheKatAbhaengigMax=60017.00&preisMin=0.00&preisMax=2990000.00&zimmerMin=0.00&zimmerMax=12.00&objq%5Border_zusammen%5D=&objqorder_zusammen=#sprung_modul_suchmakse_0'
EDER_URL = 'https://www.immoservice-eder.de/immobilien-angebote/'
GERSCHLAUER_URL = 'https://www.gerschlauer.de/Haeuser-zum-Kauf.htm'
DAHLER_URL = 'https://www.dahlercompany.com/de/immobiliensuche?sort=ds_created%20desc&dcRegion=de&chambersFrom=3&livingAreaFrom=85&priceTo=850000&sort=ds_created%20desc&lat=48.1075567&lng=11.4313524&place=M%C3%BCnchen%2C%20BY%2C%20Bayern%2C%20Deutschland&distance=15'
KRIMBACHER_URL = 'https://krimbacher-immobilien.de/#angebote'
KLATT_URL = 'https://alexander-klatt.de/immobilien/'
FT_URL = 'https://www.ftimmobilien24.com/aktuelle-immobilien/bestandsobjekte/'
TESCH_URL = 'https://immo-tesch.de/immobilien/'
RITTER_URL = 'https://ritter-bautraeger.de/kauf-verkauf-muenchen-sued-ritter-bautraeger-immobilien/haeuser/'
HIRSCHMANN_URL = 'https://hirschmann-kaul.de/kaufen/'
ROHRER_URL = 'https://rohrer-firmengruppe.de/immobilien-vermarktung/immobilien.html'
MRLODGE_URL = 'https://www.mrlodge.de/kaufen/alle-immobilien'
REICHENBERGER_URL = 'https://www.reichenberger-immobilien.de/immobilien/#angebote'
HEIDTMANN_URL = 'https://www.heidtmann-immobilien.de/fuer-interessenten/immobilienangebote/'
MUELLER_ENGLISCH_URL = 'https://mueller-englisch.de/immobilien-details.xhtml?f%5B15253-5%5D=1&f%5B15253-3%5D=1&f%5B15253-7%5D=haus&f%5B15253-13%5D=M%C3%BCnchen&f%5B15253-9%5D=kauf&f%5B15253-21%5D=&f%5B15253-23%5D=&f%5B15253-25%5D=0&f%5B15253-27%5D=850000&f%5B15253-15%5D=3&f%5B15253-29%5D=50'
STROBL_URL = 'https://www.immobilien-strobl.de/immobilien/?navid=701414701414'
AUNDOWOHNBAU_URL = 'https://aundowohnbau.de/bestandsobjekte/'
GRAEF_IMMO_URL = 'https://graef-immo.de/immobilien/'
ROETHIG_URL = 'https://www.roethig-immobilien.de/angebote/verkauf/'
WANGENHEIM_URL = 'https://www.wangenheim.de/immobilien/aktuelle-angebote?marketingType=BUY&propertyType=89703%2C89697&sorting=price-ascending&page=1&view=grid'
EGGER_URL = 'https://egger-immo.de/immobilien/immobilien-muenchen/'
NEUESNEST_URL = 'https://neuesnest.de/aktuelle-immobilien/'
PARKAVENUE_URL = 'https://parkavenue.immobilien/immobilien/muenchen/'
WEBER_URL = 'https://weber-immobilien.net/immobilien-angebot/'
WURMSEDER_URL = 'https://wurmseder-immobilien.de/immobilien/'
ELVIRA_URL = 'https://www.elvira-immo.de/immobilienangebote'
SOTHEBYS_URL = 'https://bayern-sothebysrealty.com/immobilien-muenchen/'
DUERRENBERGER_URL = 'https://www.duerrenberger-immobilien.de/Haeuser-zum-Kauf.htm'
WOEHRY_URL = 'https://www.woehry.immo/immobilienangebote/'
VONRODENHAUSEN_URL = 'https://www.vonrodenhausen.de/aktuelle-angebote'
MARTINA_SCHWARZ_URL = 'https://martina-schwarz-immobilien.de/immobilien-muenchen/'
PIENZENAUER_URL = 'https://www.pienzenauer-trudering.de/immobilien/'
FRIEDLMAIER_URL = 'https://friedlmaier-immobilien.de/immobilienangebote-in-muenchen-und-umgebung/'
WINDHAUSEN_URL = 'https://windhausen-partner.de/angebote/'
MAIER_URL = 'https://www.maierimmobilien.de/immobilien/'
RIEDL_MAKLER_URL = 'https://riedl-makler.de/referenzen/'
HEIMMOBILIEN_URL = 'https://www.heimmobilien.de/Immobilien-Angebote'
SEEBAUER_URL = 'https://www.seebauer-immobilien.de/immobilien/'
ZIPPOLD_URL = 'https://immobilien-zippold.de/haus-kaufen/'
MUELLER_GROSCURTH_URL = 'https://mueller-groscurth-immobilien.de/immobilien/'
BUNZCO_URL = 'https://bunz-co.de/immobilien/'
IMMOSMART_URL = 'https://immosmart.de/kaufen/'
LEHMANNHUEBER_URL = 'https://lehmannhueber.de/immobilien/'
DRESCHER_URL = 'https://drescher-immobilien.de/kaufangebote'
SQMETER_URL = 'https://www.sqmeter.de/verkauf/'
WEGENER_URL = 'https://www.wegenerimmobilien.de/Haeuser-zum-Kauf.htm'
HACKER_GLASS_URL = 'https://www.hacker-glass.de/immobilien/'
WOHNREF_URL = 'https://www.wohnref-muenchen.de/immobilien/'
HERRMANN_URL = 'https://www.immobilien-herrmann.net/Angebote.htm'
SCHMIDT_MUENCHEN_URL = 'https://www.immobilien-schmidt-muenchen.de/angebote-kaufen-mieten'
DAVID_JACQUES_URL = 'https://davidundjacques.de/immobilien/haus-kaufen-muenchen/'
DALEXIS_URL = 'https://www.dalexis-immobilien.de/immobilien-kauf-verkauf/'
GG_URL = 'https://www.gg-immobilien.de/angebote/immobilienangebote/'
MARTE_URL = 'https://www.immobilienmarte.de/immobilienangebote.xhtml'
DAWONIA_URL = 'https://www.dawonia.de/de/kaufen'
ORANGE_URL = 'https://orange-immobilien.de/immobilien?sectionId=69046b637c7cacf55d10a875&fields%5Bgeneral_vermarktungsart%5D=KAUF&fields%5Bgeneral_umkreissuche%5D%5Bloc%5D=M%C3%BCnchen&fields%5Bgeneral_umkreissuche%5D%5Bdistance%5D=25'
VORSTADTMAKLER_URL = 'https://vorstadtmakler.de/immobilien'
TEAMBIM_URL = 'https://team-bim.de/#imag_immobiliensuche'
SOZIUS_URL = 'https://www.sozius-immobilien.de/immobilien/wohnen/'
ANDREAS_SCHMID_URL = 'https://www.andreas-schmid-immobilien.de/alle-immobilien/'
MUENCHNER_IMMOBILIEN_URL = 'https://www.muenchner-immobilien.eu/muenchner-immobilien-angebot-verkauf_haeuser.html'
AUSDEMHAEUSCHEN_URL = 'https://www.ausdemhaeuschen.com/angebot'
HALLINGER_URL = 'https://www.hallingerimmobilien.de/kaufobjekte.php'
CKI_URL = 'https://cki-immobilien.de/immobilienangebote/kaufen/'
WINDISCH_URL = 'https://windisch-immobilien.de/kaufen/alle-angebote/'
IM7_URL = 'https://www.im7-gmbh.de/KAUF.htm'
SE_IMMOBILIEN_WOHNUNGEN_URL = 'https://www.se-immobilienmakler.de/Eigentumswohnungen.htm'
SE_IMMOBILIEN_HAEUSER_URL = 'https://www.se-immobilienmakler.de/Haeuser-zum-Kauf.htm'
HAPPY_IMMO_URL = 'https://www.happy-immo.de/suche-alle-angebote-happy-immo.xhtml?f[1581-24]=kauf'
WANDL_URL = 'https://wandl.immobilien/aktuelle-immobilien/'
EMSLANDER_URL = 'https://www.emslander-co.de/immobilien/'
HOSER_URL = 'https://www.hoser-immobilien.de/'
FEUERLEIN_URL = 'https://www.feuerlein-immobilien.de/immobilienangebot.html'
LEBENSTRAUM_URL = 'https://lebenstraum-immobilien.com/suchende/immobilien/muenchen/kaufen/'
GSCHWENDER_URL = 'https://www.gschwender-immobilien.de/angebote'
MAURER_URL = 'https://www.maurerimmobilien.de/Angebote.htm#'
PSCHEIDT_HAEUSER_URL = 'https://www.pscheidt-immobilien.com/haeuser.xhtml'
PSCHEIDT_WOHNUNGEN_URL = 'https://www.pscheidt-immobilien.com/wohnungen.xhtml'
BECHLER_URL = 'https://www.bechler-immobilien.de/verkauf-vermietung/#kauf'
ISARESTATE_URL = 'https://isarestate.de/angebot/'
WESOLY_URL = 'https://www.wesoly-immobilien.de/immobilienangebote-zum-kauf/'
IMMOBILIENWESTEND_URL = 'https://www.immobilienwestend.de/Haeuser-zum-Kauf.htm'
STIERLING_URL = 'https://www.stierling-immobilien.de/zum-kauf/'
FINESTEP_URL = 'https://www.finestep.de/immobilien/?post_type=immomakler_object&paged=1&vermarktungsart=kauf&nutzungsart=&typ=&ort=&center=&objekt-id='
FAIR_HOMES_URL = 'https://www.fair-homes.immo/immobilien-kauf'
CHALET_URL = 'https://www.chalet-immobilien.com/Angebote.htm'
KRAFT_ZILLER_URL = 'https://www.kraft-ziller.de/Haeuser-zum-Kauf.htm'
WOLF_WOHNUNG_URL = 'https://wolf-immobilien.online/wohnung-kaufen/'
WOLF_HAUS_URL = 'https://wolf-immobilien.online/haus-kaufen/'
BAYERGRUND_URL = 'https://bayergrund-immo.de/angebote/'
IMMOBILIEN_PS_URL = 'https://www.immobilien-ps.de/angebote'
RSI_EINFAMILIEN_URL = 'https://www.rsi-immobilien.de/angebote/bestandsimmobilien/einfamilienhaeuser/'
RSI_MEHRFAMILIEN_URL = 'https://www.rsi-immobilien.de/angebote/bestandsimmobilien/mehrfamilienhauser/'
RSI_WOHNUNGEN_URL = 'https://www.rsi-immobilien.de/angebote/bestandsimmobilien/wohnungen/'
SIEMAX_URL = 'https://siemax.de/immobilien/?post_type=immomakler_object&paged=1&vermarktungsart%5B%5D=kauf&center=&objekt-id=&collapse=in&von-qm=0.00&bis-qm=4200.00&von-zimmer=0.00&bis-zimmer=20.00&von-kaltmiete=0.00&bis-kaltmiete=3600.00&von-kaufpreis=0.00&bis-kaufpreis=4625000.00'
JOSEF_FREI_WOHNUNGEN_URL = 'https://josef-frei-immobilien.de/objekte/eigentumswohnungen/'
JOSEF_FREI_HAEUSER_URL = 'https://josef-frei-immobilien.de/objekte/haeuser/'
LUENENDONK_URL = 'https://www.luenendonk-immobilien.de/Objekte.htm'
HARINALI_URL = 'https://www.harinali.de/immobilienangebote/'
AUFRECHT_T2_URL = 'https://www.aufrecht-immo.de/immobilien?t=2&o=&st=buy&pr=&qm=&r='
AUFRECHT_T1_URL = 'https://www.aufrecht-immo.de/immobilien?t=1&o=&st=buy&pr=&qm=&r='
RAMONANECKAR_URL = 'https://www.ramonaneckar-immobilien.de/angebote'
MYTROPPER_URL = 'https://www.mytropper-immobilien.de/index.php4?cmd=searchResults&goto=1&alias=suchmaske&aktwaehrung=%80&kaufartids=1&kategorieids=&ortnamegenau=&flaecheKatAbhaengigMin=35.00&flaecheKatAbhaengigMax=686.00&preisMin=11.00&preisMax=1985000.00&zimmerMin=0.00&zimmerMax=15.00&objq%5Border_zusammen%5D=&objqorder_zusammen=#sprung_modul_suchmakse_0'
SRI_IMMO_URL = 'https://www.sri-immo.de/immobilien-kaufen-mieten.html'
ZG_IMMOBILIEN_URL = 'https://zg-immobilien.de/'
ZARDINI_HAEUSER_URL = 'https://www.zardini-immobilien.de/haeuser.xhtml'
ZARDINI_WOHNUNGEN_URL = 'https://www.zardini-immobilien.de/wohnungen.xhtml'
REISCHL_URL = 'https://www.reischl-immobilien.de/objects.php'
GATTINGER_URL = 'https://gattinger-immo.de/angebote'
UNKNOWN_LOCATION = 'N/A'
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
    'nikki': 'Nikki Livings',
    'tsc': 'TSC Immobilien',
    'imothek': 'IMOTHEK',
    'vr': 'VR Starnberg-Zugspitze',
    'stolze': 'Stolze Immobilien',
    'realwert': 'Realwert Bayern',
    'eden': 'Eden Living',
    'imliving': 'I-M Living',
    'webau': 'WEBAU Immobilien',
    'mar': 'MAR Immobilien',
    'seeimmo': 'SEE-Immo',
    'heidinger': 'Heidinger Immobilien',
    'funer': 'Funer Immobilien',
    'weiherer': 'Weiherer Immobilien',
    'mb': 'MB Immobilien',
    'fischer': 'Fischer Immobilien München',
    'heimhuber': 'Heimhuber Immobilien',
    'citigrund': 'Citigrund Immobilien',
    'georgi': 'Georgi Immobilien',
    'akurat': 'Akurat Service Real Estate',
    'hegerich': 'Hegerich Immobilien',
    'eder': 'Immoservice Eder',
    'gerschlauer': 'Gerschlauer Immobilien',
    'dahler': 'DAHLER Munich',
    'krimbacher': 'Krimbacher Immobilien',
    'klatt': 'Alexander Klatt Immobilien',
    'ft': 'FT Immobilien 24',
    'tesch': 'Immo Tesch',
    'ritter': 'Ritter Bautraeger Immobilien',
    'hirschmann': 'Hirschmann Kaul Immobilien',
    'rohrer': 'Rohrer Firmengruppe',
    'mrlodge': 'Mr. Lodge',
    'reichenberger': 'Reichenberger Immobilien',
    'heidtmann': 'Heidtmann Immobilien',
    'muellerenglisch': 'Mueller Englisch Immobilien',
    'strobl': 'Immobilien Strobl',
    'aundowohnbau': 'A & O Wohnbau',
    'graef': 'Graef Immobilien',
    'roethig': 'Roethig Immobilien',
    'wangenheim': 'Wangenheim Immobilien',
    'egger': 'Egger Immobilien',
    'neuesnest': 'Neues Nest',
    'parkavenue': 'Park Avenue Immobilien',
    'weber': 'Weber Immobilien',
    'wurmseder': 'Wurmseder Immobilien',
    'elvira': 'Elvira Immobilien',
    'sothebys': 'Bayern Sothebys Realty',
    'duerrenberger': 'Duerrenberger Immobilien',
    'woehry': 'Woehry Immobilien',
    'vonrodenhausen': 'von Rodenhausen Immobilien',
    'martinaschwarz': 'Martina Schwarz Immobilien',
    'pienzenauer': 'Pienzenauer Trudering Immobilien',
    'friedlmaier': 'Friedlmaier Immobilien',
    'windhausen': 'Windhausen Partner',
    'maier': 'Maier Immobilien',
    'riedl': 'Riedl Makler',
    'heimmobilien': 'Heim Immobilien',
    'seebauer': 'Seebauer Immobilien',
    'zippold': 'Immobilien Zippold',
    'muellergroscurth': 'Mueller Groscurth Immobilien',
    'bunzco': 'Bunz & Co Immobilien',
    'immosmart': 'Immosmart',
    'lehmannhueber': 'Lehmannhueber Immobilien',
    'drescher': 'Drescher Immobilien',
    'sqmeter': 'SQMETER Immobilien',
    'wegener': 'Wegener Immobilien',
    'hackerglass': 'Hacker Glass Immobilien',
    'wohnref': 'Wohnref Muenchen',
    'herrmann': 'Immobilien Herrmann',
    'schmidtmuenchen': 'Immobilien Schmidt Muenchen',
    'davidjacques': 'David & Jacques Immobilien',
    'dalexis': 'DAlexis Immobilien',
    'gg': 'GG Immobilien',
    'marte': 'Immobilien Marte',
    'dawonia': 'Dawonia',
    'orange': 'Orange Immobilien',
    'vorstadtmakler': 'Vorstadtmakler',
    'teambim': 'Team BIM',
    'sozius': 'Sozius Immobilien',
    'andreasschmid': 'Andreas Schmid Immobilien',
    'muenchnerimmobilien': 'Muenchner Immobilien',
    'ausdemhaeuschen': 'Aus dem Haeuschen',
    'hallinger': 'Hallinger Immobilien',
    'cki': 'CKI Immobilien',
    'windisch': 'Windisch Immobilien',
    'im7': 'IM7 GmbH',
    'seimmobilien': 'SE Immobilienmakler',
    'happyimmo': 'Happy Immo',
    'wandl': 'Wandl Immobilien',
    'emslander': 'Emslander & Co',
    'hoser': 'Hoser Immobilien',
    'feuerlein': 'Feuerlein Immobilien',
    'lebenstraum': 'Lebenstraum Immobilien',
    'gschwender': 'Gschwender Immobilien',
    'maurer': 'Maurer Immobilien',
    'pscheidt': 'Pscheidt Immobilien',
    'bechler': 'Bechler Immobilien',
    'isarestate': 'ISAR Estate',
    'wesoly': 'Wesoly Immobilien',
    'westend': 'Immobilien Westend',
    'stierling': 'Stierling Immobilien',
    'finestep': 'Finestep Immobilien',
    'fairhomes': 'Fair Homes',
    'chalet': 'Chalet Immobilien',
    'kraftziller': 'Kraft Ziller Immobilien',
    'wolf': 'Wolf Immobilien',
    'bayergrund': 'Bayergrund Immobilien',
    'immops': 'Immobilien PS',
    'rsi': 'RSI Immobilien',
    'siemax': 'Siemax Immobilien',
    'joseffrei': 'Josef Frei Immobilien',
    'luenendonk': 'Luenendonk Immobilien',
    'harinali': 'Harinali Immobilien',
    'aufrecht': 'Aufrecht Immo',
    'ramonaneckar': 'Ramona Neckar Immobilien',
    'mytropper': 'Mytropper Immobilien',
    'sriimmo': 'SRI Immobilien',
    'zg': 'ZG Immobilien',
    'zardini': 'Zardini Immobilien',
    'reischl': 'Reischl Immobilien',
    'gattinger': 'Gattinger Immobilien',
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


def matches_price_rule(price_text: str) -> bool:
    text = clean_text(str(price_text or ''))
    if not text:
        return False
    if 'anfrage' in text.lower():
        return True

    major = text.split(',')[0]
    digits = re.sub(r'\D', '', major)
    if not digits:
        return False
    return int(digits) > 100000


def has_explicit_price(value: str) -> bool:
    text = clean_text(str(value or ''))
    if not text:
        return False
    lower = text.lower()
    if 'anfrage' in lower:
        return True
    return bool(re.search(r'\d', text))


def is_valid_title(text: str) -> bool:
    text = clean_text(text)
    if not text:
        return False
    normalized = text.lower()
    if normalized in {
        'immobilie kaufen',
        'mehr erfahren',
        'mehr',
        'mehr infos',
        'mehr informationen',
        'hier klicken',
        'hier klicken zum expose zu gelangen',
        'hier klicken zum exposé zu gelangen',
        'zur immobilie',
        'weiterlesen',
        'details',
        'details ansehen',
        'details anzeigen',
        'zum exposé',
        'zum expose',
        'exposé',
        'expose',
        'exposé zum exposé',
        'hauptbild',
    }:
        return False
    if re.fullmatch(r'objekt\s+\d+', normalized):
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
    if not has_explicit_price(price):
        return
    price = format_price(price)
    area = clean_text(area)
    if re.fullmatch(r'\d+\.00', area):
        area = area[:-3]
    item = {
        'title': title,
        'price': price,
        'area_sqm': area,
        'location': location,
        'link': link,
    }
    link_key = ('link', item['link'])
    key = (item['title'], item['price'], item['area_sqm'], item['location'], item['link'])
    if not item['title'] or key in seen or link_key in seen or not matches_price_rule(item['price']):
        return
    seen.add(link_key)
    seen.add(key)
    listings.append(item)


def iter_configured_urls(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from iter_configured_urls(item)


def allowed_fetch_hosts():
    hosts = set()
    for name, value in globals().items():
        if not (name.endswith('_URL') or name.endswith('_URLS')):
            continue
        for configured_url in iter_configured_urls(value):
            hostname = (urlparse(configured_url).hostname or '').lower().rstrip('.')
            if hostname:
                hosts.add(hostname)
                if hostname.startswith('www.'):
                    hosts.add(hostname[4:])
    return hosts


def validate_fetch_url(url):
    parsed = urlparse(url or '')
    hostname = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme.lower() != 'https' or not hostname:
        raise ValueError('Only HTTPS broker URLs are allowed')
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local):
        raise ValueError('Private or link-local fetch targets are not allowed')
    if hostname not in allowed_fetch_hosts():
        raise ValueError('Unapproved broker host')


def clean_location_value(value: str) -> str:
    text = clean_text(str(value or ''))
    if not text:
        return ''

    # Remove common attribute-like artifacts that can leak from broken markup.
    text = re.sub(r'\b(?:class|id|style|href|src|data-[\w-]+)\s*=\s*["\'][^"\']*["\']', ' ', text, flags=re.I)
    text = re.sub(r'\b(?:class|id|style|href|src|data-[\w-]+)\s*=\s*\S+', ' ', text, flags=re.I)
    text = re.sub(r'[\{\}\[\]<>;]', ' ', text)
    text = re.sub(r'(?i)^(?:ort|lage|standort|stadt|stadtteil)\s*:\s*', '', text)
    text = re.sub(r'(?i)^(?:in|bei|von)\s+', '', text)
    text = re.sub(r'(?i)\bauf\s+anfrage\b', '', text)
    # Strip common non-location suffixes from card snippets, e.g. "Neuried Etage".
    text = re.sub(r'(?i)\b(etage|erdgeschoss|obergeschoss|dachgeschoss|eg|og|dg)\b', '', text)
    if '/' in text:
        left, right = [part.strip() for part in text.split('/', 1)]
        if re.search(r'(?i)\b(wohnung|haus|haeuser|grundst[üu]ck|villa|zweifamilienhaus|mehrfamilienhaus|reihenhaus|apartment)\b', right):
            text = left
    text = re.sub(r'\s+', ' ', text).strip(' |,-')
    text = normalize_common_city_spelling(text)

    if not text:
        return ''
    if re.search(r'(?i)(?:class=|<div|</|javascript:|onclick=)', text):
        return ''
    return text


def normalize_path(value: str) -> str:
    parsed = urlparse(value or '')
    path = (parsed.path or '').strip().lower()
    if path.endswith('/'):
        path = path[:-1]
    return path


def is_non_listing_url(value: str) -> bool:
    parsed = urlparse(value or '')
    scheme = (parsed.scheme or '').lower()
    if scheme in {'mailto', 'tel', 'javascript'}:
        return True

    path = normalize_path(value)
    if not path:
        return True

    non_listing_paths = {
        '/angebote',
        '/immobilienportfolio',
        '/immobilienbewertung',
        '/kontakt',
        '/impressum',
        '/datenschutz',
        '/datenschutzerklaerung',
        '/widerrufsrecht',
        '/leistungen',
        '/verkaufen',
        '/vermieten',
        '/bewerten',
        '/suchauftrag',
        '/ansprechpartner',
        '/standorte',
        '/finanzierung',
        '/immobilienfinanzierung',
        '/immobilienkauf',
        '/privater-immobilienverkauf',
        '/richtiger-immobilienpreis',
        '/energetische-sanierung',
        '/leibrente',
        '/sicherheit',
        '/immobilien-fakten-begriffe',
    }
    if path in non_listing_paths:
        return True

    non_listing_prefixes = (
        '/datenschutz',
        '/datenschutzerklaerung',
        '/widerrufsrecht',
        '/leistungen/',
        '/alle-immobilien/',
        '/verkaufen',
        '/vermieten',
        '/bewerten',
        '/finanzierung',
        '/ansprechpartner',
        '/standorte',
        '/suchauftrag',
    )
    if any(path.startswith(prefix) for prefix in non_listing_prefixes):
        return True

    if '/category/' in path or '/kategorie/' in path:
        return True

    return False


def normalize_title_from_link(link: str) -> str:
    path = normalize_path(link)
    if not path:
        return ''
    slug = path.split('/')[-1]
    if not slug:
        return ''
    slug = re.sub(r'\.(html?|php)$', '', slug, flags=re.I)
    slug = re.sub(r'^obj-[a-z0-9_-]+\-?', '', slug, flags=re.I)
    slug = slug.replace('_', ' ').replace('-', ' ')
    slug = re.sub(r'\s+', ' ', slug).strip()
    return clean_text(slug)


def is_generic_navigation_title(title: str) -> bool:
    normalized = clean_text(title).lower()
    blocked = {
        'angebote',
        'portfolio items',
        'verkauf',
        'alle',
        'doppelhaus',
        'bauvorbescheiden',
        'bewertung',
        'bewerten',
        'details anzeigen',
        'detailseite',
        'details zur immobilie',
        'objektdetails ansehen',
        'pdf',
        'neu',
        'preisreduktion',
        'top angebot',
        'sehr',
        'hier klicken zum expose zu gelangen',
        'hier klicken zum exposé zu gelangen',
        'kontakt',
        'impressum',
        'datenschutz',
        'datenschutzerklärung',
        'widerrufsrecht',
        'suchauftrag',
        'wohnungen',
        'grundstücke',
        'gewerbeimmobilien',
        'immobilien',
        'objektart',
        'verkaufen',
        'vermieten',
        'standorte',
        'ansprechpartner',
        'finanzierung',
        'verfügbar kauf',
        'verfugbar kauf',
    }
    return normalized in blocked


def decode_slug_words(value: str) -> str:
    text = unquote(value or '')
    text = text.replace('%c2%b2', ' ').replace('%C2%B2', ' ')
    text = text.replace('_', ' ').replace('-', ' ')
    text = text.replace('ae', 'ä').replace('oe', 'ö').replace('ue', 'ü')
    text = text.replace('ss', 'ß')
    text = re.sub(r'\s+', ' ', text).strip()
    return clean_text(text)


def normalize_common_city_spelling(text: str) -> str:
    value = clean_text(text or '')
    if not value:
        return ''
    value = re.sub(r'\bMnchen\b', 'München', value)
    value = re.sub(r'\bMuenchen\b', 'München', value)
    return value


def is_clean_location_text(value: str) -> bool:
    text = clean_location_value(value)
    if not text:
        return False
    if len(text) < 3:
        return False
    if len(text) > 40:
        return False
    if '"' in text or "'" in text:
        return False
    if re.search(r'(?i)(aria-label|srcset|http|href=|class=|<|>|\{\{|\}\}|code)', text):
        return False
    if re.search(r'\d|€', text):
        return False
    if re.search(r'(?i)\b(kaufpreis|wohnfl|zimmer|objekt|angebot|expose|exposé|details?)\b', text):
        return False
    if re.search(r'(?i)\b(potenzial|potential|familie|bezugsfrei|m[oö]glich|wp\s*content|ansprechender|architektur\s+neu|die\s+einen)\b', text):
        return False
    if text.count('/') > 1:
        return False
    if text.count(' ') >= 3 and re.search(r'(?i)\b(lage|von|mit|und)\b', text):
        return False
    if re.search(r'(?i)\b(objektinformationen|objektnummer|objekte|immobilien|immobilienangebote|kontakt|verf[uü]gbar|bestlage|top\s+lage|begehrter|kaufpreis|zimmer|flaeche|wohnfl|ca|herzlich|zur[uü]ck|expose|exposé|details?|detailseite|pdf|informationen|portfolio\s+items|verkauf|doppelhaus|bauvorbescheiden|högerstraße)\b', text):
        return False
    if re.search(r'(?i)^(der|die|das)\s+[A-ZÄÖÜa-zäöüß\-]+$', text):
        return False
    if re.search(r'(?i)\b(hobbyraum|loggia(?:s|en)?|ogm|anlage)\b', text):
        return False
    return bool(re.search(r'[A-Za-zÄÖÜäöüß]', text))


def extract_location_from_link(link: str) -> str:
    parsed = urlparse(link or '')
    path = unquote((parsed.path or '').strip('/'))
    if not path:
        return ''

    hostname = (parsed.hostname or '').lower()
    slug_text = path.lower().replace('_', '-').replace('.', '-')
    if 'aundowohnbau.de' in hostname:
        if re.search(r'germering', slug_text):
            return 'Germering'
        if re.search(r'muenchen|münchen|giesing|laim|schwabing', slug_text):
            return 'München'
    if 'elvira-immo.de' in hostname:
        for candidate in ('germering', 'maxvorstadt', 'harlaching', 'bogenhausen'):
            if candidate in slug_text:
                return candidate.title()
    if 'martina-schwarz-immobilien.de' in hostname and path.lower().endswith('.pdf'):
        for candidate in ('groebenzell', 'perlach', 'gilching', 'ismaning', 'unterhaching', 'giesing'):
            if candidate in slug_text:
                return decode_slug_words(candidate).title()
    if 'sqmeter.de' in hostname:
        city_match = re.search(r'(?:verkauf|miete)/(?:muenchen|münchen)-([a-z-]+)', slug_text)
        if city_match:
            return 'München'
    if 'seebauer-immobilien.de' in hostname and re.search(r'(?:^|-)muenchen(?:-|$)', slug_text):
        return 'München'
    if 'mueller-groscurth-immobilien.de' in hostname:
        for candidate in ('neuhausen', 'obergiesing', 'muenchen', 'münchen', 'germering', 'gauting'):
            if candidate in slug_text:
                return decode_slug_words(candidate).title()
    if 'riedl-makler.de' in hostname:
        for candidate in ('pasing', 'giesing', 'gauting', 'muenchen', 'münchen'):
            if candidate in slug_text:
                return decode_slug_words(candidate).title()

    segments = [seg for seg in path.split('/') if seg]
    if not segments:
        return ''

    last = segments[-1].lower()
    in_match = re.search(r'-in-([a-z0-9\-]+?)(?:-(?:kaufen|verkaufen|mieten)|$)', last, re.I)
    if in_match:
        return decode_slug_words(in_match.group(1)).title()

    # For paths like /Muenchen/Objekt-... use the first segment as city fallback.
    first = segments[0]
    if re.fullmatch(r'[A-Za-zÄÖÜäöüß\-]+', first) and first.lower() not in {
        'immobilie', 'immobilien', 'angebote', 'objekt', 'objekte', 'index.php4', 'kaufen'
    }:
        return decode_slug_words(first).title()

    # Links like /objekt/haidhausen/ can carry the place directly in the 2nd path segment.
    if len(segments) >= 2 and segments[0].lower() in {'objekt', 'objekte', 'immobilie', 'immobilien'}:
        second = segments[1]
        if re.fullmatch(r'[A-Za-zÄÖÜäöüß\-]{3,30}', second):
            candidate = decode_slug_words(second).title()
            if is_clean_location_text(candidate):
                return candidate

    # Some exposé slugs end with a city token, e.g. ...-hamburg.
    trailing_city = re.search(r'-([a-zäöüß]{3,})$', last, re.I)
    if trailing_city:
        token = trailing_city.group(1).lower()
        blocked_tokens = {
            'wohnung', 'haus', 'zimmer', 'garten', 'balkon', 'garage', 'kapitalanlage',
            'verkauft', 'reserviert', 'angebot', 'objekt', 'kaufen', 'mieten', 'lage', 'naehe',
            'hobbyraum', 'loggia', 'loggien', 'loggias', 'ogm', 'anlage'
        }
        if token not in blocked_tokens:
            candidate = normalize_common_city_spelling(decode_slug_words(token).title())
            if is_clean_location_text(candidate):
                return candidate

    return ''


def extract_location_from_title(title: str) -> str:
    text = clean_text(title or '')
    if not text:
        return ''

    for candidate in ('Neubiberg', 'Ottobrunn', 'Kaufbeuren', 'Waldtrudering', 'Steinhöring', 'Kirchheim', 'Perlach', 'Maxhof'):
        if re.search(rf'\b{re.escape(candidate)}\b', text, re.I):
            return candidate

    # Some brokers use city-only titles, e.g. "Unterhaching".
    if re.fullmatch(r'[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,}(?:/[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,})?', text):
        candidate = normalize_common_city_spelling(text)
        if is_clean_location_text(candidate):
            return candidate

    # Titles can start with city/district before descriptive text, e.g. "Schwabing/Nahe ...".
    slash_prefix = re.match(r'^([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,}(?:/[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,})?)\s*/\s*', text)
    if slash_prefix:
        candidate = normalize_common_city_spelling(clean_text(slash_prefix.group(1)))
        if is_clean_location_text(candidate):
            return candidate

    text = re.sub(r'^[A-ZÄÖÜ][A-ZÄÖÜ\s&\.-]{2,}\s*:\s*', '', text)

    prefix_colon_match = re.match(r'^([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,}(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,})?)\s*:\s+', text)
    if prefix_colon_match:
        candidate = normalize_common_city_spelling(clean_text(prefix_colon_match.group(1)))
        if is_clean_location_text(candidate):
            return candidate

    lower_prefix_match = re.match(r'^([a-zäöüß][a-zäöüß\-]{2,}(?:\s+[a-zäöüß][a-zäöüß\-]{2,})?)\s*[:\-–]\s+', text)
    if lower_prefix_match:
        candidate = normalize_common_city_spelling(clean_text(lower_prefix_match.group(1)).title())
        if is_clean_location_text(candidate):
            return candidate

    munich_district_prefix = re.match(r'^(?:Mü|Mue|Muenchen|München)[\-\s]+[A-Za-zÄÖÜäöüß\-]{2,}\s*:\s+', text, re.I)
    if munich_district_prefix:
        return 'München'

    match = re.search(r'\b(?:in|bei|von)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+)?(?:\s*/\s*[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+)?)', text)
    if match:
        candidate = normalize_common_city_spelling(clean_text(match.group(1)))
        if is_clean_location_text(candidate):
            return candidate

    prefix_match = re.match(r'^([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,}(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,})?)\s+-\s+', text)
    if prefix_match:
        candidate = normalize_common_city_spelling(clean_text(prefix_match.group(1)))
        if is_clean_location_text(candidate):
            return candidate

    trailing_match = re.search(r'\b(?:Station|Lage|Ort|in)\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,}(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,})?)\s*$', text)
    if trailing_match:
        candidate = normalize_common_city_spelling(clean_text(trailing_match.group(1)))
        if is_clean_location_text(candidate):
            return candidate

    # Titles like "Krailling am Rand zu Planegg" can expose the city before "am ...".
    am_phrase_match = re.match(r'^([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,}(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,})?)\s+am\s+', text)
    if am_phrase_match:
        candidate = normalize_common_city_spelling(clean_text(am_phrase_match.group(1)))
        if is_clean_location_text(candidate):
            return candidate

    am_after_dash_match = re.search(r'\s-\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,}(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,})?)\s+am\s+', text)
    if am_after_dash_match:
        candidate = normalize_common_city_spelling(clean_text(am_after_dash_match.group(1)))
        if is_clean_location_text(candidate):
            return candidate

    # Some brokers mention Munich via district/landmark wording without explicit city value.
    if re.search(
        r'(?i)olympiapark|olympia-einkaufszentrum|haidhausen|theresienwiese|g[aä]rtnerplatz|ludwigsvorstadt|isarvorstadt|pasing|laim|hadern|feldmoching',
        text,
    ):
        return 'München'

    return ''


def apply_listing_rules(raw_listings):
    normalized = []
    seen_links = set()
    seen_rows = set()

    for raw in raw_listings or []:
        if not isinstance(raw, dict):
            continue

        title = clean_text(raw.get('title', ''))

        link = clean_text(raw.get('link', ''))
        if not link or is_non_listing_url(link):
            continue

        if is_generic_navigation_title(title):
            title = normalize_title_from_link(link)
        if not is_valid_title(title):
            continue

        if is_generic_navigation_title(title):
            continue

        raw_price = clean_text(str(raw.get('price', '')))
        if not has_explicit_price(raw_price):
            continue
        price = format_price(raw_price)
        if not matches_price_rule(price):
            continue

        area = clean_text(raw.get('area_sqm', ''))
        raw_location = clean_text(raw.get('location', ''))
        location = resolve_listing_location(raw_location, title, link)

        link_key = link.rstrip('/').lower()
        row_key = (title, price, area, location, link_key)
        if link_key in seen_links or row_key in seen_rows:
            continue

        seen_links.add(link_key)
        seen_rows.add(row_key)
        normalized.append({
            'title': title,
            'price': price,
            'area_sqm': area,
            'location': location,
            'link': link,
        })

    return normalized


def resolve_listing_location(raw_location: str, title: str, link: str) -> str:
    location = clean_location_value(raw_location)
    if not is_clean_location_text(location):
        location = ''

    # Recover missing/weak locations with the same low-risk fallback chain used in source-specific fixes.
    if not location:
        postcode_city = extract_postcode_city_location(raw_location)
        if is_clean_location_text(postcode_city):
            location = postcode_city
    if not location:
        title_postcode_city = extract_postcode_city_location(title)
        if is_clean_location_text(title_postcode_city):
            location = title_postcode_city
    if not location:
        title_location = extract_location_from_title(title)
        if is_clean_location_text(title_location):
            location = title_location
    if not location:
        link_location = extract_location_from_link(link)
        if is_clean_location_text(link_location):
            location = link_location

    if location:
        return normalize_common_city_spelling(location)
    return UNKNOWN_LOCATION


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
    location_match = re.search(r'(?i)(?:Ort|Lage|Standort|Wohnort|in|gelegen in)\s*:?\s*([A-ZÄÖÜ][^|<\n]+)', text)
    if location_match:
        location = clean_text(location_match.group(1))
        if not re.match(r'^[A-ZÄÖÜ]', location):
            return clean_text(fallback)
        location = re.split(r'[.;|]', location, maxsplit=1)[0]
        location = re.sub(r'^(?:ca\.?\s*)?\d{4,5}\s+', '', location)
        location = re.sub(r'\s+-\s+\d+$', '', location)
        location = location.rstrip('.,;')
        return location
    return clean_text(fallback)


def extract_postcode_city_location(text: str) -> str:
    chunk = clean_text(text or '')
    if not chunk:
        return ''
    match = re.search(r'\b\d{4,5}\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-\/]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-\/]+){0,2})', chunk)
    if not match:
        return ''
    city = clean_text(match.group(1)).strip('.,;|')
    if not is_clean_location_text(city):
        return ''
    return city


def extract_location_from_json_ld(html: str) -> str:
    scripts = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S)
    for payload in scripts:
        text = clean_text(payload)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue

        for node in iter_json_ld_nodes(data):
            if not isinstance(node, dict):
                continue
            address = node.get('address', {})
            if not isinstance(address, dict):
                continue

            locality = clean_location_value(address.get('addressLocality') or '')
            if is_clean_location_text(locality):
                return locality

            region = clean_location_value(address.get('addressRegion') or '')
            if is_clean_location_text(region):
                return region
    return ''


def extract_location_from_description_text(text: str) -> str:
    chunk = clean_text(text or '')
    if not chunk:
        return ''

    patterns = [
        r'\bStadtteil\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+){0,2})',
        r'\bin\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+){0,2})\s+(?:liegt|befindet|gelegen|nur|nahe|bei|unweit)\b',
        r'\b([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+)\s+ist\s+ein(?:e)?\s+[^.]{0,80}\bStadtteil\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, chunk, re.I)
        if not match:
            continue
        candidate = clean_location_value(match.group(1))
        if re.match(r'(?i)^m[üu]nchen\b', candidate):
            candidate = 'München'
        candidate = normalize_common_city_spelling(candidate)
        if is_clean_location_text(candidate):
            return candidate
    return ''


def recover_location_from_detail_page(link: str, title: str, current_location: str = '') -> str:
    location = clean_location_value(current_location)
    if is_clean_location_text(location):
        return normalize_common_city_spelling(location)

    detail_html = ''
    try:
        detail_html = fetch_html(link, timeout=20)
    except Exception:
        detail_html = ''

    if detail_html:
        detail_text = clean_text(detail_html)
        candidates = [
            extract_location_text(detail_text, ''),
            extract_postcode_city_location(detail_text),
            extract_location_from_json_ld(detail_html),
            extract_location_from_description_text(detail_text),
        ]
        for candidate in candidates:
            candidate = normalize_common_city_spelling(clean_location_value(candidate))
            if is_clean_location_text(candidate):
                return candidate

    for candidate in (extract_location_from_title(title), extract_location_from_link(link)):
        candidate = normalize_common_city_spelling(clean_location_value(candidate))
        if is_clean_location_text(candidate):
            return candidate

    return ''


def fetch_html(url: str, timeout: int = 20) -> str:
    validate_fetch_url(url)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    response = urllib.request.urlopen(req, timeout=timeout)
    try:
        body = response.read()
        headers = getattr(response, 'headers', {}) or {}
        encoding = (headers.get('Content-Encoding') or '').lower()
    finally:
        close = getattr(response, 'close', None)
        if callable(close):
            close()

    if encoding == 'gzip' or body.startswith(b'\x1f\x8b'):
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    elif encoding == 'deflate':
        try:
            body = zlib.decompress(body)
        except Exception:
            try:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
            except Exception:
                pass

    return body.decode('utf-8', 'ignore')


def parse_link_cards(base_url: str, html: str, href_hint: str = r'(immobilie|objekt|expose|angebot|kauf)'):
    listings = []
    seen = set()
    pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.S | re.I)
    for match in pattern.finditer(html):
        href_raw = clean_text(match.group(1))
        if not re.search(href_hint, href_raw, re.I):
            continue
        href = urljoin(base_url, href_raw)
        if not re.search(r'/(?:immobilie|immobilien|objekt|objekte|expose|exposé|angebote)/|cmd=expose|obj-|angebotsverfahren|expose|\.pdf$', href.lower()):
            continue
        # Parse forward-only context so a later card cannot inherit price/location from the previous card.
        chunk_size = 6000 if 'reichenberger-immobilien.de' in base_url else 2200
        chunk = html[match.start():match.start() + chunk_size]

        title = clean_text(match.group(2))
        if not is_valid_title(title) or is_generic_navigation_title(title):
            title_match = re.search(r'<h2[^>]*>(.*?)</h2>|<h3[^>]*>(.*?)</h3>', chunk, re.S | re.I)
            if title_match:
                title = clean_text(title_match.group(1) or title_match.group(2) or '')
        if not is_valid_title(title) or is_generic_navigation_title(title):
            slug_title = normalize_title_from_link(href)
            if is_valid_title(slug_title):
                title = slug_title
        if not is_valid_title(title) or is_generic_navigation_title(title):
            for payload in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', chunk, re.I | re.S):
                try:
                    data = json.loads(payload.strip())
                except Exception:
                    continue
                candidate = ''
                for node in iter_json_ld_nodes(data):
                    if not isinstance(node, dict):
                        continue
                    node_name = clean_text(node.get('name') or node.get('headline') or '')
                    if not is_valid_title(node_name):
                        continue
                    node_url = clean_text(node.get('url') or node.get('@id') or '')
                    if node_url:
                        full_node_url = urljoin(base_url, node_url)
                        if normalize_path(full_node_url) == normalize_path(href):
                            candidate = node_name
                            break
                    if not candidate:
                        candidate = node_name
                if is_valid_title(candidate):
                    title = candidate
                    break
        if not is_valid_title(title):
            continue

        chunk_text = clean_text(chunk)
        price_match = re.search(r'Kaufpreis\s*:?\s*([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?|auf Anfrage)', chunk_text, re.I)
        if not price_match:
            price_match = re.search(r'Preis\s*:?\s*([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?|auf Anfrage)', chunk_text, re.I)
        if not price_match:
            price_match = re.search(r'([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?)\s*(?:EUR|€)', chunk_text, re.I)

        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*(?:ca\.?\s*)?([0-9.,]+)', chunk_text, re.I)
        if not area_match:
            area_match = re.search(r'([0-9.,]+)\s*m²', chunk_text, re.I)
        address_match = re.search(
            r'<[^>]+class=["\'][^"\']*\bes-address\b[^"\']*["\'][^>]*>(.*?)</[^>]+>',
            chunk,
            re.I | re.S,
        )
        location_match = re.search(r'>\s*(?:Ort|Lage|Standort|Stadt)\s*:?\s*([^<\n|]+)', chunk, re.I)

        # Require concrete listing facts to avoid classifying nav/service links as listings.
        if not (price_match or area_match):
            continue

        price = price_match.group(1) if price_match else ''
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(address_match.group(1)) if address_match else ''
        if not location:
            location = clean_text(location_match.group(1)) if location_match else extract_location_text(chunk_text, '')
        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


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
            if has_explicit_price(price):
                price = format_price(price)
            else:
                price = ''

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

        price = clean_text(price_match.group(1)) if price_match else ''
        if has_explicit_price(price):
            price = format_price(price)
        else:
            price = ''
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

        price = format_price(price_match.group(1)) if price_match else ''
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
    html = fetch_html(FIRSTPLACE_URL, timeout=25)

    listings = []
    seen = set()
    matches = list(re.finditer(r'FIRSTPLACE\s*-\s*([^<]+)', html, re.I))
    for index, match in enumerate(matches):
        title = clean_text(match.group(1))
        if not is_valid_title(title):
            continue
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        block = html[match.start():block_end]
        price_match = re.search(r'(?:Preis|Kaufpreis)?\s*:?[\s\u00a0]*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*€', clean_text(block), re.I)
        area_match = re.search(r'([0-9.,]+)\s*m²', block, re.I)
        location_match = re.search(r'\b((?:[A-ZÄÖÜa-zäöüß][^<,]{2,40},\s*)?\d{5}\s+[A-ZÄÖÜa-zäöüß][A-Za-zÄÖÜäöüß\-]+(?:\s+\([^)]*\))?)', clean_text(block))

        price = price_match.group(1) if price_match else ''
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else ''
        add_listing(listings, seen, title, price, area, location, f'{FIRSTPLACE_URL}#offer-{index + 1}')

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

        price = format_price(price_match.group(1)) if price_match else ''
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

        price = format_price(price_match.group(1)) if price_match else ''
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
            price = price_match.group(1) if price_match else ''
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
            price = card_price_match.group(1) if card_price_match else ''

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
            price = price_matches[-1] if price_matches else ''

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
            price = clean_text(price_match.group(1)) if price_match else ''
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
        price = price_match.group(1) if price_match else ''
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

        price = price_match.group(1) if price_match else ''
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else extract_location_text(chunk_text, '')
        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_jalea_listings():
    listings = []
    seen = set()

    try:
        overview_html = fetch_html(JALEA_URL, timeout=20)
    except Exception:
        return listings

    for match in re.finditer(r'<article\b[^>]*class=["\'][^"\']*frymo-listing-item[^"\']*["\'][^>]*>(.*?)</article>', overview_html, re.S | re.I):
        block = match.group(1)
        title_match = re.search(r'<h3\b[^>]*class=["\'][^"\']*frymo-listing-title[^"\']*["\'][^>]*>\s*<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', block, re.S | re.I)
        if not title_match:
            continue

        href = urljoin(JALEA_URL, clean_text(title_match.group(1)))
        title = clean_text(title_match.group(2))
        if not is_valid_title(title):
            continue

        location_match = re.search(r'<div class="frymo-listing-location"[^>]*>.*?</i>\s*(.*?)</div>', block, re.S | re.I)
        location = clean_text(location_match.group(1)) if location_match else ''

        price = ''
        area = ''
        try:
            detail_html = fetch_html(href, timeout=20)

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

        price = ''
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

        price = ''
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
        if not re.search(r'/(?:immobilie|objekt|expose|angebot)/|cmd=expose|obj-', href_raw, re.I):
            continue

        href = urljoin(SIS_URL, href_raw)
        chunk = html[max(0, match.start() - 900):match.start() + 900]
        anchor_title = clean_text(match.group(2))
        title = anchor_title
        if re.match(r'(?i)^zum\s+objekt', title):
            title = ''
        if re.match(r'(?i)^immobilie\s+anzeigen', title) or is_generic_navigation_title(title):
            title = normalize_title_from_link(href)
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
        if not location_match:
            location_match = re.search(r'<span[^>]*class="[^"]*(?:location|ort)[^"]*"[^>]*>(.*?)</span>', chunk, re.I | re.S)
        if not location_match:
            location_match = re.search(r'data-location\s*=\s*"([^"]+)"', chunk, re.I)

        if not (price_match or area_match or location_match):
            continue

        price = price_match.group(1) if price_match else ''
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else extract_location_text(chunk_text, '')
        if not is_clean_location_text(location):
            location = recover_location_from_detail_page(href, title, location)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION
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
        price = clean_text(price_match.group(1)) if price_match else ''
        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_nikki_listings():
    try:
        html = fetch_html(NIKKI_URL)
    except Exception:
        return []
    return parse_link_cards(NIKKI_URL, html, r'(immobilie|objekt|portfolio|angebot|expose)')


def fetch_tsc_listings():
    listings = []
    seen = set()
    for url in TSC_URLS:
        try:
            html = fetch_html(url)
        except Exception:
            continue
        for item in parse_link_cards(url, html, r'(immobilie|objekt|haus|wohnung|villa|kaufen)'):
            if not is_clean_location_text(item.get('location', '')):
                repaired = recover_location_from_detail_page(
                    clean_text(item.get('link', '')),
                    clean_text(item.get('title', '')),
                    clean_text(item.get('location', '')),
                )
                item['location'] = repaired if is_clean_location_text(repaired) else UNKNOWN_LOCATION
            key = (item['title'], item['price'], item['area_sqm'], item['location'], item['link'])
            if key in seen:
                continue
            seen.add(key)
            listings.append(item)
            if len(listings) >= 12:
                return listings
    return listings


def fetch_imothek_listings():
    try:
        html = fetch_html(IMOTHEK_URL)
    except Exception:
        return []
    return parse_link_cards(IMOTHEK_URL, html, r'(immobilie|objekt|expose|angebot)')


def fetch_vr_listings():
    listings = []
    seen = set()
    for url in VR_URLS:
        try:
            html = fetch_html(url)
        except Exception:
            continue
        for item in parse_link_cards(url, html, r'(immobilie|objekt|expose|angebote|kaufen|haus|wohnung)'):
            key = (item['title'], item['price'], item['area_sqm'], item['location'], item['link'])
            if key in seen:
                continue
            seen.add(key)
            listings.append(item)
            if len(listings) >= 12:
                return listings
    return listings


def fetch_stolze_listings():
    try:
        html = fetch_html(STOLZE_URL)
    except Exception:
        return []
    return parse_link_cards(STOLZE_URL, html, r'(immobilie|objekt|expose|angebot|kaufen)')


def fetch_realwert_listings():
    try:
        html = fetch_html(REALWERT_URL)
    except Exception:
        return []
    return parse_link_cards(REALWERT_URL, html, r'(immobilie|objekt|expose|angebot)')


def fetch_eden_listings():
    try:
        html = fetch_html(EDEN_URL)
    except Exception:
        return []
    listings = parse_link_cards(EDEN_URL, html, r'(immobilie|objekt|expose|angebot|kaufen)')
    for row in listings:
        location = row.get('location', '')
        if not is_clean_location_text(location):
            row['location'] = extract_location_from_link(row.get('link', ''))
        if not is_clean_location_text(row.get('location', '')):
            row['location'] = extract_location_from_title(row.get('title', ''))
        if not is_clean_location_text(row.get('location', '')):
            row['location'] = UNKNOWN_LOCATION
    return listings


def fetch_imliving_listings():
    listings = []
    seen = set()

    try:
        html = fetch_html(IMLIVING_URL)
    except Exception:
        return listings

    starts = list(re.finditer(r'<div\s+class="immo_offers_item"([^>]*)>', html, re.I))
    for index, start in enumerate(starts):
        attrs = start.group(1)
        next_start = starts[index + 1].start() if index + 1 < len(starts) else len(html)
        chunk = html[start.start():next_start]

        href_match = re.search(r'<a[^>]+href="([^"]+)"', chunk, re.I)
        title_match = re.search(r'<h3[^>]*>(.*?)</h3>', chunk, re.I | re.S)
        if not href_match or not title_match:
            continue

        href = urljoin(IMLIVING_URL, clean_text(href_match.group(1)))
        title = clean_text(title_match.group(1))
        if not is_valid_title(title):
            continue

        location_match = re.search(r'data-location="([^"]+)"', attrs, re.I)
        if not location_match:
            location_match = re.search(r'<span class="location"[^>]*>(.*?)</span>', chunk, re.I | re.S)
        location = clean_text(location_match.group(1)) if location_match else ''

        area_match = re.search(r'Wfl\.\s*:\s*([0-9.,]+)\s*m²', chunk, re.I)
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

        price_match = re.search(r'data-price="([^"]+)"', attrs, re.I)
        price = ''
        if price_match and re.search(r'\d', price_match.group(1)):
            price = f"{price_match.group(1)} €"
        else:
            chunk_text = clean_text(chunk)
            fallback_price = re.search(r'Kaufpreis\s*:\s*([0-9.]+(?:\s?[.,]\d{3})*(?:\s?[.,]\d{2})?|auf Anfrage)', chunk_text, re.I)
            if fallback_price:
                price = fallback_price.group(1)

        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_webau_listings():
    try:
        html = fetch_html(WEBAU_URL)
    except Exception:
        return []
    listings = parse_link_cards(WEBAU_URL, html, r'(cmd=expose|immobilie|objekt|angebot)')
    for row in listings:
        location = row.get('location', '')
        if not is_clean_location_text(location):
            row['location'] = extract_location_from_link(row.get('link', ''))
        if not is_clean_location_text(row.get('location', '')):
            row['location'] = extract_location_from_title(row.get('title', ''))
        if not is_clean_location_text(row.get('location', '')):
            row['location'] = UNKNOWN_LOCATION
    return listings


def fetch_mar_listings():
    try:
        html = fetch_html(MAR_URL)
    except Exception:
        return []
    return parse_link_cards(MAR_URL, html, r'(immobilie|objekt|expose|angebot)')


def fetch_seeimmo_listings():
    try:
        html = fetch_html(SEEIMMO_URL)
    except Exception:
        return []
    return parse_link_cards(SEEIMMO_URL, html, r'(immobilie|objekt|expose|angebot)')


def fetch_heidinger_listings():
    try:
        html = fetch_html(HEIDINGER_URL)
    except Exception:
        return []
    return parse_link_cards(HEIDINGER_URL, html, r'(immobilie|objekt|expose|angebot|kaufobjekt)')


def fetch_funer_listings():
    try:
        html = fetch_html(FUNER_URL)
    except Exception:
        return []
    return parse_link_cards(FUNER_URL, html, r'(immobilie|objekt|expose|angebot)')


def merge_listing_rows(base_rows, extra_rows):
    merged = []
    seen = set()
    for row in (base_rows or []) + (extra_rows or []):
        if not isinstance(row, dict):
            continue
        key = (
            clean_text(row.get('title', '')),
            clean_text(row.get('price', '')),
            clean_text(row.get('area_sqm', '')),
            clean_text(row.get('location', '')),
            clean_text(row.get('link', '')).rstrip('/').lower(),
        )
        if not key[-1] or key in seen:
            continue
        seen.add(key)
        merged.append(row)
        if len(merged) >= 12:
            break
    return merged


def extract_embedded_urls(base_url: str, html: str):
    found = []
    patterns = [
        r'<iframe[^>]+src=["\']([^"\']+)["\']',
        r'data-src=["\']([^"\']+)["\']',
        r'data-url=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.I):
            url = urljoin(base_url, clean_text(match.group(1)))
            if re.search(r'(immobilie|objekt|expose|search|angebote|kaufen|api|feed)', url, re.I):
                found.append(url)

    seen = set()
    result = []
    for url in found:
        key = url.rstrip('/').lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result[:6]


def fetch_generic_broker_listings(base_url: str, href_hint: str):
    try:
        html = fetch_html(base_url)
    except Exception:
        return []
    return parse_link_cards(base_url, html, href_hint)


def fetch_external_broker_listings(base_url: str, href_hint: str):
    try:
        html = fetch_html(base_url)
    except Exception:
        return []

    listings = parse_link_cards(base_url, html, href_hint)
    extras = []
    for embedded_url in extract_embedded_urls(base_url, html):
        try:
            embedded_html = fetch_html(embedded_url)
        except Exception:
            continue
        extras.extend(parse_link_cards(embedded_url, embedded_html, href_hint))

    return merge_listing_rows(listings, extras)


def fetch_source_specific_broker_listings(base_url: str, href_hint: str, detail_hint: str):
    listings = fetch_generic_broker_listings(base_url, href_hint)
    if listings:
        return listings

    try:
        html = fetch_html(base_url)
    except Exception:
        return []

    parsed = []
    seen = set()
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href_raw = clean_text(match.group(1))
        href = urljoin(base_url, href_raw)
        if not re.search(detail_hint, href, re.I):
            continue

        chunk = html[max(0, match.start() - 1800):match.start() + 1800]
        chunk_text = clean_text(chunk)

        title = clean_text(match.group(2))
        if not is_valid_title(title):
            heading_match = re.search(r'<h2[^>]*>(.*?)</h2>|<h3[^>]*>(.*?)</h3>|title=["\']([^"\']+)["\']', chunk, re.I | re.S)
            if heading_match:
                title = clean_text(heading_match.group(1) or heading_match.group(2) or heading_match.group(3) or '')
        if not is_valid_title(title):
            continue

        price_match = re.search(r'(?:Kaufpreis|Preis)\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', chunk_text, re.I)
        if not price_match:
            price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:EUR|€)', chunk_text, re.I)
        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*(?:ca\.?\s*)?([0-9.,]+)', chunk_text, re.I)
        if not area_match:
            area_match = re.search(r'([0-9.,]+)\s*m²', chunk_text, re.I)

        location = extract_location_text(chunk_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = ''

        price = price_match.group(1) if price_match else ''
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        add_listing(parsed, seen, title, price, area, location, href)

    return parsed[:12]


def fetch_source_specific_with_embedded_retry(base_url: str, href_hint: str, detail_hint: str):
    listings = fetch_source_specific_broker_listings(base_url, href_hint, detail_hint)
    if listings:
        return listings

    try:
        html = fetch_html(base_url)
    except Exception:
        return []

    extras = []
    extras.extend(parse_link_cards(base_url, html, href_hint))
    extras.extend(parse_json_ld_listings(base_url, html))
    extras.extend(parse_price_blocks_without_links(base_url, html))
    extras.extend(parse_searchdetails_cards(base_url, html))

    for embedded_url in extract_embedded_urls(base_url, html):
        try:
            embedded_html = fetch_html(embedded_url)
        except Exception:
            continue

        extras.extend(parse_link_cards(embedded_url, embedded_html, href_hint))
        extras.extend(parse_json_ld_listings(embedded_url, embedded_html))
        extras.extend(parse_price_blocks_without_links(embedded_url, embedded_html))
        extras.extend(parse_searchdetails_cards(embedded_url, embedded_html))

    return merge_listing_rows([], extras)[:12]


def fetch_wangenheim_listings_retry_alt():
    urls = [WANGENHEIM_URL]
    if 'page=1' in WANGENHEIM_URL:
        urls.append(WANGENHEIM_URL.replace('page=1', 'page=2'))

    merged = []
    for url in urls:
        try:
            html = fetch_html(url)
        except Exception:
            continue

        merged = merge_listing_rows(merged, parse_link_cards(url, html, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen|obj-|details)'))
        merged = merge_listing_rows(merged, parse_json_ld_listings(url, html))
        merged = merge_listing_rows(merged, parse_price_blocks_without_links(url, html))

    return merged[:12]


def fetch_mytropper_listings_retry_alt():
    try:
        html = fetch_html(MYTROPPER_URL)
    except Exception:
        return []

    rows = []
    rows = merge_listing_rows(rows, parse_searchdetails_cards(MYTROPPER_URL, html))
    rows = merge_listing_rows(rows, parse_price_blocks_without_links(MYTROPPER_URL, html))
    rows = merge_listing_rows(rows, parse_json_ld_listings(MYTROPPER_URL, html))
    rows = merge_listing_rows(rows, parse_link_cards(MYTROPPER_URL, html, r'(cmd=searchDetails|cmd=expose|obj-|immobilie|objekt|angebot|kauf)'))
    return rows[:12]


def fetch_elvira_listings_retry_alt():
    try:
        html = fetch_html(ELVIRA_URL)
    except Exception:
        return []

    listings = parse_property_link_cards(ELVIRA_URL, html, r'/immobilienangebote/(?!$)')
    if listings:
        return listings

    links = []
    for href_raw in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        if not re.search(r'/immobilienangebote/(?!$)', href_raw, re.I):
            continue
        href = urljoin(ELVIRA_URL, clean_text(href_raw))
        if href not in links:
            links.append(href)

    listings = []
    seen = set()
    for href in links[:20]:
        try:
            detail_html = fetch_html(href)
        except Exception:
            continue

        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        title = re.sub(r'\s+\|\s*ELVIRA.*$', '', title, flags=re.I).strip(' -|')
        if not is_valid_title(title):
            title = normalize_title_from_link(href)
        if not is_valid_title(title):
            continue

        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        price = price_match.group(1) if price_match else ''
        if not price and re.search(r'preis\s+auf\s+anfrage|auf\s+anfrage', detail_text, re.I):
            price = 'Preis auf Anfrage'

        area = extract_area_text(detail_text)
        location = extract_location_text(detail_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings[:12]


def fetch_isarestate_listings_retry_alt():
    try:
        html = fetch_html(ISARESTATE_URL)
    except Exception:
        return []

    links = []
    for href_raw in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        if not re.search(r'/immobilie/', href_raw, re.I):
            continue
        href = urljoin(ISARESTATE_URL, clean_text(href_raw))
        if href not in links:
            links.append(href)

    listings = []
    seen = set()
    for href in links[:24]:
        try:
            detail_html = fetch_html(href)
        except Exception:
            continue

        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        title = re.sub(r'\s+\|\s*ISAR\s*Estate.*$', '', title, flags=re.I).strip(' -|')
        if not is_valid_title(title):
            title = normalize_title_from_link(href)
        if not is_valid_title(title):
            continue

        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        price = price_match.group(1) if price_match else ''
        if not price and re.search(r'preis\s+auf\s+anfrage|auf\s+anfrage', detail_text, re.I):
            price = 'Preis auf Anfrage'

        area = extract_area_text(detail_text)
        location = extract_location_text(detail_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings[:12]


def fetch_vonrodenhausen_listings_retry_alt():
    try:
        html = fetch_html(VONRODENHAUSEN_URL)
    except Exception:
        return []

    listings = []
    seen = set()
    card_pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']*/aktuelle-angebote/[^"\']+-e\d+)["\'][^>]*class=["\'][^"\']*immo_item[^"\']*["\'][^>]*>(.*?)</a>',
        re.I | re.S,
    )

    for match in card_pattern.finditer(html):
        href = urljoin(VONRODENHAUSEN_URL, clean_text(match.group(1)))
        card_html = match.group(2)
        card_text = clean_text(card_html)

        title_match = re.search(r'<h4[^>]*>(.*?)</h4>', card_html, re.I | re.S)
        title = clean_text(title_match.group(1)) if title_match else ''
        if not is_valid_title(title):
            title = normalize_title_from_link(href)
        if not is_valid_title(title):
            continue

        location_match = re.search(r'<h5[^>]*>(.*?)</h5>', card_html, re.I | re.S)
        location = clean_text(location_match.group(1)) if location_match else ''

        price_match = re.search(r'Kaufpreis\s*:?\s*€?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)', card_text, re.I)
        price = price_match.group(1) if price_match else ''
        if not price and re.search(r'preis\s+auf\s+anfrage|auf\s+anfrage', card_text, re.I):
            price = 'Preis auf Anfrage'

        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*([0-9.,]+)', card_text, re.I)
        area = area_match.group(1) if area_match else ''

        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings[:12]


def fetch_wohnref_listings_retry_alt():
    try:
        html = fetch_html(WOHNREF_URL)
    except Exception:
        return []

    detail_pattern = re.compile(
        r'(?:data-url|href)=["\']([^"\']+/immobilien/(?:haus|wohnung|grundstueck)[^"\']+-wrm\d+/?)["\']',
        re.I,
    )
    detail_links = []
    for raw in detail_pattern.findall(html):
        href = urljoin(WOHNREF_URL, clean_text(raw))
        if href not in detail_links:
            detail_links.append(href)

    listings = []
    seen = set()
    for href in detail_links[:24]:
        try:
            detail_html = fetch_html(href)
        except Exception:
            continue

        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        title = re.sub(r'\s*\|\s*WOHNREF.*$', '', title, flags=re.I).strip(' -|')
        if not is_valid_title(title):
            title = normalize_title_from_link(href)
        if not is_valid_title(title):
            continue

        price_match = re.search(r'Kaufpreis\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        price = price_match.group(1) if price_match else ''
        if not price and re.search(r'preis\s+auf\s+anfrage|auf\s+anfrage', detail_text, re.I):
            price = 'Preis auf Anfrage'

        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*([0-9.,]+)\s*m²', detail_text, re.I)
        area = area_match.group(1) if area_match else extract_area_text(detail_text)

        location_match = re.search(r'Standort\s*([A-ZÄÖÜa-zäöüß\-/ ]{2,80})', detail_text, re.I)
        location = clean_text(location_match.group(1)) if location_match else ''
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings[:12]


def fetch_imothek_listings_livewire_retry_alt():
    try:
        html = fetch_html(IMOTHEK_URL)
    except Exception:
        return []

    livewire_match = re.search(
        r'<livewire[^>]+data-component=(["\'])(.*?)\1[^>]*data-params=(["\'])(.*?)\3',
        html,
        re.I | re.S,
    )
    if not livewire_match:
        return []

    payload = {
        'components': [{
            'key': 'imothek-k1',
            'name': clean_text(livewire_match.group(2)),
            'params': unescape(clean_text(livewire_match.group(4))),
        }]
    }

    try:
        request = urllib.request.Request(
            'https://www.immobilie1.de/livewire/embed',
            data=json.dumps(payload).encode('utf-8'),
            headers={
                'User-Agent': 'Mozilla/5.0',
                'Content-Type': 'application/json',
            },
        )
        raw = urllib.request.urlopen(request, timeout=30).read().decode('utf-8', errors='ignore')
        component_html = ''.join(json.loads(raw).get('components', {}).values())
        snapshot_match = re.search(r'wire:snapshot=\"([^\"]+)\"', component_html)
        if not snapshot_match:
            return []
        snapshot = json.loads(unescape(snapshot_match.group(1)))
    except Exception:
        return []

    estates = snapshot.get('data', {}).get('estates', [])
    listings = []
    seen = set()

    for bucket in estates:
        if not isinstance(bucket, dict):
            continue
        for value in bucket.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or 'id' not in item:
                    continue

                link = clean_text(item.get('exposeUrl', ''))
                title = clean_text(item.get('headline', ''))
                if not is_valid_title(title):
                    title = normalize_title_from_link(link)
                if not is_valid_title(title):
                    continue

                location = clean_text(item.get('city', ''))
                if not is_clean_location_text(location):
                    location = extract_location_from_title(title)
                if not is_clean_location_text(location):
                    location = extract_location_from_link(link)
                if not is_clean_location_text(location):
                    location = UNKNOWN_LOCATION

                price = ''
                mainprice = item.get('mainprice')
                if isinstance(mainprice, list):
                    for p in mainprice:
                        if isinstance(p, dict) and p.get('value'):
                            price = str(p.get('value'))
                            break

                area = ''
                mainarea = item.get('mainarea')
                if isinstance(mainarea, list):
                    for a in mainarea:
                        if isinstance(a, dict) and a.get('value') not in (None, ''):
                            area = str(a.get('value'))
                            break

                add_listing(listings, seen, title, price, area, location, link)
                if len(listings) >= 12:
                    return listings[:12]

    return listings[:12]


def fetch_wurmseder_listings_retry_alt():
    try:
        html = fetch_html(WURMSEDER_URL)
    except Exception:
        return []

    detail_links = []
    for href_raw in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        href = urljoin(WURMSEDER_URL, clean_text(href_raw))
        if not href.startswith(('http://', 'https://')):
            continue
        if re.search(r'wp-json|/feed/?|instagram\.com|facebook\.com|linkedin\.com|mailto:|tel:', href, re.I):
            continue
        if not re.search(r'wurmseder-immobilien\.de/.+/?$', href, re.I):
            continue
        if re.search(r'/wp-content/|/wp-includes/|/wp-admin/', href, re.I):
            continue
        if re.search(r'\.(?:css|js|png|jpe?g|webp|svg|gif|ico|woff2?|ttf|eot|map|xml|json)(?:[?#]|$)', href, re.I):
            continue
        if re.search(r'[?&](?:ver|v)=', href, re.I):
            continue
        if re.search(r'/immobilien/?$', href, re.I):
            continue
        if re.search(r'immobilienmakler|referenzen|ueber-uns|ratgeber|wertermittlung|kontakt|impressum|datenschutz', href, re.I):
            continue
        if not re.search(r'/(?:[^/]*(?:haus|wohnung|villa|grundst|mehrfamilien|doppelhaus|penthouse|objekt|immobilie)[^/]*)/?$', href, re.I):
            continue
        if href not in detail_links:
            detail_links.append(href)

    listings = []
    seen = set()
    for href in detail_links[:24]:
        try:
            detail_html = fetch_html(href)
        except Exception:
            continue

        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        if not is_valid_title(title):
            title = normalize_title_from_link(href)
        if not is_valid_title(title):
            continue

        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        price = price_match.group(1) if price_match else ''
        if not price:
            price = 'Preis auf Anfrage'

        area = extract_area_text(detail_text)
        if not area and not re.search(r'wohnfl(?:ä|ae)che|grundst(?:ü|ue)ck|\bzimmer\b|m²', detail_text, re.I):
            continue

        location = extract_location_text(detail_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings[:12]


def fetch_riedl_listings_retry_alt():
    try:
        html = fetch_html('https://riedl-makler.de/referenzen/')
    except Exception:
        return []

    detail_links = []
    for href_raw in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        if '/portfolio-items/' not in href_raw:
            continue
        href = urljoin(RIEDL_MAKLER_URL, clean_text(href_raw))
        if href not in detail_links:
            detail_links.append(href)

    listings = []
    seen = set()
    for href in detail_links[:24]:
        try:
            detail_html = fetch_html(href)
        except Exception:
            continue

        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        title = re.sub(r'\s*[\-|]\s*Riedl.*$', '', title, flags=re.I).strip(' -|')
        if not is_valid_title(title):
            title = normalize_title_from_link(href)
        if not is_valid_title(title):
            continue

        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        price = price_match.group(1) if price_match else ''
        if not price and re.search(r'preis\s+auf\s+anfrage|auf\s+anfrage', detail_text, re.I):
            price = 'Preis auf Anfrage'
        if not price:
            price = 'Preis auf Anfrage'

        area = extract_area_text(detail_text)
        location = extract_location_text(detail_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings[:12]


def fetch_no_price_detail_retry(base_url: str, href_hint: str, detail_signal_hint: str):
    try:
        root_html = fetch_html(base_url)
    except Exception:
        return []

    page_urls = [base_url]
    for embedded_url in extract_embedded_urls(base_url, root_html):
        if embedded_url not in page_urls:
            page_urls.append(embedded_url)

    detail_links = []
    seen_links = set()
    for page_url in page_urls[:4]:
        try:
            page_html = root_html if page_url == base_url else fetch_html(page_url)
        except Exception:
            continue

        for href_raw in re.findall(r'href=["\']([^"\']+)["\']', page_html, re.I):
            href = urljoin(page_url, clean_text(href_raw))
            if not href.startswith(('http://', 'https://')):
                continue
            if re.search(r'instagram\.com|facebook\.com|linkedin\.com|mailto:|tel:|wp-json|/feed/?', href, re.I):
                continue
            if re.search(r'\.(?:css|js|png|jpe?g|webp|svg|gif|ico|woff2?|ttf|eot|map|xml|json)(?:[?#]|$)', href, re.I):
                continue
            if not re.search(href_hint, href, re.I):
                continue

            key = href.rstrip('/').lower()
            if key in seen_links:
                continue
            seen_links.add(key)
            detail_links.append(href)

    listings = []
    seen = set()
    for href in detail_links[:28]:
        try:
            detail_html = fetch_html(href)
        except Exception:
            continue

        detail_text = clean_text(detail_html)
        if not re.search(detail_signal_hint, href + ' ' + detail_text, re.I):
            continue

        title = extract_page_title(detail_html)
        if not is_valid_title(title):
            title = normalize_title_from_link(href)
        if not is_valid_title(title):
            continue

        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        if price_match:
            price = clean_text(price_match.group(1))
        elif re.search(r'preis\s+auf\s+anfrage|auf\s+anfrage', detail_text, re.I):
            price = 'Preis auf Anfrage'
        else:
            price = 'Preis auf Anfrage'

        area = extract_area_text(detail_text)
        location = extract_location_text(detail_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        # Keep no-price fallback strict enough to avoid nav/assets false positives.
        if not area and location == UNKNOWN_LOCATION and not re.search(r'wohnfl(?:ä|ae)che|grundst(?:ü|ue)ck|zimmer|m²|qm|lage|standort', detail_text, re.I):
            continue

        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings[:12]


def fetch_bechler_listings_retry_alt():
    bechler_url = 'https://www.bechler-immobilien.de/verkauf-vermietung/'
    try:
        html = fetch_html(bechler_url)
    except Exception:
        html = ''

    listings = []
    seen = set()
    if html:
        heading_pattern = re.compile(r'<h2[^>]*>(.*?)</h2>', re.I | re.S)
        headings = list(heading_pattern.finditer(html))
        for idx, match in enumerate(headings):
            title = clean_text(match.group(1))
            title_lower = title.lower()
            if (
                not is_valid_title(title)
                or title_lower in {'verkauf + vermietung .', 'verkauf.', 'vermietung.', 'zu verkaufen.', 'zu vermieten.'}
                or title_lower.startswith('verkauft:')
                or title_lower.startswith('vermietet:')
                or 'traumimmobilie wartet' in title_lower
            ):
                continue

            start = match.start()
            end = headings[idx + 1].start() if idx + 1 < len(headings) else len(html)
            block = html[start:end]
            block_text = clean_text(block)

            price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*€', block_text, re.I)
            if price_match:
                price = clean_text(price_match.group(1))
            elif re.search(r'auf\s+anfrage', block_text, re.I):
                price = 'Preis auf Anfrage'
            else:
                price = ''

            area_match = re.search(r'Wohnfl(?:ä|ae)che\s*([0-9.,]+)\s*(?:qm|m²)', block_text, re.I)
            area = clean_text(area_match.group(1)) if area_match else extract_area_text(block_text)

            location_match = re.search(r'\b\d{5}\s+[A-ZÄÖÜa-zäöüß][A-Za-zÄÖÜäöüß\-()/. ]{1,60}', block_text)
            location = clean_text(location_match.group(0)) if location_match else ''
            if not is_clean_location_text(location):
                location = extract_location_from_title(title)
            if not is_clean_location_text(location):
                location = UNKNOWN_LOCATION

            if not price and not area and location == UNKNOWN_LOCATION:
                continue

            link = f'{bechler_url}#objekt-{len(listings) + 1}'
            add_listing(listings, seen, title, price, area, location, link)
            if len(listings) >= 12:
                break

    if listings:
        return listings[:12]

    rows = fetch_source_specific_with_embedded_retry(
        bechler_url,
        r'(immobilie|objekt|expose|angebot|kauf|verkauf|haus|wohnung|haeuser|wohnungen)',
        r'(verkauf|vermietung|kauf|angebote|objekt|immobilie|detail|expose)',
    )
    if rows:
        return rows[:12]
    return fetch_zero_broker_detail_crawl(bechler_url)


def fetch_gattinger_listings_retry_alt():
    try:
        html = fetch_html(GATTINGER_URL)
    except Exception:
        html = ''

    page_urls = [GATTINGER_URL]
    if html:
        for src in re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
            iframe_url = urljoin(GATTINGER_URL, clean_text(src))
            if iframe_url not in page_urls:
                page_urls.append(iframe_url)
        for embedded_url in extract_embedded_urls(GATTINGER_URL, html):
            if embedded_url not in page_urls:
                page_urls.append(embedded_url)

    listings = []
    seen = set()
    for page_url in page_urls[:6]:
        try:
            page_html = html if page_url == GATTINGER_URL else fetch_html(page_url)
        except Exception:
            continue

        for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', page_html, re.I | re.S):
            href = urljoin(page_url, clean_text(match.group(1)))
            card_html = match.group(2)

            title_match = re.search(r'<h4[^>]*>(.*?)</h4>', card_html, re.I | re.S)
            title = clean_text(title_match.group(1)) if title_match else ''
            if not is_valid_title(title):
                continue

            p_values = [clean_text(v) for v in re.findall(r'<p[^>]*>(.*?)</p>', card_html, re.I | re.S)]
            p_values = [v for v in p_values if v]
            detail_text = ' '.join(p_values)

            price_match = re.search(r'(?:Preis|Kaltmiete)\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*€', detail_text, re.I)
            price = clean_text(price_match.group(1)) if price_match else ''

            area_match = re.search(r'(?:Wohnfläche|Gesamtfläche|Grundstücksfläche|Büro-/\s*Praxisfläche)\s*ca\.?\s*:?\s*([0-9.,]+)\s*(?:m²|qm)', detail_text, re.I)
            area = clean_text(area_match.group(1)) if area_match else extract_area_text(detail_text)

            location = p_values[0] if p_values else ''
            if not is_clean_location_text(location):
                location_match = re.search(r'\b\d{5}\s+[A-ZÄÖÜa-zäöüß][A-Za-zÄÖÜäöüß\-()/. ]{1,60}', detail_text)
                location = clean_text(location_match.group(0)) if location_match else ''
            if not is_clean_location_text(location):
                location = extract_location_from_title(title)
            if not is_clean_location_text(location):
                location = extract_location_from_link(href)
            if not is_clean_location_text(location):
                location = UNKNOWN_LOCATION

            add_listing(listings, seen, title, price, area, location, href)
            if len(listings) >= 12:
                return listings[:12]

        listings = merge_listing_rows(listings, parse_json_ld_listings(page_url, page_html))
        listings = merge_listing_rows(listings, parse_price_blocks_without_links(page_url, page_html))
        if len(listings) >= 12:
            return listings[:12]

    if listings:
        return listings[:12]

    rows = fetch_source_specific_with_embedded_retry(
        GATTINGER_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(angebote|objekt|immobilie|detail|expose|kauf)',
    )
    if rows:
        return rows[:12]
    return fetch_zero_broker_detail_crawl(GATTINGER_URL)


def fetch_reischl_listings_retry_alt():
    rows = fetch_source_specific_with_embedded_retry(
        REISCHL_URL,
        r'(objects|immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(objects|angebote|objekt|immobilie|detail|expose|kauf|reference_main)',
    )
    if rows:
        return rows[:12]

    rows = fetch_multi_page_retry(REISCHL_URL, r'(objects\.php|reference_main\.php|Ordner=|search\.php)')
    if rows:
        return rows[:12]

    return fetch_zero_broker_detail_crawl(REISCHL_URL)


def fetch_sriimmo_listings_retry_alt():
    rows = fetch_no_price_detail_retry(
        SRI_IMMO_URL,
        r'(immobilie|objekt|angebot|kauf|mieten|wohnen|haus|wohnung)',
        r'(immobilie|objekt|kaufpreis|preis|wohnfl(?:ä|ae)che|grundst(?:ü|ue)ck|lage|standort|zimmer|m²|qm)',
    )
    if rows:
        return rows[:12]

    return fetch_source_specific_with_embedded_retry(
        SRI_IMMO_URL,
        r'(immobilie|objekt|expose|angebot|kauf|mieten|haus|wohnung|haeuser|wohnungen)',
        r'(kaufen|mieten|immobilien|angebote|objekt|immobilie|detail|expose)',
    )


def fetch_dalexis_listings_retry_alt():
    rows = fetch_source_specific_with_embedded_retry(
        DALEXIS_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(immobilien-kauf-verkauf|immobilien|angebote|objekt|immobilie|detail|expose|kauf)',
    )
    if rows:
        return rows[:12]

    try:
        root_html = fetch_html(DALEXIS_URL)
    except Exception:
        return fetch_zero_broker_detail_crawl(DALEXIS_URL)

    page_urls = []
    for src in re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', root_html, re.I):
        iframe_url = urljoin(DALEXIS_URL, clean_text(src).replace('&amp;', '&'))
        if not re.search(r'aktuelle-objekte\.xhtml', iframe_url, re.I):
            continue
        if iframe_url not in page_urls:
            page_urls.append(iframe_url)
    if not page_urls:
        page_urls.append(DALEXIS_URL)

    discovered_pages = list(page_urls)
    for page_url in list(page_urls)[:3]:
        try:
            listing_html = root_html if page_url == DALEXIS_URL else fetch_html(page_url)
        except Exception:
            continue
        for href_raw in re.findall(r'href=["\']([^"\']*aktuelle-objekte\.xhtml[^"\']*)["\']', listing_html, re.I):
            href = urljoin(page_url, clean_text(href_raw).replace('&amp;', '&'))
            if href not in discovered_pages:
                discovered_pages.append(href)
            if len(discovered_pages) >= 8:
                break
        if len(discovered_pages) >= 8:
            break

    listings = []
    seen = set()
    seen_links = set()
    for page_url in discovered_pages[:8]:
        try:
            page_html = root_html if page_url == DALEXIS_URL else fetch_html(page_url)
        except Exception:
            continue

        segments = re.split(r'<div class=["\']obj-list-object[^>]*>', page_html, flags=re.I)
        for segment in segments[1:48]:
            href_match = re.search(r'href=["\']([^"\']*immobiliendetails\.xhtml\?id\[obj0\]=\d+[^"\']*)["\']', segment, re.I)
            if not href_match:
                continue

            href = urljoin(page_url, clean_text(href_match.group(1)).replace('&amp;', '&'))
            href_key = href.rstrip('/').lower()
            if href_key in seen_links:
                continue
            seen_links.add(href_key)

            card_html = segment[:2600]
            card_text = clean_text(card_html)

            title_match = re.search(r'<div class=["\']obj-title["\'][^>]*>\s*<a[^>]*>(.*?)</a>', card_html, re.I | re.S)
            title = clean_text(title_match.group(1)) if title_match else ''
            if not is_valid_title(title):
                title = normalize_title_from_link(href)

            price_match = re.search(r'(?:Kaufpreis|Mietpreis)\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf\s+Anfrage)', card_text, re.I)
            if not price_match:
                price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', card_text, re.I)
            price = clean_text(price_match.group(1)) if price_match else ''

            area_match = re.search(r'(?:Wohnfl(?:ä|ae)che|Wohnfl\.)\s*:?\s*([0-9][0-9.,]*)\s*m²', card_text, re.I)
            if not area_match:
                area_match = re.search(r'object-area-value[^>]*>\s*([0-9][0-9.,]*)\s*m²', card_html, re.I | re.S)
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

            location_match = re.search(r'<div class=["\']obj-ort["\'][^>]*>(.*?)</div>', card_html, re.I | re.S)
            location_raw = clean_text(location_match.group(1)) if location_match else ''
            location = extract_postcode_city_location(location_raw)
            if not is_clean_location_text(location):
                in_location_match = re.search(r'\b\d{4,5}\s+in\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/]+){0,2})', location_raw, re.I)
                if in_location_match:
                    location = clean_text(in_location_match.group(1))
            if not is_clean_location_text(location):
                location = clean_location_value(location_raw)
            if is_clean_location_text(location):
                location = re.sub(r'\bLink\b\s*$', '', location, flags=re.I).strip(' ,;|-')

            needs_detail = (not price) or (not area) or (not is_clean_location_text(location))
            if needs_detail:
                try:
                    detail_html = fetch_html(href)
                except Exception:
                    detail_html = ''
                if detail_html:
                    detail_text = clean_text(detail_html)
                    if not price:
                        detail_price = re.search(r'(?:Kaufpreis|Mietpreis)\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf\s+Anfrage)', detail_text, re.I)
                        if not detail_price:
                            detail_price = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
                        if detail_price:
                            price = clean_text(detail_price.group(1))
                    if not area:
                        area = extract_area_text(detail_text)
                    if not is_clean_location_text(location):
                        location = extract_postcode_city_location(detail_text)
                    if not is_clean_location_text(location):
                        location = extract_location_text(detail_text, '')

            if not is_clean_location_text(location):
                location = extract_location_from_title(title)
            if not is_clean_location_text(location):
                location = extract_location_from_link(href)
            if not is_clean_location_text(location):
                location = UNKNOWN_LOCATION

            add_listing(listings, seen, title, price, area, location, href)
            if len(listings) >= 12:
                return listings[:12]

    if listings:
        return listings[:12]

    merged = []
    for page_url in discovered_pages[:5]:
        try:
            page_html = root_html if page_url == DALEXIS_URL else fetch_html(page_url)
        except Exception:
            continue
        merged = merge_listing_rows(merged, parse_link_cards(page_url, page_html, r'(immobilie|objekt|expose|angebot|kauf|wohnung|haus|detail|obj-|xhtml)'))
        merged = merge_listing_rows(merged, parse_json_ld_listings(page_url, page_html))
        merged = merge_listing_rows(merged, parse_price_blocks_without_links(page_url, page_html))
    if merged:
        return merged[:12]

    return fetch_zero_broker_detail_crawl(DALEXIS_URL)


def fetch_vorstadtmakler_listings_retry_alt():
    try:
        html = fetch_html(VORSTADTMAKLER_URL)
    except Exception:
        html = ''

    if html:
        listings = []
        seen = set()
        blocked_paths = re.compile(r'/(?:kontakt|karriere|news-und-blog|leistungen|uber-uns|tippgeber|immobilienbewertung)(?:/|$)', re.I)

        for match in re.finditer(r'"first_link_href"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', html, re.I):
            href_raw = clean_text(match.group(1))
            try:
                href_decoded = json.loads(f'"{href_raw}"')
            except Exception:
                href_decoded = href_raw.replace('\\u002F', '/').replace('\\/', '/')

            href = urljoin(VORSTADTMAKLER_URL, clean_text(href_decoded))
            if not href.startswith(('http://', 'https://')):
                continue
            if urlparse(href).netloc and 'vorstadtmakler.de' not in urlparse(href).netloc.lower():
                continue
            if blocked_paths.search(urlparse(href).path or ''):
                continue

            chunk = html[max(0, match.start() - 5200):match.start() + 900]
            chunk = chunk.replace('\\u002F', '/').replace('\\/', '/')
            chunk = chunk.replace('\\u003C', '<').replace('\\u003E', '>').replace('\\u0026', '&')
            chunk_text = clean_text(chunk)

            price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*)(?:\s*-\s*[0-9]{1,3}(?:\.[0-9]{3})*)?\s*€', chunk_text, re.I)
            if not price_match:
                continue
            price = clean_text(price_match.group(1))

            text_candidates = re.findall(r'"text"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', chunk, re.I)
            title_candidates = []
            for candidate_raw in text_candidates:
                try:
                    candidate = json.loads(f'"{candidate_raw}"')
                except Exception:
                    candidate = candidate_raw
                candidate = clean_text(candidate)
                if not is_valid_title(candidate):
                    continue
                if re.search(r'kaufpreis|wohnfl(?:ä|ae)che|grundst(?:ü|ue)ck|hier geht\'s zum objekt|sekund', candidate, re.I):
                    continue
                title_candidates.append(candidate)

            title = max(title_candidates, key=len) if title_candidates else normalize_title_from_link(href)
            if not is_valid_title(title):
                continue

            area = ''
            area_match = re.search(r'([0-9]{1,4}(?:[\.,][0-9]{1,2})?)\s*m²', chunk_text, re.I)
            if area_match:
                area = area_match.group(1).replace('.', '').replace(',', '.')

            location = extract_postcode_city_location(chunk_text)
            location_match = re.search(r'\b\d{5}\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+(?:\s+(?:[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+|am|an|im|in|bei|der|den|vom|zum)){0,4})', chunk_text)
            if location_match:
                location_candidate = clean_text(location_match.group(1))
                location_candidate = re.sub(r'\b(?:Baujahr|Wohnfl(?:ä|ae)che|Zimmer|Kauf|Miete)\b.*$', '', location_candidate, flags=re.I).strip(' ,;|-')
                if is_clean_location_text(location_candidate) and (not is_clean_location_text(location) or len(location_candidate) > len(location)):
                    location = location_candidate
            if not is_clean_location_text(location):
                location = extract_location_from_title(title)
            if not is_clean_location_text(location):
                location = extract_location_from_link(href)
            if not is_clean_location_text(location):
                location = UNKNOWN_LOCATION

            add_listing(listings, seen, title, price, area, location, href)
            if len(listings) >= 12:
                return listings[:12]

        if listings:
            return listings[:12]

    base_urls = [
        VORSTADTMAKLER_URL,
        'https://vorstadtmakler.de/immobilien/kaufen/',
        'https://vorstadtmakler.de/immobilien/angebote/',
    ]
    rows = []
    for base_url in base_urls:
        rows = merge_listing_rows(rows, fetch_source_specific_with_embedded_retry(
            base_url,
            r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
            r'(immobilien|kaufen|angebote|angebot|objekt|immobilie|detail|expose|kauf)',
        ))
        if len(rows) >= 12:
            return rows[:12]
    if rows:
        return rows[:12]

    return fetch_zero_broker_detail_crawl(VORSTADTMAKLER_URL)


def fetch_cki_listings_retry_alt():
    try:
        _ = fetch_html(CKI_URL, timeout=20)
    except Exception as exc:
        if re.search(r'403|forbidden|cloudflare|sicherheits', str(exc), re.I):
            return []

    rows = fetch_source_specific_with_embedded_retry(
        CKI_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(immobilienangebote|kaufen|immobilien|angebote|objekt|immobilie|detail|expose)',
    )
    if rows:
        return rows[:12]
    return fetch_zero_broker_detail_crawl(CKI_URL)


def fetch_hoser_listings_retry_alt():
    try:
        html = fetch_html(HOSER_URL)
    except Exception:
        return []
    rows = parse_property_link_cards(
        HOSER_URL,
        html,
        r'/(?:immobilien|objekte|angebote)/[^/?#]+(?:/|\.html?)?$',
    )[:12]
    for row in rows:
        row['title'] = re.sub(r'\s+(?:Zum Objekt|Objekt ansehen|Details?)$', '', row.get('title', ''), flags=re.I)
    return rows


def fetch_maurer_listings():
    try:
        html = fetch_html(MAURER_URL)
    except Exception:
        return []

    detail_links = []
    seen_links = set()
    for href_raw in re.findall(r'href=["\']([^"\']+)', html, re.I):
        href = urljoin(MAURER_URL, clean_text(href_raw))
        path = normalize_path(href)
        if href in seen_links or is_non_listing_url(href):
            continue
        if not re.search(r'(?:/angebote/|/immobilien/|cmd=(?:expose|searchDetails))', href, re.I):
            continue
        if path in {'/angebote', '/immobilien'} or re.search(r'(?:impressum|kontakt|datenschutz|ueber-uns)', href, re.I):
            continue
        seen_links.add(href)
        detail_links.append(href)

    listings = []
    seen = set()
    for href in detail_links[:18]:
        try:
            detail_html = fetch_html(href)
        except Exception:
            continue
        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        if not is_valid_title(title) or is_generic_navigation_title(title):
            continue
        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        if not price_match:
            continue
        area = extract_area_text(detail_text)
        explicit_location = re.search(r'(?:Ort|Lage|Standort)\s*:\s*([^<\n]+)', detail_text, re.I)
        location = clean_text(explicit_location.group(1)) if explicit_location else extract_location_text(detail_text, '')
        location = re.sub(r'(?i)M(?:u|ue|ü)nchen[- ]Frstenried', 'München-Frstenried', location)
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION
        add_listing(listings, seen, title, price_match.group(1), area, location, href)
    return listings[:12]


def fetch_sozius_listings_retry_alt():
    try:
        html = fetch_html(SOZIUS_URL)
    except Exception:
        return []
    return parse_property_link_cards(SOZIUS_URL, html, r'/detailseite/')


def fetch_hallinger_listings_retry_alt():
    try:
        html = fetch_html(HALLINGER_URL)
    except Exception:
        try:
            request = urllib.request.Request(HALLINGER_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(request, timeout=20, context=ssl._create_unverified_context()) as response:
                html = response.read().decode('utf-8', 'ignore')
        except Exception:
            return []
    listings = []
    seen = set()
    for match in re.finditer(r'<h2\b[^>]*>(.*?)</h2>(.*?)(?=<h2\b|</body>|$)', html, re.I | re.S):
        title = clean_text(match.group(1))
        block = match.group(2)
        link_match = re.search(r'href=["\']([^"\']*immobilien-details\.php\?[^"\']+)["\']', block, re.I)
        price_match = re.search(r'Kaufpreis\s*:\s*([0-9.]+(?:,[0-9]{2})?)\s*(?:€|&euro;)', block, re.I)
        if not link_match or not price_match or len(title) < 8:
            continue
        area_match = re.search(r'(?:Wohn|Nutz|Grundstücks?)fläche\s*:?\s*([0-9.,]+)\s*(?:m²|qm)', block, re.I)
        location = extract_location_from_title(title)
        add_listing(
            listings,
            seen,
            title,
            price_match.group(1),
            area_match.group(1) if area_match else '',
            location if is_clean_location_text(location) else UNKNOWN_LOCATION,
            urljoin(HALLINGER_URL, link_match.group(1)),
        )
        if len(listings) >= 12:
            break
    return listings


def fetch_wesoly_listings_retry_alt():
    rows = fetch_source_specific_with_embedded_retry(
        WESOLY_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(immobilienangebote-zum-kauf|angebote|kauf|objekt|immobilie|detail|expose)',
    )
    if rows:
        return rows[:12]

    rows = fetch_no_price_detail_retry(
        WESOLY_URL,
        r'(immobilie|objekt|angebot|kauf|wohnen|haus|wohnung|expose|detail)',
        r'(immobilie|objekt|kaufpreis|preis|wohnfl(?:ä|ae)che|lage|standort|zimmer|m²|qm)',
    )
    if rows:
        return rows[:12]
    return fetch_zero_broker_detail_crawl(WESOLY_URL)


def fetch_lebenstraum_listings_retry_alt():
    candidate_urls = [
        LEBENSTRAUM_URL,
        'https://lebenstraum-immobilien.com/immobilien/',
    ]

    rows = []
    for url in candidate_urls:
        rows = merge_listing_rows(rows, fetch_source_specific_with_embedded_retry(
            url,
            r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
            r'(suchende|immobilien|muenchen|kaufen|angebote|objekt|immobilie|detail|expose)',
        ))
        if len(rows) >= 12:
            return rows[:12]

    rows = merge_listing_rows(rows, fetch_no_price_detail_retry(
        LEBENSTRAUM_URL,
        r'(immobilie|objekt|angebot|kauf|wohnen|haus|wohnung|expose|detail)',
        r'(immobilie|objekt|kaufpreis|preis|wohnfl(?:ä|ae)che|lage|standort|zimmer|m²|qm)',
    ))
    if rows:
        return rows[:12]
    return fetch_zero_broker_detail_crawl(LEBENSTRAUM_URL)


def fetch_zero_broker_detail_crawl(base_url: str):
    try:
        html = fetch_html(base_url)
    except Exception:
        return []

    href_hint = r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen|cmd=searchDetails|cmd=expose|xhtml)'
    detail_hint = r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen|cmd=searchDetails|cmd=expose|xhtml)'

    rows = []
    rows = merge_listing_rows(rows, parse_searchdetails_cards(base_url, html))
    rows = merge_listing_rows(rows, parse_link_cards(base_url, html, href_hint))
    rows = merge_listing_rows(rows, parse_json_ld_listings(base_url, html))
    rows = merge_listing_rows(rows, parse_price_blocks_without_links(base_url, html))
    rows = merge_listing_rows(rows, fetch_source_specific_broker_listings(base_url, href_hint, detail_hint))

    extras = []
    for embedded_url in extract_embedded_urls(base_url, html):
        try:
            embedded_html = fetch_html(embedded_url)
        except Exception:
            continue
        extras = merge_listing_rows(extras, parse_searchdetails_cards(embedded_url, embedded_html))
        extras = merge_listing_rows(extras, parse_link_cards(embedded_url, embedded_html, href_hint))
        extras = merge_listing_rows(extras, parse_json_ld_listings(embedded_url, embedded_html))
        extras = merge_listing_rows(extras, parse_price_blocks_without_links(embedded_url, embedded_html))

    return merge_listing_rows(rows, extras)[:12]


def fetch_multi_page_retry(base_url: str, page_hint: str):
    try:
        root_html = fetch_html(base_url)
    except Exception:
        return []

    page_re = re.compile(page_hint, re.I)
    href_hint = r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen|detail|xhtml)'
    detail_hint = r'(immobilien|angebote|angebot|objekt|immobilie|detail|expose|xhtml|kaufangebote)'

    page_urls = [base_url]
    for href_raw in re.findall(r'href=["\']([^"\']+)["\']', root_html, re.I):
        href = urljoin(base_url, clean_text(href_raw))
        if not href.startswith(('http://', 'https://')):
            continue
        if not page_re.search(href):
            continue
        if re.search(r'wp-json|/feed/?|/tag/|/author/|instagram\.com|facebook\.com|linkedin\.com', href, re.I):
            continue
        if href not in page_urls:
            page_urls.append(href)
        if len(page_urls) >= 7:
            break

    rows = []
    for page_url in page_urls:
        try:
            html = root_html if page_url == base_url else fetch_html(page_url)
        except Exception:
            continue
        rows = merge_listing_rows(rows, parse_searchdetails_cards(page_url, html))
        rows = merge_listing_rows(rows, parse_link_cards(page_url, html, href_hint))
        rows = merge_listing_rows(rows, parse_json_ld_listings(page_url, html))
        rows = merge_listing_rows(rows, parse_price_blocks_without_links(page_url, html))
        rows = merge_listing_rows(rows, fetch_source_specific_broker_listings(page_url, href_hint, detail_hint))
        if len(rows) >= 12:
            break

    return rows[:12]


def fetch_detail_page_listings(base_url: str, link_hint: str):
    try:
        root_html = fetch_html(base_url)
    except Exception:
        return []

    links = []
    seen_links = set()
    for href_raw in re.findall(r'href=["\']([^"\']+)["\']', root_html, re.I):
        href = urljoin(base_url, clean_text(href_raw))
        if not re.search(link_hint, href, re.I) or href in seen_links:
            continue
        if re.search(r'/archiv(?:/|$)', href, re.I):
            continue
        seen_links.add(href)
        links.append(href)

    listings = []
    seen = set()
    for href in links[:18]:
        try:
            detail_html = fetch_html(href)
        except Exception:
            continue
        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        title = re.sub(r'\s*\|\s*(?:MUELLER\s*&\s*ENGLISCH|Windhausen Partner).*$', '', title, flags=re.I).strip(' -|')
        if not is_valid_title(title):
            title = normalize_title_from_link(href)
        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        if not price_match:
            continue
        location_match = re.search(r'>\s*Ort\s*</[^>]+>\s*(?:<[^>]+>\s*)+([^<]+)', detail_html, re.I | re.S)
        location = clean_text(location_match.group(1)) if location_match else ''
        if not location:
            location = extract_location_from_json_ld(detail_html)
        if not location:
            location = extract_location_text(detail_text, '')
        area = extract_area_text(detail_text)
        add_listing(listings, seen, title, price_match.group(1), area, location, href)
        if len(listings) >= 12:
            break
    return listings


def iter_json_ld_nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from iter_json_ld_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_json_ld_nodes(item)


def parse_json_ld_listings(base_url: str, html: str):
    listings = []
    seen = set()
    scripts = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S)
    for payload in scripts:
        text = clean_text(payload)
        if not text:
            continue
        try:
            data = json.loads(text)
        except Exception:
            continue

        for node in iter_json_ld_nodes(data):
            if not isinstance(node, dict):
                continue

            node_type = str(node.get('@type', ''))
            if isinstance(node.get('@type'), list):
                node_type = ' '.join(str(x) for x in node.get('@type', []))

            if not re.search(r'Product|Residence|House|Apartment|SingleFamily|Offer', node_type, re.I):
                continue

            title = clean_text(node.get('name') or node.get('headline') or '')
            link = clean_text(node.get('url') or node.get('@id') or '')
            offers = node.get('offers', {})
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if not isinstance(offers, dict):
                offers = {}

            price = clean_text(
                offers.get('price')
                or (offers.get('priceSpecification') or {}).get('price')
                or node.get('price')
                or ''
            )
            if price and re.fullmatch(r'\d+(?:\.\d+)?', price):
                price = price + ' €'

            area = ''
            floor_size = node.get('floorSize', {})
            if isinstance(floor_size, dict):
                area = clean_text(floor_size.get('value') or floor_size.get('name') or '')

            location = ''
            address = node.get('address', {})
            if isinstance(address, dict):
                location = clean_text(address.get('addressLocality') or address.get('streetAddress') or '')

            if not link:
                link = base_url
            link = urljoin(base_url, link)
            add_listing(listings, seen, title, price, area, location, link)

    return listings[:12]


def parse_property_link_cards(base_url: str, html: str, link_pattern: str):
    listings = []
    seen = set()
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = urljoin(base_url, clean_text(match.group(1)))
        if not re.search(link_pattern, href, re.I):
            continue
        chunk = html[max(0, match.start() - 1800):min(len(html), match.end() + 1800)]
        chunk_text = clean_text(chunk)
        title = extract_title(chunk)
        if not is_valid_title(title) or is_generic_navigation_title(title):
            title = clean_text(re.sub(r'<[^>]+>', ' ', match.group(2)))
        if not is_valid_title(title) or is_generic_navigation_title(title):
            title = normalize_title_from_link(href)
        if not is_valid_title(title):
            continue
        price_match = re.search(
            r'(?:Kaufpreis|Miete(?:\s+pro\s+Monat)?|Preis)?\s*:?' 
            r'\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|[0-9]{3,}(?:,[0-9]{2})?)\s*€',
            chunk_text,
            re.I,
        )
        if not price_match:
            continue
        area_match = re.search(r'(?:Wohnfläche|Wfl(?:\s+Fläche)?|Fläche|Grundstück)\s*(?:ca\.?|:)?\s*([0-9.,]+)\s*(?:m²|qm)', chunk_text, re.I)
        area = area_match.group(1) if area_match else extract_area_text(chunk_text)
        location = extract_location_text(chunk_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION
        add_listing(listings, seen, title, price_match.group(1), area, location, href)
        if len(listings) >= 12:
            break
    return listings[:12]


def fetch_property_link_cards_retry(base_url: str, link_pattern: str):
    try:
        html = fetch_html(base_url)
    except Exception:
        return []
    return parse_property_link_cards(base_url, html, link_pattern)


def parse_price_blocks_without_links(base_url: str, html: str):
    listings = []
    seen = set()
    for index, match in enumerate(re.finditer(r'(?:Kaufpreis|Preis)\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', html, re.I)):
        start = max(0, match.start() - 2500)
        end = min(len(html), match.start() + 1200)
        chunk = html[start:end]
        chunk_text = clean_text(chunk)

        heading_matches = re.findall(r'<h[1-4][^>]*>(.*?)</h[1-4]>', chunk, re.I | re.S)
        title = clean_text(heading_matches[-1]) if heading_matches else ''

        href_match = re.search(r'<a[^>]+href=["\']([^"\']+)["\']', chunk, re.I | re.S)
        link = urljoin(base_url, clean_text(href_match.group(1))) if href_match else f'{base_url}#price-block-{index + 1}'

        if not is_valid_title(title):
            anchor_titles = [clean_text(x) for x in re.findall(r'<a[^>]*>(.*?)</a>', chunk, re.I | re.S)]
            valid_titles = [x for x in anchor_titles if is_valid_title(x)]
            if valid_titles:
                title = max(valid_titles, key=len)
        if not is_valid_title(title):
            slug_title = normalize_title_from_link(link)
            if is_valid_title(slug_title):
                title = slug_title
        if not is_valid_title(title):
            title = f'Objekt {index + 1}'

        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*(?:ca\.?\s*)?([0-9.,]+)', chunk_text, re.I)
        if not area_match:
            area_match = re.search(r'([0-9.,]+)\s*m²', chunk_text, re.I)
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

        location = extract_location_text(chunk_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        price = clean_text(match.group(1))
        add_listing(listings, seen, title, price, area, location, link)
        if len(listings) >= 12:
            break

    return listings


def extract_json_array_after_key(text: str, key: str):
    marker = f'{key} ='
    idx = text.find(marker)
    if idx == -1:
        return []

    start = text.find('[', idx)
    if start == -1:
        return []

    depth = 0
    in_string = False
    escape = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                snippet = text[start:pos + 1]
                try:
                    return json.loads(snippet)
                except Exception:
                    return []
    return []


def parse_searchdetails_cards(base_url: str, html: str):
    listings = []
    seen = set()
    seen_links = set()
    suspicious_title_pattern = re.compile(r'(?i)(?:cmd=|objq\[|icmd=|kaufartids=|href=|<div|">|&[a-z]+;)')

    for match in re.finditer(r'<a[^>]+href=["\']([^"\']*cmd=searchDetails[^"\']*)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = urljoin(base_url, clean_text(match.group(1)).replace('&amp;', '&'))
        href_key = href.rstrip('/').lower()
        if href_key in seen_links:
            continue
        seen_links.add(href_key)

        chunk = html[max(0, match.start() - 400):match.start() + 2400]
        chunk_text = clean_text(chunk)

        title = clean_text(match.group(2))
        if suspicious_title_pattern.search(title) or is_generic_navigation_title(title):
            title = ''
        if not is_valid_title(title):
            heading = re.search(r'<h[23][^>]*class=["\'][^"\']*property-title[^"\']*["\'][^>]*>\s*<a[^>]*>(.*?)</a>|<h2[^>]*>(.*?)</h2>|<h3[^>]*>(.*?)</h3>', chunk, re.I | re.S)
            if heading:
                title = clean_text(heading.group(1) or heading.group(2) or heading.group(3) or '')
        if suspicious_title_pattern.search(title) or is_generic_navigation_title(title):
            title = ''

        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*,-?\s*€', chunk_text, re.I)
        if not price_match:
            price_match = re.search(r'Kaufpreis\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', chunk_text, re.I)

        area_match = re.search(r'([0-9]{2,4}(?:,[0-9]{1,2})?)\s*m\s*WOHNFL', chunk_text, re.I)
        if not area_match:
            area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*(?:ca\.?\s*)?([0-9.,]+)', chunk_text, re.I)
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

        location = extract_postcode_city_location(chunk_text)
        if not is_clean_location_text(location):
            location = extract_location_text(chunk_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = ''

        needs_detail = (not is_valid_title(title)) or (not price_match) or (not area) or (not is_clean_location_text(location))
        if needs_detail:
            try:
                detail_html = fetch_html(href)
            except Exception:
                detail_html = ''

            if detail_html:
                detail_text = clean_text(detail_html)
                detail_title = extract_page_title(detail_html)
                detail_title = re.sub(r'\s+(?:HEGERICH|GERSCHLAUER).*$', '', detail_title, flags=re.I).strip(' -|')
                if is_valid_title(detail_title) and not suspicious_title_pattern.search(detail_title):
                    title = detail_title

                if not price_match:
                    price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*,-?\s*€', detail_text, re.I)
                if not price_match:
                    price_match = re.search(r'Kaufpreis\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', detail_text, re.I)

                if not area:
                    area = extract_area_text(detail_text)

                if not is_clean_location_text(location):
                    location = extract_postcode_city_location(detail_text)
                if not is_clean_location_text(location):
                    location = extract_location_text(detail_text, '')
                if not is_clean_location_text(location):
                    location = extract_location_from_title(title)
                if not is_clean_location_text(location):
                    location = UNKNOWN_LOCATION

        if not is_valid_title(title):
            continue
        if suspicious_title_pattern.search(title):
            continue
        if not price_match:
            continue

        add_listing(listings, seen, title, clean_text(price_match.group(1)), area, location, href)
        if len(listings) >= 12:
            break

    return listings


def fetch_akurat_listings_retry_alt():
    try:
        html = fetch_html(AKURAT_URL)
    except Exception:
        return []

    listings = []
    seen = set()
    units = extract_json_array_after_key(html, 'const wlacUnits')
    if units:

        for unit in units:
            if not isinstance(unit, dict):
                continue
            title = clean_text(unit.get('title') or '')
            price_value = unit.get('purchase_price')
            price = str(price_value) + ' €' if price_value not in (None, '', 0, '0') else ''
            area = clean_text(unit.get('living_space') or unit.get('livingArea') or '')
            location = clean_text(unit.get('city') or unit.get('postal_city') or '')
            unit_id = clean_text(str(unit.get('unit_id') or unit.get('id') or ''))
            link = clean_text(unit.get('url') or unit.get('link') or '')
            if not link:
                link = f'{AKURAT_URL}#unit-{unit_id or len(listings) + 1}'
            link = urljoin(AKURAT_URL, link)
            add_listing(listings, seen, title, price, area, location, link)
            if len(listings) >= 12:
                break

    if listings:
        return listings[:12]
    return merge_listing_rows(parse_json_ld_listings(AKURAT_URL, html), parse_price_blocks_without_links(AKURAT_URL, html))[:12]


def fetch_fischer_listings_retry_alt():
    try:
        html = fetch_html(FISCHER_URL)
    except Exception:
        return []

    listings = []
    seen = set()
    scripts = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S)
    detail_urls = []
    for payload in scripts:
        try:
            data = json.loads(payload.strip())
        except Exception:
            continue
        for node in iter_json_ld_nodes(data):
            if not isinstance(node, dict):
                continue
            if node.get('@type') == 'ListItem':
                url = clean_text(node.get('url') or '')
                if not url:
                    item = node.get('item', {})
                    if isinstance(item, dict):
                        url = clean_text(item.get('url') or '')
                    else:
                        url = clean_text(str(item))
                if url:
                    detail_urls.append(urljoin(FISCHER_URL, url))

    seen_urls = set()
    for detail_url in detail_urls:
        key = detail_url.rstrip('/').lower()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        try:
            detail_html = fetch_html(detail_url)
        except Exception:
            continue

        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        if not is_valid_title(title):
            title = normalize_title_from_link(detail_url)
        price_match = re.search(r'(?:Kaufpreis|Preis)\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', detail_text, re.I)
        if not price_match:
            price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:EUR|€)', detail_text, re.I)
        area = extract_area_text(detail_text)
        location = extract_location_text(detail_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION
        price = price_match.group(1) if price_match else ''
        add_listing(listings, seen, title, price, area, location, detail_url)
        if len(listings) >= 12:
            break

    if listings:
        return listings
    return merge_listing_rows(parse_json_ld_listings(FISCHER_URL, html), parse_price_blocks_without_links(FISCHER_URL, html))[:12]


def fetch_hegerich_listings_retry_alt():
    try:
        html = fetch_html(HEGERICH_URL)
    except Exception:
        return []
    return merge_listing_rows(parse_searchdetails_cards(HEGERICH_URL, html), parse_price_blocks_without_links(HEGERICH_URL, html))[:12]


def fetch_gerschlauer_listings_retry_alt():
    try:
        html = fetch_html(GERSCHLAUER_URL)
    except Exception:
        return []
    return merge_listing_rows(parse_searchdetails_cards(GERSCHLAUER_URL, html), parse_price_blocks_without_links(GERSCHLAUER_URL, html))[:12]


def fetch_ft_listings_retry_alt():
    try:
        html = fetch_html(FT_URL)
    except Exception:
        return []
    return merge_listing_rows(parse_price_blocks_without_links(FT_URL, html), parse_json_ld_listings(FT_URL, html))[:12]


def fetch_dahler_listings_retry_alt():
    try:
        html = fetch_html(DAHLER_URL)
    except Exception:
        return []

    listings = []
    seen = set()

    solr_url = ''
    solr_match = re.search(r'api\s*:\s*\{\s*url\s*:\s*"([^"]+)"', html, re.I)
    if solr_match:
        solr_url = clean_text(solr_match.group(1))

    if not solr_url:
        try:
            vae_html = fetch_html('https://www.dahlercompany.com/de/immobiliensuche/vae', timeout=25)
        except Exception:
            vae_html = ''
        if vae_html:
            m = re.search(r'https://[^"\']*solr[^"\']*/select', vae_html, re.I)
            if m:
                solr_url = clean_text(m.group(0))

    if not solr_url:
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+maklaro-property-search[^"\']+)["\']', html, re.I):
            try:
                js = fetch_html(urljoin(DAHLER_URL, src), timeout=20)
            except Exception:
                continue
            m = re.search(r'https://[^"\']*solr[^"\']*/select', js, re.I)
            if m:
                solr_url = clean_text(m.group(0))
                break

    if not solr_url:
        solr_url = 'https://solr.dahlercompany.com:443/solr/live_dahler_realestates/select'

    if solr_url:
        query_url = solr_url + '?q=*:*&rows=120&wt=json'
        try:
            payload = fetch_html(query_url, timeout=25)
            data = json.loads(payload)
        except Exception:
            data = {}

        docs = []
        if isinstance(data, dict):
            docs = ((data.get('response') or {}).get('docs') or [])

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            title = clean_text(doc.get('ss_title') or doc.get('title') or doc.get('name') or doc.get('estate_title') or '')
            if not is_valid_title(title):
                continue

            price = clean_text(str(doc.get('fts_price') or doc.get('price') or doc.get('purchase_price') or doc.get('kaufpreis') or ''))
            if price and re.fullmatch(r'\d+(?:\.\d+)?', price):
                price = price + ' €'
            area = clean_text(str(doc.get('fts_living_area') or doc.get('living_area') or doc.get('livingSpace') or doc.get('area') or ''))
            location = clean_text(doc.get('ss_locality') or doc.get('city') or doc.get('location') or doc.get('district') or '')
            link = clean_text(doc.get('ss_url') or doc.get('url') or doc.get('link') or '')
            if not link:
                slug = clean_text(str(doc.get('slug') or doc.get('id') or ''))
                link = f'https://www.dahlercompany.com/de/{slug}' if slug else DAHLER_URL
            link = urljoin(DAHLER_URL, link)
            add_listing(listings, seen, title, price, area, location, link)
            if len(listings) >= 12:
                break

    if listings:
        return listings
    return merge_listing_rows(parse_json_ld_listings(DAHLER_URL, html), parse_price_blocks_without_links(DAHLER_URL, html))[:12]


def fetch_hirschmann_listings_retry_alt():
    try:
        html = fetch_html(HIRSCHMANN_URL)
    except Exception:
        return []

    listings = parse_price_blocks_without_links(HIRSCHMANN_URL, html)
    if listings:
        return listings[:12]

    seen = set()
    detail_urls = []
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
        href = urljoin(HIRSCHMANN_URL, clean_text(match.group(1)))
        path = normalize_path(href)
        if not path or path in {'', '/kaufen'}:
            continue
        if '/immobilienmakler-' in path or '/referenz' in path or '/verkaufen' in path:
            continue
        if not re.search(r'(haus|wohnung|objekt|wohnen|kaufen|flair|lage)', path, re.I):
            continue
        key = href.rstrip('/').lower()
        if key in seen:
            continue
        seen.add(key)
        detail_urls.append(href)

    parsed = []
    parsed_seen = set()
    for detail_url in detail_urls[:30]:
        try:
            detail_html = fetch_html(detail_url)
        except Exception:
            continue
        detail_text = clean_text(detail_html)
        title = extract_page_title(detail_html)
        title = re.sub(r'\s+Hirschmann.*$', '', title, flags=re.I).strip()
        if not is_valid_title(title):
            title = normalize_title_from_link(detail_url)
        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', detail_text, re.I)
        if not price_match and re.search(r'preis\s+auf\s+anfrage|auf\s+anfrage', detail_text, re.I):
            price = 'Preis auf Anfrage'
        else:
            price = price_match.group(1) if price_match else ''
        area_match = re.search(r'([0-9]{2,4}(?:,[0-9]{1,2})?)\s*(?:m²|qm)', detail_text, re.I)
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = extract_location_text(detail_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION
        add_listing(parsed, parsed_seen, title, price, area, location, detail_url)
        if len(parsed) >= 12:
            break

    if parsed:
        return parsed

    extras = []
    for embedded_url in extract_embedded_urls(HIRSCHMANN_URL, html):
        try:
            embedded_html = fetch_html(embedded_url)
        except Exception:
            continue
        extras.extend(parse_link_cards(embedded_url, embedded_html, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'))
    return merge_listing_rows(parse_json_ld_listings(HIRSCHMANN_URL, html), extras)[:12]


def fetch_mrlodge_listings_retry_alt():
    try:
        html = fetch_html(MRLODGE_URL)
    except Exception:
        return []

    listings = []
    seen = set()
    link_pattern = re.compile(r'<a[^>]+href=["\']([^"\']*/immobilienverkauf/expose/\d+)["\'][^>]*>\s*(.*?)\s*</a>', re.I | re.S)

    for match in link_pattern.finditer(html):
        href = urljoin(MRLODGE_URL, clean_text(match.group(1)))
        start = max(0, match.start() - 2000)
        end = min(len(html), match.start() + 5000)
        chunk = html[start:end]
        chunk_text = clean_text(chunk)

        title = clean_text(match.group(2))
        if not is_valid_title(title):
            for payload in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', chunk, re.I | re.S):
                try:
                    data = json.loads(payload.strip())
                except Exception:
                    continue
                candidate = ''
                for node in iter_json_ld_nodes(data):
                    if not isinstance(node, dict):
                        continue
                    node_name = clean_text(node.get('name') or node.get('headline') or '')
                    if not is_valid_title(node_name):
                        continue
                    node_url = clean_text(node.get('url') or node.get('@id') or '')
                    if node_url and normalize_path(urljoin(MRLODGE_URL, node_url)) == normalize_path(href):
                        candidate = node_name
                        break
                    if not candidate:
                        candidate = node_name
                if is_valid_title(candidate):
                    title = candidate
                    break

        if not is_valid_title(title):
            continue

        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)', chunk_text, re.I)
        area_match = re.search(r'([0-9]{2,4}(?:,[0-9]{1,2})?)\s*m²', chunk_text, re.I)

        location = ''
        split_parts = [clean_text(x) for x in title.split('|')]
        if len(split_parts) >= 2 and is_clean_location_text(split_parts[1]):
            location = split_parts[1]
        if not is_clean_location_text(location):
            location = extract_location_text(chunk_text, '')
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        price = price_match.group(1) if price_match else ''
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings[:12]


def fetch_krimbacher_listings_retry_alt():
    try:
        html = fetch_html(KRIMBACHER_URL)
    except Exception:
        return []
    parsed = merge_listing_rows(
        parse_price_blocks_without_links(KRIMBACHER_URL, html),
        parse_json_ld_listings(KRIMBACHER_URL, html),
    )
    if parsed:
        return parsed[:12]
    return parse_link_cards(KRIMBACHER_URL, html, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')[:12]


def fetch_citigrund_listings_retry_alt():
    try:
        html = fetch_html(CITIGRUND_URL)
    except Exception:
        return []

    listings = parse_json_ld_listings(CITIGRUND_URL, html)
    if listings:
        return listings[:12]

    extras = []
    for embedded_url in extract_embedded_urls(CITIGRUND_URL, html):
        try:
            embedded_html = fetch_html(embedded_url)
        except Exception:
            continue
        extras.extend(parse_price_blocks_without_links(embedded_url, embedded_html))
        extras.extend(parse_link_cards(embedded_url, embedded_html, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'))
    return merge_listing_rows(parse_price_blocks_without_links(CITIGRUND_URL, html), extras)[:12]


def fetch_weiherer_listings():
    rows = fetch_generic_broker_listings(WEIHERER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')
    if not rows:
        return []

    listings = []
    seen = set()
    weak_titles = {'ort', 'lage', 'stadt', 'standort'}

    for row in rows:
        title = clean_text(row.get('title', ''))
        price = clean_text(row.get('price', ''))
        area = clean_text(row.get('area_sqm', ''))
        location = clean_location_value(row.get('location', ''))
        link = clean_text(row.get('link', ''))

        if '"' in location or len(location) <= 2:
            location = ''

        detail_html = ''
        # Weiherer cards often expose generic labels as anchor text (for example "Ort").
        if title.lower() in weak_titles or not is_valid_title(title):
            try:
                detail_html = fetch_html(link)
            except Exception:
                detail_html = ''

            if detail_html:
                detail_title = extract_page_title(detail_html)
                detail_title = re.sub(r'\s+Weiherer\s+Immobilien\s*$', '', detail_title, flags=re.I).strip(' -|')
                if is_valid_title(detail_title) and detail_title.lower() not in weak_titles:
                    title = detail_title

        if not is_clean_location_text(location):
            from_title = extract_location_from_title(title)
            if is_clean_location_text(from_title):
                location = from_title
        if not is_clean_location_text(location):
            from_link = extract_location_from_link(link)
            if is_clean_location_text(from_link):
                location = from_link
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, link)
        if len(listings) >= 12:
            break

    return listings


def fetch_mb_listings():
    rows = fetch_external_broker_listings(MB_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')
    if not rows:
        return []

    for row in rows:
        location = clean_location_value(row.get('location', ''))
        if is_clean_location_text(location):
            row['location'] = normalize_common_city_spelling(location)
            continue

        repaired = recover_location_from_detail_page(
            clean_text(row.get('link', '')),
            clean_text(row.get('title', '')),
            location,
        )
        row['location'] = repaired if is_clean_location_text(repaired) else UNKNOWN_LOCATION

    return rows


def fetch_fischer_listings():
    return fetch_source_specific_broker_listings(FISCHER_URL, r'(immobilie|objekt|expose|angebot|kauf)', r'(immobilie|objekt|expose|angebot|kauf)')


def fetch_heimhuber_listings():
    return fetch_generic_broker_listings(HEIMHUBER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_citigrund_listings():
    return fetch_source_specific_broker_listings(CITIGRUND_URL, r'(immobilie|objekt|expose|angebot|kauf)', r'(immobilie|objekt|expose|angebot|kauf|details)')


def fetch_georgi_listings():
    return fetch_generic_broker_listings(GEORGI_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_akurat_listings():
    return fetch_external_broker_listings(AKURAT_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_hegerich_listings():
    return fetch_source_specific_broker_listings(HEGERICH_URL, r'(cmd=searchDetails|cmd=expose|immobilie|objekt|angebot)', r'(cmd=searchDetails|cmd=expose|obj-)')


def fetch_eder_listings():
    return fetch_generic_broker_listings(EDER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_gerschlauer_listings():
    return fetch_source_specific_broker_listings(GERSCHLAUER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnungen?)', r'(Haeuser-zum-Kauf|cmd=expose|objekt|immobilie)')


def fetch_dahler_listings():
    return fetch_source_specific_broker_listings(DAHLER_URL, r'(immobilie|objekt|expose|angebot|kauf)', r'(immobilie|objekt|expose|angebot|kauf|immobiliensuche)')


def fetch_krimbacher_listings():
    return fetch_external_broker_listings(KRIMBACHER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_klatt_listings():
    return fetch_klatt_listings_source_specific()


def fetch_klatt_listings_source_specific():
    try:
        html = fetch_html(KLATT_URL)
    except Exception:
        return []

    listings = []
    seen = set()
    links = []

    for href_raw in re.findall(r'href=["\']([^"\']+/immobilien/[^"\']+)["\']', html, re.I):
        href = urljoin(KLATT_URL, clean_text(href_raw))
        href_lower = href.lower()
        if '/feed/' in href_lower:
            continue
        if '-mieten-' in href_lower:
            continue
        if '-kaufen-' not in href_lower:
            continue

        key = href.rstrip('/').lower()
        if key in links:
            continue
        links.append(key)

        idx = html.find(href_raw)
        chunk = html[max(0, idx - 1200):idx + 3200] if idx >= 0 else ''
        chunk_text = clean_text(chunk)

        title = normalize_title_from_link(href)
        price = ''
        area = extract_area_text(chunk_text)
        location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = extract_postcode_city_location(chunk_text)

        price_match = re.search(r'Kaufpreis\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', chunk_text, re.I)
        if not price_match:
            price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:EUR|€)', chunk_text, re.I)
        if price_match:
            price = clean_text(price_match.group(1))

        try:
            detail_html = fetch_html(href)
        except Exception:
            detail_html = ''

        if detail_html:
            detail_text = clean_text(detail_html)
            detail_title = extract_page_title(detail_html)
            detail_title = re.sub(r'\s*\|\s*Alexander\s+Klatt.*$', '', detail_title, flags=re.I).strip(' -|')
            if is_valid_title(detail_title):
                title = detail_title

            detail_price_match = re.search(r'Kaufpreis\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', detail_text, re.I)
            if not detail_price_match:
                detail_price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:EUR|€)', detail_text, re.I)
            if detail_price_match:
                price = clean_text(detail_price_match.group(1))

            detail_area = extract_area_text(detail_text)
            if detail_area:
                area = detail_area

            if not is_clean_location_text(location):
                detail_location = extract_postcode_city_location(detail_text)
                if is_clean_location_text(detail_location):
                    location = detail_location
            if not is_clean_location_text(location):
                location = extract_location_text(detail_text, '')

        if not is_clean_location_text(location):
            location = extract_location_from_link(href)
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, href)
        if len(listings) >= 12:
            break

    return listings


def fetch_ft_listings():
    return fetch_external_broker_listings(FT_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_tesch_listings():
    return fetch_generic_broker_listings(TESCH_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_ritter_listings():
    html = fetch_html(RITTER_URL)
    property_links = []
    seen_links = set()
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']*/immobilien/[^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = urljoin(RITTER_URL, clean_text(match.group(1)))
        if re.search(r'\.pdf(?:$|#)|/(?:karte|details?)/?$', href, re.I):
            continue
        key = href.rstrip('/').lower()
        if key in seen_links:
            continue
        seen_links.add(key)
        property_links.append((match.start(), href, clean_text(match.group(2))))

    listings = []
    seen = set()
    for index, (position, href, anchor_title) in enumerate(property_links):
        next_position = property_links[index + 1][0] if index + 1 < len(property_links) else len(html)
        block_text = clean_text(html[position:next_position])
        title = anchor_title if is_valid_title(anchor_title) else normalize_title_from_link(href)
        price_match = re.search(
            r'(?:Kaufpreis|Preis)\s*:?[\s\u00a0]*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)',
            block_text,
            re.I,
        )
        area_match = re.search(
            r'Wohnfl(?:ä|ae)che\s*:?[\s\u00a0]*([0-9]+(?:[.,][0-9]+)?)\s*m(?:²|2)',
            block_text,
            re.I,
        )
        location_match = re.search(r'\b(\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/ ]{2,50})\b', block_text)

        price = price_match.group(1) if price_match else ''
        area = area_match.group(1).replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else ''

        if not price or not area or not location:
            detail_html = fetch_html(href)
            detail_text = clean_text(detail_html)
            if not is_valid_title(title):
                title = extract_page_title(detail_html)
            if not price:
                detail_price = re.search(
                    r'(?:Kaufpreis|Preis)\s*:?[\s\u00a0]*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)',
                    detail_text,
                    re.I,
                )
                if detail_price:
                    price = detail_price.group(1)
            if not area:
                area = extract_area_text(detail_text)
            if not location:
                location = extract_postcode_city_location(detail_text)

        add_listing(listings, seen, title, price, area, location or UNKNOWN_LOCATION, href)

    return listings[:12]


def fetch_hirschmann_listings():
    return fetch_external_broker_listings(HIRSCHMANN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_rohrer_listings_source_specific():
    listings = []
    seen = set()
    page_urls = [
        ROHRER_URL,
        ROHRER_URL + '?__yPage=2',
    ]
    link_pattern = re.compile(
        r'href=["\']([^"\']*/immobilien-vermarktung/immobilien/[^"\']+-r-\d+\.html)["\']',
        re.I,
    )

    for page_url in page_urls:
        try:
            html = fetch_html(page_url)
        except Exception:
            continue

        for match in link_pattern.finditer(html):
            href = urljoin(page_url, clean_text(match.group(1)))
            if '-zum-kaufen-' not in href.lower():
                continue

            chunk = html[max(0, match.start() - 2600):match.start() + 2600]
            chunk_text = clean_text(chunk)

            title_match = re.search(r'<a[^>]+class=["\'][^"\']*angebote-teaser-slider-link[^"\']*["\'][^>]*>(.*?)</a>', chunk, re.I | re.S)
            title = clean_text(title_match.group(1)) if title_match else ''
            if re.search(r'(?i)\baktuelle\s+angebote\b', title):
                title = ''
            if not is_valid_title(title):
                title = normalize_title_from_link(href)
            if not is_valid_title(title):
                continue

            price_match = re.search(r'Kaufpreis\s*:?[\s\u00a0]*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', chunk_text, re.I)
            if not price_match:
                price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:EUR|€)', chunk_text, re.I)
            price = price_match.group(1) if price_match else ''

            area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?[\s\u00a0]*([0-9.,]+)', chunk_text, re.I)
            if not area_match:
                area_match = re.search(r'([0-9.,]+)\s*m²', chunk_text, re.I)
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

            location_match = re.search(r'(?:Ort|Lage|Standort)\s*:?[\s\u00a0]*([A-ZÄÖÜa-zäöüß\-/ ]{2,60})', chunk_text, re.I)
            location = clean_text(location_match.group(1)) if location_match else ''
            if re.search(r'(?i)\b(immobilien\s+vermarktung|zu\s+kaufen\s+in|kaufen\s+in)\b', location):
                location = ''

            slug_location = ''
            city_from_slug = re.search(r'-in-([a-z0-9\-]+)-r-\d+\.html$', href.lower())
            if city_from_slug:
                slug_location = decode_slug_words(city_from_slug.group(1)).title()

            if not is_clean_location_text(location):
                location = extract_location_from_title(title)
            if not is_clean_location_text(location):
                location = slug_location
            if not is_clean_location_text(location):
                location = extract_location_from_link(href)
            if not is_clean_location_text(location):
                location = UNKNOWN_LOCATION

            add_listing(listings, seen, title, price, area, location, href)

    return listings[:36]


def fetch_rohrer_listings():
    rows = fetch_rohrer_listings_source_specific()
    if rows:
        return rows
    return fetch_external_broker_listings(ROHRER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_mrlodge_listings():
    return fetch_external_broker_listings(MRLODGE_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_reichenberger_listings():
    return fetch_generic_broker_listings(REICHENBERGER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_heidtmann_listings():
    return fetch_generic_broker_listings(HEIDTMANN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_muellerenglisch_listings():
    return fetch_source_specific_broker_listings(MUELLER_ENGLISCH_URL, r'(immobilie|objekt|expose|angebot|kauf|xhtml)', r'(xhtml|immobilie|objekt|expose|kauf)')


def fetch_strobl_listings():
    return fetch_generic_broker_listings(STROBL_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_aundowohnbau_listings():
    return fetch_generic_broker_listings(AUNDOWOHNBAU_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_graef_listings():
    return fetch_generic_broker_listings(GRAEF_IMMO_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_roethig_listings():
    return fetch_generic_broker_listings(ROETHIG_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_wangenheim_listings():
    return fetch_external_broker_listings(WANGENHEIM_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_egger_listings():
    return fetch_generic_broker_listings(EGGER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_neuesnest_listings():
    return fetch_generic_broker_listings(NEUESNEST_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_parkavenue_listings():
    return fetch_external_broker_listings(PARKAVENUE_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_weber_listings():
    return fetch_generic_broker_listings(WEBER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_wurmseder_listings():
    return fetch_generic_broker_listings(WURMSEDER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_elvira_listings():
    return fetch_generic_broker_listings(ELVIRA_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_sothebys_listings():
    return fetch_external_broker_listings(SOTHEBYS_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_duerrenberger_listings():
    return fetch_source_specific_broker_listings(DUERRENBERGER_URL, r'(immobilie|objekt|expose|angebot|kauf|haeuser)', r'(haeuser-zum-kauf|immobilie|objekt|expose)')


def fetch_woehry_listings():
    return fetch_generic_broker_listings(WOEHRY_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_vonrodenhausen_listings():
    return fetch_generic_broker_listings(VONRODENHAUSEN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_martinaschwarz_listings():
    return fetch_generic_broker_listings(MARTINA_SCHWARZ_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_pienzenauer_listings():
    return fetch_generic_broker_listings(PIENZENAUER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_friedlmaier_listings():
    return fetch_generic_broker_listings(FRIEDLMAIER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_windhausen_listings():
    return fetch_generic_broker_listings(WINDHAUSEN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_maier_listings():
    return fetch_generic_broker_listings(MAIER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_riedl_listings():
    return fetch_generic_broker_listings(RIEDL_MAKLER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_heimmobilien_listings():
    return fetch_source_specific_broker_listings(HEIMMOBILIEN_URL, r'(immobilie|objekt|expose|angebot|kauf)', r'(immobilie|objekt|expose|angebote)')


def fetch_seebauer_listings():
    return fetch_generic_broker_listings(SEEBAUER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_zippold_listings():
    return fetch_generic_broker_listings(ZIPPOLD_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_muellergroscurth_listings():
    return fetch_detail_page_listings(MUELLER_GROSCURTH_URL, r'/immobilien/')


def fetch_bunzco_listings():
    return fetch_generic_broker_listings(BUNZCO_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_immosmart_listings():
    return fetch_generic_broker_listings(IMMOSMART_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_lehmannhueber_listings():
    return fetch_generic_broker_listings(LEHMANNHUEBER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_drescher_listings():
    return fetch_generic_broker_listings(DRESCHER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_sqmeter_listings():
    return fetch_generic_broker_listings(SQMETER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_wegener_listings():
    return fetch_source_specific_broker_listings(WEGENER_URL, r'(immobilie|objekt|expose|angebot|kauf|haeuser)', r'(haeuser-zum-kauf|immobilie|objekt|expose)')


def fetch_hackerglass_listings():
    return fetch_generic_broker_listings(HACKER_GLASS_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_wohnref_listings():
    return fetch_generic_broker_listings(WOHNREF_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_herrmann_listings():
    return fetch_detail_page_listings(HERRMANN_URL, r'cmd=searchDetails')


def fetch_schmidtmuenchen_listings():
    return fetch_generic_broker_listings(SCHMIDT_MUENCHEN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_davidjacques_listings():
    return fetch_generic_broker_listings(DAVID_JACQUES_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_dalexis_listings():
    return fetch_generic_broker_listings(DALEXIS_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_gg_listings():
    return fetch_generic_broker_listings(GG_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_marte_listings():
    return fetch_source_specific_broker_listings(MARTE_URL, r'(immobilie|objekt|expose|angebot|kauf|xhtml)', r'(xhtml|immobilie|objekt|expose|kauf)')


def fetch_dawonia_listings():
    return fetch_external_broker_listings(DAWONIA_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_orange_listings():
    return fetch_external_broker_listings(ORANGE_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_vorstadtmakler_listings():
    return fetch_generic_broker_listings(VORSTADTMAKLER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)')


def fetch_heidinger_listings_source_specific():
    listings = []
    seen = set()

    try:
        html = fetch_html(HEIDINGER_URL)
    except Exception:
        return listings

    for match in re.finditer(r'<article class="elementor-post[^"]*".*?</article>', html, re.I | re.S):
        block = match.group(0)
        link_match = re.search(r'<h1[^>]*class="elementor-post__title"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.I | re.S)
        if not link_match:
            continue

        href = urljoin(HEIDINGER_URL, clean_text(link_match.group(1)))
        title = clean_text(link_match.group(2))
        if not is_valid_title(title):
            continue

        excerpt_match = re.search(r'<div class="elementor-post__excerpt"[^>]*>(.*?)</div>', block, re.I | re.S)
        excerpt = clean_text(excerpt_match.group(1)) if excerpt_match else ''
        price_match = re.search(r'(?:EUR|€)\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)', excerpt, re.I)
        if not price_match:
            price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:EUR|€)', excerpt, re.I)
        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*(?:ca\.)?\s*([0-9.,]+)\s*m²', excerpt, re.I)

        price = price_match.group(1) if price_match else ''
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = ''
        from_match = re.search(r'(?i)\bvon\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/ ]{2,30})', title)
        if from_match:
            location = clean_text(from_match.group(1))
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = extract_location_text(excerpt, '')
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_imothek_listings_source_specific():
    listings = []
    seen = set()

    try:
        html = fetch_html(IMOTHEK_URL)
    except Exception:
        return listings

    for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.I | re.S):
        href = clean_text(match.group(1))
        full_href = urljoin(IMOTHEK_URL, href)
        if not re.search(r'/(?:immobilie|objekt|angebote?)/|expose|kaufen', full_href, re.I):
            continue

        chunk = html[max(0, match.start() - 1800):match.start() + 1800]
        chunk_text = clean_text(chunk)
        price_match = re.search(r'(?:Kaufpreis|Preis)\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', chunk_text, re.I)
        if not price_match:
            price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:EUR|€)', chunk_text, re.I)
        if not price_match:
            continue

        title = clean_text(match.group(2))
        if not is_valid_title(title):
            heading = re.search(r'<h2[^>]*>(.*?)</h2>|<h3[^>]*>(.*?)</h3>', chunk, re.I | re.S)
            if heading:
                title = clean_text(heading.group(1) or heading.group(2) or '')
        if not is_valid_title(title):
            continue

        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*(?:ca\.?\s*)?([0-9.,]+)', chunk_text, re.I)
        if not area_match:
            area_match = re.search(r'([0-9.,]+)\s*m²', chunk_text, re.I)
        location_match = re.search(r'(?:Ort|Lage|Standort|Stadt)\s*:?\s*([A-ZÄÖÜa-zäöüß\-/ ]{2,40})', chunk_text, re.I)

        price = price_match.group(1)
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
        location = clean_text(location_match.group(1)) if location_match else ''
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price, area, location, full_href)

    return listings[:12]


def fetch_mar_listings_source_specific():
    listings = []
    seen = set()

    try:
        html = fetch_html(MAR_URL)
    except Exception:
        return listings

    iframe_match = re.search(r'<iframe[^>]+src="([^"]+)"', html, re.I)
    if iframe_match:
        iframe_url = iframe_match.group(1).strip()
        if iframe_url.startswith('//'):
            iframe_url = 'https:' + iframe_url
        iframe_url = urljoin(MAR_URL, iframe_url)
        try:
            iframe_html = fetch_html(iframe_url)
        except Exception:
            iframe_html = ''

        if iframe_html:
            for match in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', iframe_html, re.I | re.S):
                href = urljoin(iframe_url, clean_text(match.group(1)))
                chunk = iframe_html[max(0, match.start() - 1800):match.start() + 1800]
                chunk_text = clean_text(chunk)
                price_match = re.search(r'(?:Kaufpreis|Preis)\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', chunk_text, re.I)
                if not price_match:
                    price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:EUR|€)', chunk_text, re.I)
                if not price_match:
                    continue

                title = clean_text(match.group(2))
                if not is_valid_title(title):
                    continue

                area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?\s*(?:ca\.?\s*)?([0-9.,]+)', chunk_text, re.I)
                if not area_match:
                    area_match = re.search(r'([0-9.,]+)\s*m²', chunk_text, re.I)
                location = extract_location_text(chunk_text, '')
                if not is_clean_location_text(location):
                    location = extract_location_from_title(title)
                if not is_clean_location_text(location):
                    location = UNKNOWN_LOCATION

                price = price_match.group(1)
                area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
                add_listing(listings, seen, title, price, area, location, href)

    return listings[:12]


def fetch_seeimmo_listings_source_specific():
    listings = []
    seen = set()

    try:
        html = fetch_html(SEEIMMO_URL)
    except Exception:
        return listings

    for match in re.finditer(r'<a([^>]+)href="(/aktuelle-immobilienangebote/[^"]+\.html)"([^>]*)>(.*?)</a>', html, re.I | re.S):
        attrs = f"{match.group(1)} {match.group(3)}"
        href = urljoin(SEEIMMO_URL, clean_text(match.group(2)))
        if not href.endswith('.html'):
            continue

        chunk = html[max(0, match.start() - 2200):match.start() + 2200]
        chunk_text = clean_text(chunk)
        price_match = re.search(r'Kaufpreis\s*:?\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?|auf Anfrage)', chunk_text, re.I)
        if not price_match:
            continue

        long_title = re.search(r'title="([A-ZÄÖÜ][A-ZÄÖÜ\-/ ]{2,30}:[^"]{10,})"', chunk, re.I)
        if long_title:
            title = clean_text(long_title.group(1))
        else:
            title_attr = re.search(r'title="([^"]+)"', attrs, re.I)
            title = clean_text(title_attr.group(1)) if title_attr else clean_text(match.group(4))
        title = re.sub(r'(?i)^\s*Merkzettel\s+', '', title).strip()
        if title.lower() in {'details', 'merkzettel', 'empfehlen', 'anfragen'}:
            continue
        if title.lower() in {'herzlich willkommen'}:
            continue
        if not is_valid_title(title):
            fallback_title = re.search(r'title="([A-ZÄÖÜ][^"]{12,})"', chunk, re.I)
            if fallback_title:
                title = clean_text(fallback_title.group(1))
        if not is_valid_title(title):
            continue

        area_match = re.search(r'Fläche\s*:?\s*([0-9.,]+)', chunk_text, re.I)
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

        location = ''
        city_prefix = re.match(r'^([A-ZÄÖÜ][A-ZÄÖÜ\-/ ]{2,30}):', title)
        if city_prefix:
            location = clean_text(city_prefix.group(1).title())
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price_match.group(1), area, location, href)

    return listings[:12]


def fetch_sopart_listings_source_specific():
    listings = []
    seen = set()

    try:
        html = fetch_html(SOPART_URL)
    except Exception:
        return listings

    pattern = re.compile(r'<a[^>]+href="([^"]*cmd=searchDetails[^"]*)"[^>]*>(.*?)</a>', re.I | re.S)
    for match in pattern.finditer(html):
        href = urljoin(SOPART_URL, clean_text(match.group(1)).replace('&amp;', '&'))
        chunk = html[max(0, match.start() - 400):match.start() + 1600]
        chunk_text = clean_text(chunk)

        title = clean_text(match.group(2))
        if re.fullmatch(r'[0-9.,\-\s€]+(?:NEU|VERKAUFT)?', title, re.I):
            title = ''
        if not is_valid_title(title):
            title_match = re.search(r'(SOPART\s+IMMOBILIEN\s*-\s*[^|]{12,180})', chunk_text, re.I)
            if title_match:
                title = clean_text(title_match.group(1))
        if not is_valid_title(title):
            continue

        if re.search(r'\bVERKAUFT\b', chunk_text, re.I) and not re.search(r'\d{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?\s*,-\s*€', chunk_text):
            continue

        price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*,-?\s*€', chunk_text, re.I)
        if not price_match:
            continue

        area_match = re.search(r'([0-9]{2,4}(?:,[0-9]{1,2})?)\s*m\s*WOHNFL', chunk_text, re.I)
        area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

        location_match = re.search(r'€\s*(?:NEU|VERKAUFT)?\s*([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/ ]{2,45})\s+SOPART\s+IMMOBILIEN', chunk_text, re.I)
        location = clean_text(location_match.group(1)) if location_match else ''
        if not is_clean_location_text(location):
            location = extract_location_from_title(title)
        if not is_clean_location_text(location):
            location = UNKNOWN_LOCATION

        add_listing(listings, seen, title, price_match.group(1), area, location, href)

    return listings[:12]


def fetch_tsc_listings_source_specific():
    listings = []
    seen = set()

    for page_url in TSC_URLS:
        try:
            html = fetch_html(page_url)
        except Exception:
            continue

        for match in re.finditer(r'<a[^>]+href="([^"]*?/kaufen/[^"]+)"[^>]*>', html, re.I | re.S):
            anchor_html = match.group(0)
            if 'hidden-trigger-link' not in anchor_html:
                continue

            href = urljoin(page_url, clean_text(match.group(1)))
            title_match = re.search(r'title="([^"]+)"', anchor_html, re.I)
            title = clean_text(title_match.group(1)) if title_match else ''
            if not is_valid_title(title):
                continue

            chunk = html[max(0, match.start() - 1500):match.start() + 7000]
            chunk_text = clean_text(chunk)

            price_match = re.search(r'([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*€\s*Kaufpreis', chunk_text, re.I)
            if not price_match:
                price_match = re.search(r'Kaufpreis\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*€', chunk_text, re.I)
            if not price_match:
                price_match = re.search(r'ab\s*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*€\s*Kaufpreis', chunk_text, re.I)
            if not price_match:
                continue

            area_match = re.search(r'([0-9]{2,4}(?:,[0-9]{1,2})?)\s*m²\s*Wohnfl', chunk_text, re.I)
            if not area_match:
                area_match = re.search(r'Wohnfl(?:ä|ae)che\s*([0-9]{2,4}(?:,[0-9]{1,2})?)\s*m²', chunk_text, re.I)
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''

            location_match = re.search(r'([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/ ]{2,35})\s+Ort', chunk_text, re.I)
            location = clean_text(location_match.group(1)) if location_match else ''
            if not is_clean_location_text(location):
                location = extract_location_from_title(title)
            if not is_clean_location_text(location):
                location = extract_location_from_link(href)
            if not is_clean_location_text(location):
                location = UNKNOWN_LOCATION

            add_listing(listings, seen, title, price_match.group(1), area, location, href)
            if len(listings) >= 12:
                return listings

    return listings


def fetch_seimmobilien_listings():
    rows = fetch_source_specific_broker_listings(
        SE_IMMOBILIEN_HAEUSER_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(Haeuser-zum-Kauf|Eigentumswohnungen|cmd=expose|objekt|immobilie)',
    )
    extras = fetch_source_specific_broker_listings(
        SE_IMMOBILIEN_WOHNUNGEN_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(Haeuser-zum-Kauf|Eigentumswohnungen|cmd=expose|objekt|immobilie)',
    )
    return merge_listing_rows(rows, extras)


def fetch_pscheidt_listings():
    rows = fetch_source_specific_broker_listings(
        PSCHEIDT_HAEUSER_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(haeuser|wohnungen|cmd=expose|objekt|immobilie)',
    )
    extras = fetch_source_specific_broker_listings(
        PSCHEIDT_WOHNUNGEN_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(haeuser|wohnungen|cmd=expose|objekt|immobilie)',
    )
    return merge_listing_rows(rows, extras)


def fetch_wolf_listings():
    rows = fetch_source_specific_broker_listings(
        WOLF_HAUS_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(haus|wohnung|cmd=expose|objekt|immobilie)',
    )
    extras = fetch_source_specific_broker_listings(
        WOLF_WOHNUNG_URL,
        r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)',
        r'(haus|wohnung|cmd=expose|objekt|immobilie)',
    )
    return merge_listing_rows(rows, extras)


def parse_rsi_listing_blocks(base_url, html):
    listings = []
    seen = set()
    blocks = re.findall(
        r'<article\b[^>]*>.*?</article>|<li\b[^>]*class=["\'][^"\']*(?:object|immobil|angebot|property)[^"\']*["\'][^>]*>.*?</li>',
        html,
        re.I | re.S,
    )
    for block in blocks:
        link_match = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\']', block, re.I)
        if not link_match:
            continue
        href = urljoin(base_url, clean_text(link_match.group(1)))
        if is_non_listing_url(href) or not re.search(r'(?:immobilie|objekt|expose|angebot)', href, re.I):
            continue
        heading_match = re.search(r'<h[1-4]\b[^>]*>(.*?)</h[1-4]>', block, re.I | re.S)
        title = clean_text(heading_match.group(1)) if heading_match else extract_title(block)
        block_text = clean_text(block)
        price_match = re.search(
            r'(?:Kaufpreis|Preis)\s*:?[ ]*([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{2})?)\s*(?:€|EUR)',
            block_text,
            re.I,
        )
        area_match = re.search(r'Wohnfl(?:ä|ae)che\s*:?[ ]*(?:ca\.?\s*)?([0-9.,]+)\s*(?:m²|qm)', block_text, re.I)
        location_match = re.search(r'(?:Ort|Lage|Standort)\s*:?[ ]*([^<|\n]+)', block, re.I)
        location = clean_text(location_match.group(1)) if location_match else extract_location_text(block_text, '')
        if not price_match or not is_valid_title(title) or not is_clean_location_text(location):
            continue
        add_listing(listings, seen, title, price_match.group(1), area_match.group(1) if area_match else '', location, href)
    return listings[:12]


def fetch_rsi_listings():
    rows = []
    for page_url in (RSI_EINFAMILIEN_URL, RSI_MEHRFAMILIEN_URL, RSI_WOHNUNGEN_URL):
        try:
            html = fetch_html(page_url)
        except Exception:
            continue
        rows = merge_listing_rows(rows, parse_rsi_listing_blocks(page_url, html))
    return rows[:12]


def fetch_joseffrei_listings():
    listings = []
    seen = set()
    for page_url in (JOSEF_FREI_HAEUSER_URL, JOSEF_FREI_WOHNUNGEN_URL):
        try:
            html = fetch_html(page_url)
        except Exception:
            continue
        openings = list(re.finditer(r'<div\b[^>]*class=["\'][^"\']*et_pb_text[^"\']*["\'][^>]*>', html, re.I))
        for index, opening in enumerate(openings):
            end = openings[index + 1].start() if index + 1 < len(openings) else min(len(html), opening.start() + 8000)
            block = html[opening.start():end]
            text = clean_text(block)
            object_match = re.search(r'Objekt(?:-|\s)?(?:Nr\.?\s*)?(\d{3,})', text, re.I)
            if not object_match:
                continue
            headings = [clean_text(value) for value in re.findall(r'<h[1-4][^>]*>(.*?)</h[1-4]>', block, re.I | re.S)]
            title = next((value for value in headings if is_valid_title(value) and not re.search(r'^(?:Objekt|Suchen nach)', value, re.I)), '')
            if not title:
                title_match = re.search(r'Objekt(?:-|\s)?(?:Nr\.?\s*)?\d{3,}\s+(.+?)(?=\s+(?:Kaufpreis|Preis|Wohnfl|\d+[.,]?\d*\s*m²))', text, re.I)
                title = clean_text(title_match.group(1)) if title_match else ''
            price_match = re.search(r'(Preis auf Anfrage|Kaufpreis\s*:?[ ]*([0-9][0-9.]*\s*(?:,\d{2})?\s*€))', text, re.I)
            area_match = re.search(r'(?<![A-Za-zÄÖÜäöüß])Wohnfläche\s*:?\s*(?:ca\.\s*)?([0-9][0-9.,]*)\s*m²', text, re.I)
            if not title or not price_match:
                continue
            price = price_match.group(2) or price_match.group(1)
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
            location_match = re.search(r'\b(München[- /][A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]+)', title)
            location = location_match.group(1) if location_match else UNKNOWN_LOCATION
            add_listing(listings, seen, title, price, area, location, f'{page_url}#objekt-{object_match.group(1)}')
    return listings[:12]


def fetch_lebenstraum_listings():
    try:
        html = fetch_html(LEBENSTRAUM_URL)
    except Exception:
        return []
    listings = []
    seen = set()
    detail_re = re.compile(r'/(?:immobilie|objekt|expose)/[^/?#]+/?$', re.I)
    detail_anchors = [
        anchor for anchor in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S)
        if detail_re.search(urlparse(urljoin(LEBENSTRAUM_URL, clean_text(anchor.group(1)))).path)
    ]
    for index, anchor in enumerate(detail_anchors):
        href = urljoin(LEBENSTRAUM_URL, clean_text(anchor.group(1)))
        fallback_start = detail_anchors[index - 1].end() if index else 0
        prefix = html[max(fallback_start, anchor.start() - 5000):anchor.start()]
        openings = list(re.finditer(
            r'<(?:article|li|section)\b[^>]*>|<div\b[^>]*class=["\'][^"\']*(?:card|immobil|objekt|angebot|property)[^"\']*["\'][^>]*>',
            prefix,
            re.I,
        ))
        start = anchor.start() - len(prefix) + openings[-1].start() if openings else fallback_start
        block = html[start:anchor.end()]
        text = clean_text(block)
        headings = [clean_text(value) for value in re.findall(r'<h[1-4][^>]*>(.*?)</h[1-4]>', block, re.I | re.S)]
        title = next((value for value in reversed(headings) if is_valid_title(value)), '')
        price_match = re.search(r'(Preis auf Anfrage|[0-9][0-9.]*\s*(?:,\d{2})?\s*€)', text, re.I)
        area_match = re.search(r'(?<![A-Za-zÄÖÜäöüß])Wohnfläche\s*:?\s*(?:ca\.\s*)?([0-9][0-9.,]*)\s*m²', text, re.I)
        location_match = re.search(r'\b(\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/]+)', text)
        if title and price_match:
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
            add_listing(listings, seen, title, price_match.group(1), area, location_match.group(1) if location_match else UNKNOWN_LOCATION, href)
    return listings[:12]


def fetch_ramonaneckar_listings():
    try:
        html = fetch_html(RAMONANECKAR_URL)
    except Exception:
        return []
    listings = []
    seen = set()
    cards = list(re.finditer(r'<(?:div|article|li)\b[^>]*class=["\'][^"\']*w-dyn-item[^"\']*["\'][^>]*>', html, re.I))
    for index, card in enumerate(cards):
        end = cards[index + 1].start() if index + 1 < len(cards) else min(len(html), card.start() + 10000)
        block = html[card.start():end]
        link_match = re.search(r'<a\b[^>]*href=["\']([^"\']*/short-immobilienangebote/rni-[^"\']+)["\']', block, re.I)
        if not link_match:
            continue
        text = clean_text(block)
        headings = [clean_text(value) for value in re.findall(r'<h[1-4][^>]*>(.*?)</h[1-4]>', block, re.I | re.S)]
        title = next((value for value in headings if is_valid_title(value)), '')
        price_match = re.search(r'(Preis auf Anfrage|[0-9][0-9.]*\s*(?:,\d{2})?\s*€)', text, re.I)
        area_match = re.search(r'(?<![A-Za-zÄÖÜäöüß])Wohnfläche\s*:?\s*(?:ca\.\s*)?([0-9][0-9.,]*)\s*m²', text, re.I)
        location_match = re.search(r'\b(\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/]+)', text)
        if title and price_match:
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
            add_listing(listings, seen, title, price_match.group(1), area, location_match.group(1) if location_match else UNKNOWN_LOCATION, urljoin(RAMONANECKAR_URL, link_match.group(1)))
    return listings[:12]


def fetch_fairhomes_listings():
    try:
        html = fetch_html(FAIR_HOMES_URL)
    except Exception:
        return []
    listings = []
    seen = set()
    cards = list(re.finditer(
        r'<div\b[^>]*class=["\'](?=[^"\']*\bdmRespRow\b)(?=[^"\']*\bhide-for-large\b)(?=[^"\']*\bhide-for-medium\b)[^"\']*["\'][^>]*>',
        html,
        re.I,
    ))
    for index, card in enumerate(cards):
        following_row = re.search(r'<div\b[^>]*class=["\'][^"\']*\bdmRespRow\b[^"\']*["\'][^>]*>', html[card.end():], re.I)
        end = card.end() + following_row.start() if following_row else len(html)
        block = html[card.start():end]
        if len(re.findall(r'<div\b[^>]*class=["\'][^"\']*\bdmRespCol\b[^"\']*["\']', block, re.I)) != 2:
            continue
        anchor = re.search(r'<a\b[^>]*href=["\'](/projekt-20[^"\']*)["\']', block, re.I)
        if not anchor:
            continue
        title_match = re.search(
            r'<div\b[^>]*class=["\'][^"\']*\bdmNewParagraph\b[^"\']*["\'][^>]*>.*?<h2\b[^>]*>\s*<span\b[^>]*class=["\'][^"\']*\bm-font-size-26\b[^"\']*["\'][^>]*>(.*?)</span>',
            block,
            re.I | re.S,
        )
        title = clean_text(title_match.group(1)) if title_match else ''
        data_start = re.search(
            r'<div\b[^>]*class=["\'](?=[^"\']*\bdmNewParagraph\b)(?=[^"\']*\bhide-for-large\b)[^"\']*["\'][^>]*>',
            block,
            re.I,
        )
        data_text = clean_text(block[data_start.start():]) if data_start else ''
        price_match = re.search(r'(Preis auf Anfrage|[0-9][0-9.]*\s*(?:,\d{2}|,-)?\s*€)', data_text, re.I)
        area_match = re.search(r'([0-9][0-9.,]*)\s*m²\s*WFL\b', data_text, re.I)
        location_match = re.search(r'(?<!\d)(?:\d)?(\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/]+(?:\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/]+){0,2})', data_text)
        if title and price_match:
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
            add_listing(listings, seen, title, price_match.group(1), area, location_match.group(1) if location_match else UNKNOWN_LOCATION, urljoin(FAIR_HOMES_URL, anchor.group(1)))
    return listings[:13]


def fetch_stierling_listings():
    try:
        html = fetch_html(STIERLING_URL)
    except Exception:
        return []
    listings = []
    seen = set()
    links = list(re.finditer(r'<a\b[^>]*href=["\'](https?://[^"\']*immobilienscout24\.de/expose/[^"\']+)["\'][^>]*>.*?</a>', html, re.I | re.S))
    links.extend(re.finditer(r'goToExpose\s*\(\s*["\'](https?://[^"\']*immobilienscout24\.de/expose/[^"\']+)["\']\s*\)', html, re.I))
    links = sorted(links, key=lambda value: value.start())
    for index, link_match in enumerate(links):
        href = clean_text(link_match.group(1))
        fallback_start = links[index - 1].end() if index else 0
        prefix = html[max(fallback_start, link_match.start() - 5000):link_match.start()]
        card_openings = list(re.finditer(
            r'<(?:article|li|section)\b[^>]*>|<div\b[^>]*class=["\'][^"\']*(?:card|immobil|objekt|angebot|property)[^"\']*["\'][^>]*>',
            prefix,
            re.I,
        ))
        start = link_match.start() - len(prefix) + card_openings[-1].start() if card_openings else fallback_start
        block = html[start:link_match.end()]
        text = clean_text(block)
        headings = [clean_text(value) for value in re.findall(r'<h[1-4][^>]*>(.*?)</h[1-4]>', block, re.I | re.S)]
        title = next((value for value in reversed(headings) if is_valid_title(value) and not re.search(r'^(?:Suche|Zum Exposé)', value, re.I)), '')
        price_matches = list(re.finditer(r'(Preis auf Anfrage|[0-9][0-9.]*\s*(?:,\d{2})?\s*€)', text, re.I))
        price_match = price_matches[-1] if price_matches else None
        area_match = re.search(r'(?<![A-Za-zÄÖÜäöüß])Wohnfläche\s*:?[\s\S]{0,30}?([0-9][0-9.,]*)\s*m²', text, re.I)
        location_match = re.search(r'\b(\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/]+)', text)
        if not title or not price_match or not area_match or not location_match:
            try:
                detail_html = fetch_html(href)
            except Exception:
                detail_html = ''
            detail_text = clean_text(detail_html)
            if not title:
                detail_heading = re.search(r'<h1[^>]*>(.*?)</h1>', detail_html, re.I | re.S)
                title = clean_text(detail_heading.group(1)) if detail_heading else ''
            if not price_match:
                price_match = re.search(r'(Preis auf Anfrage|[0-9][0-9.]*\s*(?:,\d{2})?\s*€)', detail_text, re.I)
            if not area_match:
                area_match = re.search(r'(?<![A-Za-zÄÖÜäöüß])Wohnfläche\s*:?[\s\S]{0,30}?([0-9][0-9.,]*)\s*m²', detail_text, re.I)
            if not location_match:
                location_match = re.search(r'\b(\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-/]+)', detail_text)
        if title and price_match:
            area_value = next((value for value in area_match.groups() if value), '') if area_match else ''
            area = area_value.replace('.', '').replace(',', '.')
            add_listing(listings, seen, title, price_match.group(1), area, location_match.group(1) if location_match else UNKNOWN_LOCATION, href)
    return listings[:12]


def fetch_aufrecht_listings():
    rows = fetch_generic_broker_listings(AUFRECHT_T1_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)')
    extras = fetch_generic_broker_listings(AUFRECHT_T2_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)')
    return merge_listing_rows(rows, extras)


def fetch_zardini_listings():
    listings = []
    seen = set()
    for page_url in (ZARDINI_HAEUSER_URL, ZARDINI_WOHNUNGEN_URL):
        try:
            html = fetch_html(page_url)
        except Exception:
            continue
        list_row = re.search(r'<div\b[^>]*class=["\'](?=[^"\']*\brow\b)(?=[^"\']*\bobjektliste\b)[^"\']*["\'][^>]*>', html, re.I)
        if not list_row:
            continue
        cards = list(re.finditer(r'<div\b[^>]*class=["\'][^"\']*\blistenobjekt\b[^"\']*["\'][^>]*>', html[list_row.end():], re.I))
        for index, card in enumerate(cards):
            end = cards[index + 1].start() if index + 1 < len(cards) else len(html)
            block = html[list_row.end() + card.start():list_row.end() + end]
            anchor = re.search(
                r'<a\b[^>]*href=["\']([^"\']*immobiliendetails\.xhtml\?id\[obj0\]=[^"\']+)["\']',
                block,
                re.I,
            )
            if not anchor:
                continue
            href = urljoin(page_url, anchor.group(1))
            price_field = re.search(r'<span\b[^>]*class=["\'][^"\']*\bobj-price\b[^"\']*["\'][^>]*>(.*?)</span>', block, re.I | re.S)
            price_match = re.search(r'(Preis auf Anfrage|[0-9][0-9.]*\s*(?:,\d{2})?\s*€)', clean_text(price_field.group(1)), re.I) if price_field else None
            living_area_field = re.search(
                r'<div\b[^>]*class=["\'](?=[^"\']*\bobjectdata\b)(?=[^"\']*\bwohnen\b)[^"\']*["\'][^>]*>(.*?)</div>',
                block,
                re.I | re.S,
            )
            first_area_column = re.search(r'<span\b[^>]*class=["\'][^"\']*\bgrid-33\b[^"\']*["\'][^>]*>(.*?)</span>\s*</span>', living_area_field.group(1), re.I | re.S) if living_area_field else None
            area_match = re.search(r'([0-9][0-9.,]*)\s*m²', clean_text(first_area_column.group(1)), re.I) if first_area_column else None
            geo_field = re.search(r'<[^>]*class=["\'][^"\']*\bobj-geo\b[^"\']*["\'][^>]*>(.*?)</[^>]+>', block, re.I | re.S)
            location_match = re.search(r'\bin\s+(\d{5}\s+.+)', clean_text(geo_field.group(1)), re.I) if geo_field else None
            try:
                detail_html = fetch_html(href)
            except Exception:
                detail_html = ''
            title = extract_page_title(detail_html)
            title = re.sub(r'\s*(?:\||–|-)\s*Zardini(?:\s+Immobilien)?\b.*$', '', title, flags=re.I).strip(' -|')
            if not title or not price_match:
                continue
            location = clean_text(location_match.group(1)) if location_match else UNKNOWN_LOCATION
            area = area_match.group(1).replace('.', '').replace(',', '.') if area_match else ''
            add_listing(listings, seen, title, price_match.group(1), area, location, href)
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
    ('nikki', fetch_nikki_listings),
    ('tsc', fetch_tsc_listings),
    ('imothek', fetch_imothek_listings),
    ('vr', fetch_vr_listings),
    ('stolze', fetch_stolze_listings),
    ('realwert', fetch_realwert_listings),
    ('eden', fetch_eden_listings),
    ('imliving', fetch_imliving_listings),
    ('webau', fetch_webau_listings),
    ('mar', fetch_mar_listings),
    ('seeimmo', fetch_seeimmo_listings),
    ('heidinger', fetch_heidinger_listings),
    ('funer', fetch_funer_listings),
    ('weiherer', fetch_weiherer_listings),
    ('mb', fetch_mb_listings),
    ('fischer', fetch_fischer_listings),
    ('heimhuber', fetch_heimhuber_listings),
    ('citigrund', fetch_citigrund_listings),
    ('georgi', fetch_georgi_listings),
    ('akurat', fetch_akurat_listings),
    ('hegerich', fetch_hegerich_listings),
    ('eder', fetch_eder_listings),
    ('gerschlauer', fetch_gerschlauer_listings),
    ('dahler', fetch_dahler_listings),
    ('krimbacher', fetch_krimbacher_listings),
    ('klatt', fetch_klatt_listings),
    ('ft', fetch_ft_listings),
    ('tesch', fetch_tesch_listings),
    ('ritter', fetch_ritter_listings),
    ('hirschmann', fetch_hirschmann_listings),
    ('rohrer', fetch_rohrer_listings),
    ('mrlodge', fetch_mrlodge_listings),
    ('reichenberger', fetch_reichenberger_listings),
    ('heidtmann', fetch_heidtmann_listings),
    ('muellerenglisch', fetch_muellerenglisch_listings),
    ('strobl', fetch_strobl_listings),
    ('aundowohnbau', fetch_aundowohnbau_listings),
    ('graef', fetch_graef_listings),
    ('roethig', fetch_roethig_listings),
    ('wangenheim', fetch_wangenheim_listings),
    ('egger', fetch_egger_listings),
    ('neuesnest', fetch_neuesnest_listings),
    ('parkavenue', fetch_parkavenue_listings),
    ('weber', fetch_weber_listings),
    ('wurmseder', fetch_wurmseder_listings),
    ('elvira', fetch_elvira_listings),
    ('sothebys', fetch_sothebys_listings),
    ('duerrenberger', fetch_duerrenberger_listings),
    ('woehry', fetch_woehry_listings),
    ('vonrodenhausen', fetch_vonrodenhausen_listings),
    ('martinaschwarz', fetch_martinaschwarz_listings),
    ('pienzenauer', fetch_pienzenauer_listings),
    ('friedlmaier', fetch_friedlmaier_listings),
    ('windhausen', fetch_windhausen_listings),
    ('maier', fetch_maier_listings),
    ('riedl', fetch_riedl_listings),
    ('heimmobilien', fetch_heimmobilien_listings),
    ('seebauer', fetch_seebauer_listings),
    ('zippold', fetch_zippold_listings),
    ('muellergroscurth', fetch_muellergroscurth_listings),
    ('bunzco', fetch_bunzco_listings),
    ('immosmart', fetch_immosmart_listings),
    ('lehmannhueber', fetch_lehmannhueber_listings),
    ('drescher', fetch_drescher_listings),
    ('sqmeter', fetch_sqmeter_listings),
    ('wegener', fetch_wegener_listings),
    ('hackerglass', fetch_hackerglass_listings),
    ('wohnref', fetch_wohnref_listings),
    ('herrmann', fetch_herrmann_listings),
    ('schmidtmuenchen', fetch_schmidtmuenchen_listings),
    ('davidjacques', fetch_davidjacques_listings),
    ('dalexis', fetch_dalexis_listings),
    ('gg', fetch_gg_listings),
    ('marte', fetch_marte_listings),
    ('dawonia', fetch_dawonia_listings),
    ('orange', fetch_orange_listings),
    ('vorstadtmakler', fetch_vorstadtmakler_listings),
    ('teambim', lambda: fetch_external_broker_listings(TEAMBIM_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)')),
    ('sozius', fetch_sozius_listings_retry_alt),
    ('andreasschmid', lambda: fetch_source_specific_broker_listings(ANDREAS_SCHMID_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilie|objekt|expose|angebot|kauf)')),
    ('muenchnerimmobilien', lambda: fetch_source_specific_broker_listings(MUENCHNER_IMMOBILIEN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|haeuser)', r'(Haeuser-zum-Kauf|cmd=expose|objekt|immobilie)')),
    ('ausdemhaeuschen', lambda: fetch_source_specific_broker_listings(AUSDEMHAEUSCHEN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilie|objekt|expose|angebot|kauf)')),
    ('hallinger', lambda: fetch_source_specific_broker_listings(HALLINGER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(kaufobjekte|cmd=expose|objekt|immobilie)')),
    ('cki', lambda: fetch_generic_broker_listings(CKI_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)')),
    ('windisch', lambda: fetch_source_specific_broker_listings(WINDISCH_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(kaufen|angebote|cmd=expose|objekt|immobilie)')),
    ('im7', lambda: fetch_source_specific_broker_listings(IM7_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(KAUF|cmd=expose|objekt|immobilie)')),
    ('seimmobilien', fetch_seimmobilien_listings),
    ('happyimmo', lambda: fetch_source_specific_broker_listings(HAPPY_IMMO_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(xhtml|cmd=expose|objekt|immobilie)')),
    ('wandl', lambda: fetch_source_specific_broker_listings(WANDL_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(aktuelle-immobilien|cmd=expose|objekt|immobilie)')),
    ('emslander', lambda: fetch_source_specific_broker_listings(EMSLANDER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilie|objekt|expose|angebot|kauf)')),
    ('hoser', lambda: fetch_external_broker_listings(HOSER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)')),
    ('feuerlein', lambda: fetch_source_specific_broker_listings(FEUERLEIN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilienangebot|cmd=expose|objekt|immobilie)')),
    ('lebenstraum', fetch_lebenstraum_listings),
    ('gschwender', lambda: fetch_source_specific_broker_listings(GSCHWENDER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(angebote|cmd=expose|objekt|immobilie)')),
    ('maurer', lambda: fetch_source_specific_broker_listings(MAURER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(Angebote|cmd=expose|objekt|immobilie)')),
    ('pscheidt', fetch_pscheidt_listings),
    ('bechler', lambda: fetch_source_specific_broker_listings(BECHLER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(verkauf|vermietung|kauf|cmd=expose|objekt|immobilie)')),
    ('isarestate', lambda: fetch_source_specific_broker_listings(ISARESTATE_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(angebot|cmd=expose|objekt|immobilie)')),
    ('wesoly', lambda: fetch_source_specific_broker_listings(WESOLY_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(angebote|kauf|cmd=expose|objekt|immobilie)')),
    ('westend', lambda: fetch_source_specific_broker_listings(IMMOBILIENWESTEND_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(Haeuser-zum-Kauf|cmd=expose|objekt|immobilie)')),
    ('stierling', fetch_stierling_listings),
    ('finestep', lambda: fetch_generic_broker_listings(FINESTEP_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)')),
    ('fairhomes', fetch_fairhomes_listings),
    ('chalet', lambda: fetch_source_specific_broker_listings(CHALET_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(Angebote|cmd=expose|objekt|immobilie)')),
    ('kraftziller', lambda: fetch_source_specific_broker_listings(KRAFT_ZILLER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(Haeuser-zum-Kauf|cmd=expose|objekt|immobilie)')),
    ('wolf', fetch_wolf_listings),
    ('bayergrund', lambda: fetch_source_specific_broker_listings(BAYERGRUND_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(angebote|cmd=expose|objekt|immobilie)')),
    ('immops', lambda: fetch_source_specific_broker_listings(IMMOBILIEN_PS_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(angebote|cmd=expose|objekt|immobilie)')),
    ('rsi', fetch_rsi_listings),
    ('siemax', lambda: fetch_generic_broker_listings(SIEMAX_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)')),
    ('joseffrei', fetch_joseffrei_listings),
    ('luenendonk', lambda: fetch_source_specific_broker_listings(LUENENDONK_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(Objekte|cmd=expose|objekt|immobilie)')),
    ('harinali', lambda: fetch_source_specific_broker_listings(HARINALI_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilienangebote|cmd=expose|objekt|immobilie)')),
    ('aufrecht', fetch_aufrecht_listings),
    ('ramonaneckar', fetch_ramonaneckar_listings),
    ('mytropper', lambda: fetch_source_specific_broker_listings(MYTROPPER_URL, r'(cmd=searchDetails|cmd=expose|immobilie|objekt|angebot|kauf|haus|wohnung)', r'(cmd=searchDetails|cmd=expose|obj-)')),
    ('sriimmo', lambda: fetch_source_specific_broker_listings(SRI_IMMO_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(kaufen|mieten|cmd=expose|objekt|immobilie)')),
    ('zg', lambda: fetch_external_broker_listings(ZG_IMMOBILIEN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)')),
    ('zardini', fetch_zardini_listings),
    ('reischl', lambda: fetch_source_specific_broker_listings(REISCHL_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen|objects)', r'(objects|cmd=expose|objekt|immobilie)')),
    ('gattinger', lambda: fetch_source_specific_broker_listings(GATTINGER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(angebote|cmd=expose|objekt|immobilie)')),
]


ZERO_RESULT_RETRY_FETCHERS = {
    'engel': lambda: fetch_source_specific_with_embedded_retry(ENGEL_URLS[0], r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)', r'(expose|immobilie|objekt)'),
    'heidinger': fetch_heidinger_listings_source_specific,
    'imothek': fetch_imothek_listings_source_specific,
    'mar': fetch_mar_listings_source_specific,
    'seeimmo': fetch_seeimmo_listings_source_specific,
    'sopart': fetch_sopart_listings_source_specific,
    'tsc': fetch_tsc_listings_source_specific,
    'weiherer': lambda: fetch_source_specific_broker_listings(WEIHERER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)', r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'),
    'mb': lambda: fetch_external_broker_listings(MB_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'),
    'fischer': fetch_fischer_listings_retry_alt,
    'heimhuber': lambda: fetch_source_specific_broker_listings(HEIMHUBER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)', r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'),
    'citigrund': fetch_citigrund_listings_retry_alt,
    'georgi': lambda: fetch_source_specific_broker_listings(GEORGI_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)', r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'),
    'akurat': fetch_akurat_listings_retry_alt,
    'hegerich': fetch_hegerich_listings_retry_alt,
    'eder': lambda: fetch_source_specific_broker_listings(EDER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)', r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'),
    'gerschlauer': fetch_gerschlauer_listings_retry_alt,
    'dahler': fetch_dahler_listings_retry_alt,
    'krimbacher': fetch_krimbacher_listings_retry_alt,
    'hallinger': fetch_hallinger_listings_retry_alt,
    'muenchnerimmobilien': lambda: fetch_source_specific_with_embedded_retry(MUENCHNER_IMMOBILIEN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(Haeuser-zum-Kauf|cmd=expose|objekt|immobilie|detail|expose)'),
    'feuerlein': lambda: fetch_source_specific_with_embedded_retry(FEUERLEIN_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilienangebot|angebote|objekt|immobilie|detail|expose)'),
    'klatt': fetch_klatt_listings_source_specific,
    'ft': fetch_ft_listings_retry_alt,
    'tesch': lambda: fetch_source_specific_broker_listings(TESCH_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)', r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'),
    'ritter': lambda: fetch_source_specific_broker_listings(RITTER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)', r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung)'),
    'hirschmann': fetch_hirschmann_listings_retry_alt,
    'rohrer': fetch_rohrer_listings_source_specific,
    'mrlodge': fetch_mrlodge_listings_retry_alt,
    'wangenheim': fetch_wangenheim_listings_retry_alt,
    'parkavenue': lambda: fetch_property_link_cards_retry(PARKAVENUE_URL, r'/apartment/'),
    'elvira': fetch_elvira_listings_retry_alt,
    'gg': lambda: fetch_source_specific_broker_listings(GG_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilienangebote|angebote|angebot|objekt|immobilie|kauf)'),
    'graef': lambda: fetch_source_specific_broker_listings(GRAEF_IMMO_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilien|angebote|angebot|objekt|immobilie|expose)'),
    'egger': lambda: fetch_property_link_cards_retry(EGGER_URL, r'/immobilien/objekt/\?oid='),
    'mytropper': fetch_mytropper_listings_retry_alt,
    'seebauer': lambda: fetch_source_specific_with_embedded_retry(SEEBAUER_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(immobilien|angebote|angebot|objekt|immobilie|detail|expose)'),
    'aundowohnbau': lambda: fetch_source_specific_broker_listings(AUNDOWOHNBAU_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(bestandsobjekte|immobilien|angebot|objekt|immobilie|kauf)'),
    'isarestate': fetch_isarestate_listings_retry_alt,
    'imothek': fetch_imothek_listings_livewire_retry_alt,
    'heidtmann': lambda: fetch_zero_broker_detail_crawl(HEIDTMANN_URL),
    'muellerenglisch': lambda: fetch_detail_page_listings(MUELLER_ENGLISCH_URL, r'immobilien-details\.xhtml'),
    'neuesnest': lambda: fetch_zero_broker_detail_crawl(NEUESNEST_URL),
    'wurmseder': fetch_wurmseder_listings_retry_alt,
    'duerrenberger': lambda: fetch_zero_broker_detail_crawl(DUERRENBERGER_URL),
    'vonrodenhausen': fetch_vonrodenhausen_listings_retry_alt,
    'martinaschwarz': lambda: fetch_zero_broker_detail_crawl(MARTINA_SCHWARZ_URL),
    'friedlmaier': lambda: fetch_zero_broker_detail_crawl(FRIEDLMAIER_URL),
    'windhausen': lambda: fetch_detail_page_listings(WINDHAUSEN_URL, r'/detailseite/'),
    'riedl': fetch_riedl_listings_retry_alt,
    'herrmann': lambda: fetch_zero_broker_detail_crawl(HERRMANN_URL),
    'zippold': lambda: fetch_zero_broker_detail_crawl(ZIPPOLD_URL),
    'bunzco': lambda: fetch_zero_broker_detail_crawl(BUNZCO_URL),
    'drescher': lambda: fetch_zero_broker_detail_crawl(DRESCHER_URL),
    'sqmeter': lambda: fetch_zero_broker_detail_crawl(SQMETER_URL),
    'wegener': lambda: fetch_zero_broker_detail_crawl(WEGENER_URL),
    'wohnref': fetch_wohnref_listings_retry_alt,
    'schmidtmuenchen': lambda: fetch_zero_broker_detail_crawl(SCHMIDT_MUENCHEN_URL),
    'dalexis': fetch_dalexis_listings_retry_alt,
    'dawonia': lambda: fetch_source_specific_with_embedded_retry(DAWONIA_URL, r'(immobilie|objekt|expose|angebot|kauf|haus|wohnung|haeuser|wohnungen)', r'(kaufen|immobilien|angebote|angebot|objekt|immobilie|detail|expose)'),
    'vorstadtmakler': fetch_vorstadtmakler_listings_retry_alt,
    'andreasschmid': lambda: fetch_zero_broker_detail_crawl(ANDREAS_SCHMID_URL),
    'ausdemhaeuschen': lambda: fetch_zero_broker_detail_crawl(AUSDEMHAEUSCHEN_URL),
    'cki': fetch_cki_listings_retry_alt,
    'windisch': lambda: fetch_zero_broker_detail_crawl(WINDISCH_URL),
    'im7': lambda: fetch_zero_broker_detail_crawl(IM7_URL),
    'seimmobilien': lambda: fetch_zero_broker_detail_crawl(SE_IMMOBILIEN_HAEUSER_URL),
    'wandl': lambda: fetch_zero_broker_detail_crawl(WANDL_URL),
    'hoser': fetch_hoser_listings_retry_alt,
    'lebenstraum': fetch_lebenstraum_listings,
    'maurer': lambda: fetch_zero_broker_detail_crawl(MAURER_URL),
    'bechler': fetch_bechler_listings_retry_alt,
    'wesoly': fetch_wesoly_listings_retry_alt,
    'stierling': fetch_stierling_listings,
    'fairhomes': fetch_fairhomes_listings,
    'chalet': lambda: fetch_zero_broker_detail_crawl(CHALET_URL),
    'kraftziller': lambda: fetch_zero_broker_detail_crawl(KRAFT_ZILLER_URL),
    'wolf': lambda: fetch_zero_broker_detail_crawl(WOLF_HAUS_URL),
    'joseffrei': fetch_joseffrei_listings,
    'luenendonk': lambda: fetch_zero_broker_detail_crawl(LUENENDONK_URL),
    'harinali': lambda: fetch_zero_broker_detail_crawl(HARINALI_URL),
    'ramonaneckar': fetch_ramonaneckar_listings,
    'sriimmo': fetch_sriimmo_listings_retry_alt,
    'reischl': fetch_reischl_listings_retry_alt,
    'gattinger': fetch_gattinger_listings_retry_alt,
}


def iter_retry_fetchers(key: str):
    entry = ZERO_RESULT_RETRY_FETCHERS.get(key)
    if not entry:
        return []
    if callable(entry):
        return [entry]
    if isinstance(entry, (list, tuple)):
        return [fn for fn in entry if callable(fn)]
    return []


def fetch_broker_rows_with_retry(key: str, fetcher):
    try:
        normalized = apply_listing_rules(fetcher())
    except Exception:
        normalized = []

    if normalized:
        return normalized

    for retry_fetcher in iter_retry_fetchers(key):
        try:
            retry_rows = apply_listing_rules(retry_fetcher())
        except Exception:
            continue
        if retry_rows:
            return retry_rows

    return []


def fetch_broker_rows_with_status(key: str, fetcher):
    errors = []
    try:
        normalized = apply_listing_rules(fetcher())
    except Exception as error:
        normalized = []
        errors.append(error)

    if normalized:
        return normalized, None

    for retry_fetcher in iter_retry_fetchers(key):
        try:
            retry_rows = apply_listing_rules(retry_fetcher())
        except Exception as error:
            errors.append(error)
            continue
        if retry_rows:
            return retry_rows, None

    if errors:
        error = errors[-1]
        return [], f'{type(error).__name__}: {error}'
    return [], None


def fetch_listings(force_refresh=False):
    global LISTINGS_CACHE, LISTINGS_CACHE_TIME, LISTINGS_CACHE_UPDATED_AT
    now = time.time()
    if not force_refresh and LISTINGS_CACHE is not None and now - LISTINGS_CACHE_TIME < CACHE_TTL_SECONDS:
        return LISTINGS_CACHE

    if not force_refresh:
        try:
            persisted_listings = read_listings_blob()
        except Exception as error:
            LOGGER.warning(
                'Could not read listings blob %s: %s',
                LISTINGS_BLOB_NAME,
                format_blob_error(error),
            )
            persisted_listings = None
        if persisted_listings is not None:
            persisted_listings, generated_at = persisted_listings
            LISTINGS_CACHE = persisted_listings
            LISTINGS_CACHE_TIME = now
            LISTINGS_CACHE_UPDATED_AT = generated_at
            return LISTINGS_CACHE

    previous_listings = LISTINGS_CACHE or {}
    if not previous_listings:
        try:
            persisted = read_listings_blob(include_stale=True)
        except Exception:
            persisted = None
        if persisted is not None:
            previous_listings = persisted[0] or {}

    listings = {}
    broker_success = {}
    max_workers = min(8, len(BROKER_SOURCES)) or 1
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {
        executor.submit(lambda key=key, fetcher=fetcher: fetch_broker_rows_with_status(key, fetcher)): key
        for key, fetcher in BROKER_SOURCES
    }
    try:
        try:
            completed_futures = as_completed(futures, timeout=LISTINGS_FETCH_TIMEOUT_SECONDS)
        except TypeError:
            # Test doubles may not accept timeout kwarg.
            completed_futures = as_completed(futures)

        for future in completed_futures:
            key = futures[future]
            try:
                rows, error_message = future.result()
                listings[key] = rows
                broker_success[key] = not error_message and key not in BLOCKED_BROKER_REASONS
            except Exception:
                listings[key] = []
                broker_success[key] = False
    except FuturesTimeoutError:
        pass
    finally:
        shutdown = getattr(executor, 'shutdown', None)
        if shutdown:
            shutdown(wait=False, cancel_futures=True)

    # Ensure API returns even when some brokers time out.
    for key, _fetcher in BROKER_SOURCES:
        listings.setdefault(key, [])

    listings = enrich_listing_history(previous_listings, listings, broker_success)

    LISTINGS_CACHE = listings
    LISTINGS_CACHE_TIME = now
    LISTINGS_CACHE_UPDATED_AT = time.time()
    if listings:
        try:
            write_listings_blob(listings, LISTINGS_CACHE_UPDATED_AT)
        except Exception as error:
            LOGGER.error(
                'Could not write listings blob %s: %s',
                LISTINGS_BLOB_NAME,
                format_blob_error(error),
            )
            if force_refresh:
                raise
    return LISTINGS_CACHE


def empty_listings():
    return {key: [] for key, _fetcher in BROKER_SOURCES}


def refresh_status_payload(include_listings=False):
    with REFRESH_STATE_LOCK:
        payload = {
            'active': REFRESH_STATE['active'],
            'started_at': REFRESH_STATE['started_at'],
            'updated_at': REFRESH_STATE['updated_at'] or LISTINGS_CACHE_UPDATED_AT,
            'brokers': {
                key: dict(status)
                for key, status in REFRESH_STATE['brokers'].items()
            },
            'error': REFRESH_STATE['error'],
        }
        if include_listings:
            payload['listings'] = REFRESH_STATE['listings']
        return payload


def run_async_refresh():
    global LISTINGS_CACHE, LISTINGS_CACHE_TIME, LISTINGS_CACHE_UPDATED_AT

    listings = {}
    previous_listings = LISTINGS_CACHE or {}
    if not previous_listings:
        try:
            persisted = read_listings_blob(include_stale=True)
        except Exception:
            persisted = None
        if persisted is not None:
            previous_listings = persisted[0] or {}
    broker_success = {}
    executor = ThreadPoolExecutor(max_workers=min(8, len(BROKER_SOURCES)) or 1)
    futures = {
        executor.submit(
            lambda key=key, fetcher=fetcher: fetch_broker_rows_with_status(key, fetcher)
        ): key
        for key, fetcher in BROKER_SOURCES
    }

    try:
        for future in as_completed(futures):
            key = futures[future]
            try:
                rows, error_message = future.result()
            except Exception as error:
                rows = []
                error_message = f'{type(error).__name__}: {error}'

            listings[key] = rows
            broker_success[key] = not error_message and key not in BLOCKED_BROKER_REASONS
            with REFRESH_STATE_LOCK:
                REFRESH_STATE['brokers'][key] = {
                    'status': 'error' if error_message else ('done' if rows else 'empty'),
                    'count': len(rows),
                    'error': error_message,
                }
    finally:
        executor.shutdown(wait=True)

    for key, _fetcher in BROKER_SOURCES:
        listings.setdefault(key, [])

    listings = enrich_listing_history(previous_listings, listings, broker_success)

    updated_at = time.time()
    blob_updated_at = LISTINGS_CACHE_UPDATED_AT
    storage_error = None
    if LISTINGS_BLOB_ENABLED and listings:
        try:
            write_listings_blob(listings, updated_at)
            blob_updated_at = updated_at
        except Exception as error:
            storage_error = format_blob_error(error)
            LOGGER.error('Could not write listings blob %s', storage_error)

    LISTINGS_CACHE = listings
    LISTINGS_CACHE_TIME = updated_at
    LISTINGS_CACHE_UPDATED_AT = blob_updated_at
    with REFRESH_STATE_LOCK:
        REFRESH_STATE.update({
            'active': False,
            'updated_at': blob_updated_at,
            'listings': listings,
            'error': storage_error,
        })


def start_async_refresh():
    with REFRESH_STATE_LOCK:
        if REFRESH_STATE['active']:
            return False
        REFRESH_STATE.update({
            'active': True,
            'started_at': time.time(),
            'updated_at': None,
            'brokers': {
                key: {'status': 'loading', 'count': 0, 'error': None}
                for key, _fetcher in BROKER_SOURCES
            },
            'listings': None,
            'error': None,
        })

    threading.Thread(target=run_async_refresh, daemon=True).start()
    return True


def get_current_listings():
    global LISTINGS_CACHE, LISTINGS_CACHE_TIME, LISTINGS_CACHE_UPDATED_AT
    now = time.time()
    if LISTINGS_CACHE is not None:
        return LISTINGS_CACHE

    try:
        persisted_listings = read_listings_blob()
    except Exception as error:
        LOGGER.warning(
            'Could not read listings blob %s: %s',
            LISTINGS_BLOB_NAME,
            format_blob_error(error),
        )
        persisted_listings = None
    if persisted_listings is not None:
        persisted_listings, generated_at = persisted_listings
        LISTINGS_CACHE = persisted_listings
        LISTINGS_CACHE_TIME = now
        LISTINGS_CACHE_UPDATED_AT = generated_at
        return LISTINGS_CACHE
    return empty_listings()


def request_origin_allowed(handler):
    origin = handler.headers.get('Origin')
    if not origin:
        return True
    parsed_origin = urlparse(origin)
    request_host = handler.headers.get('Host', '')
    return parsed_origin.scheme in {'http', 'https'} and parsed_origin.netloc == request_host


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/listings/refresh-status':
            payload = refresh_status_payload(include_listings=not REFRESH_STATE['active'])
            body = json.dumps(payload).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path.startswith('/api/listings'):
            listings = get_current_listings()
            with REFRESH_STATE_LOCK:
                refresh_active = REFRESH_STATE['active']
            if LISTINGS_CACHE is None and not refresh_active:
                start_async_refresh()
            body = json.dumps(listings).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            if LISTINGS_CACHE_UPDATED_AT is not None:
                self.send_header('X-Listings-Updated-At', str(LISTINGS_CACHE_UPDATED_AT))
            with REFRESH_STATE_LOCK:
                self.send_header('X-Listings-Refresh-Active', str(REFRESH_STATE['active']).lower())
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
        .numeric-filters { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
        .numeric-filter { width: 100%; border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px 12px; font-size: 14px; background: white; box-sizing: border-box; }
        .sort-select { border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px 12px; font-size: 14px; background: white; }
        .tabs { display: flex; gap: 10px; margin-bottom: 4px; overflow-x: auto; padding-bottom: 4px; scrollbar-width: thin; }
        .tab { border: 0; padding: 10px 16px; border-radius: 999px; background: #e2e8f0; cursor: pointer; font-weight: 600; white-space: nowrap; flex: 0 0 auto; }
        .refresh-actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
        .refresh-button { border: 1px solid #94a3b8; border-radius: 8px; padding: 10px 14px; background: white; color: #0f172a; cursor: pointer; font-weight: 600; }
        .refresh-button:disabled { cursor: wait; opacity: 0.6; }
        .result-summary { color: #64748b; font-size: 14px; margin: 0 0 12px; }
        @media (max-width: 700px) { .numeric-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
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
            <div class="numeric-filters">
                <input id="min-price" class="numeric-filter" type="number" min="0" step="1000" placeholder="Preis von (€)">
                <input id="max-price" class="numeric-filter" type="number" min="0" step="1000" placeholder="Preis bis (€)">
                <input id="min-area" class="numeric-filter" type="number" min="0" step="1" placeholder="Wohnfläche von (m²)">
                <input id="max-area" class="numeric-filter" type="number" min="0" step="1" placeholder="Wohnfläche bis (m²)">
            </div>
            <label class="meta" for="listing-sort">Sortierung
                <select id="listing-sort" class="sort-select">
                    <option value="first-newest">Erst gefunden: neu nach alt</option>
                    <option value="first-oldest">Erst gefunden: alt nach neu</option>
                </select>
            </label>
            <div class="refresh-actions">
                <button id="refresh-all" class="refresh-button" type="button">Alle Angebote aktualisieren</button>
                <p id="last-updated" class="result-summary"></p>
            </div>
            <div id="tabs" class="tabs"></div>
        </div>
        <div id="listings" class="loading">
            <span class="spinner"></span>
            <span>Lade Inserate…</span>
        </div>
  </main>
  <script>
        const brokerLabels = __BROKER_LABELS__;
      const blockedBrokerReasons = __BLOCKED_BROKER_REASONS__;
        const tabsRoot = document.getElementById('tabs');
        const brokerSearchInput = document.getElementById('broker-search');
        const listingSearchInput = document.getElementById('listing-search');
        const minPriceInput = document.getElementById('min-price');
        const maxPriceInput = document.getElementById('max-price');
        const minAreaInput = document.getElementById('min-area');
        const maxAreaInput = document.getElementById('max-area');
        const listingSortInput = document.getElementById('listing-sort');
        const refreshAllButton = document.getElementById('refresh-all');
        const lastUpdated = document.getElementById('last-updated');
        const root = document.getElementById('listings');
        const allListingsTab = '__all__';
        let activeTab = allListingsTab;
        let cachedData = null;
        let cacheTimestamp = 0;
        const cacheTtlMs = 5 * 60 * 1000;
        let brokerQuery = '';
        let listingQuery = '';
        let listingSort = 'first-newest';
        let brokerStatuses = {};
        let refreshPollTimer = null;

        function parsePriceValue(value) {
            const text = String(value || '').replace(/[^0-9,.-]/g, '');
            if (!text) {
                return null;
            }
            const normalized = text.includes(',')
                ? text.replace(/\\./g, '').replace(',', '.')
                : text.replace(/\\./g, '');
            const number = Number.parseFloat(normalized);
            return Number.isFinite(number) ? number : null;
        }

        function parseAreaValue(value) {
            const text = String(value || '').replace(/[^0-9,.-]/g, '');
            if (!text) {
                return null;
            }
            const normalized = text.includes(',')
                ? text.replace(/\\./g, '').replace(',', '.')
                : text;
            const number = Number.parseFloat(normalized);
            return Number.isFinite(number) ? number : null;
        }

        function readFilterValue(input) {
            const value = Number.parseFloat(input.value);
            return Number.isFinite(value) ? value : null;
        }

        function flattenListings(data) {
            return Object.entries(data).flatMap(([brokerKey, listings]) =>
                (listings || []).map(item => ({ ...item, brokerKey }))
            );
        }

        function setLoading() {
            root.className = 'loading';
            root.replaceChildren();
            const spinner = document.createElement('span');
            spinner.className = 'spinner';
            const label = document.createElement('span');
            label.textContent = 'Lade Inserate...';
            root.append(spinner, label);
        }

        function renderLastUpdated(timestamp) {
            const value = Number(timestamp);
            if (!Number.isFinite(value)) {
                lastUpdated.textContent = '';
                return;
            }
            lastUpdated.textContent = `Letzte Aktualisierung: ${new Date(value * 1000).toLocaleString('de-DE')}`;
        }

        function formatLabel(key) {
            return brokerLabels[key] || key.replace(/_/g, ' ').replace(/\\b\\w/g, char => char.toUpperCase());
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
            const minPrice = readFilterValue(minPriceInput);
            const maxPrice = readFilterValue(maxPriceInput);
            const minArea = readFilterValue(minAreaInput);
            const maxArea = readFilterValue(maxAreaInput);
            const filtered = listings.filter(item => {
                const haystack = [item.title, item.location, item.price, item.area_sqm].join(' ').toLowerCase();
                if (query && !haystack.includes(query)) {
                    return false;
                }
                const price = parsePriceValue(item.price);
                const area = parseAreaValue(item.area_sqm);
                if (minPrice !== null && (price === null || price < minPrice)) {
                    return false;
                }
                if (maxPrice !== null && (price === null || price > maxPrice)) {
                    return false;
                }
                if (minArea !== null && (area === null || area < minArea)) {
                    return false;
                }
                if (maxArea !== null && (area === null || area > maxArea)) {
                    return false;
                }
                return true;
            });
            return filtered.sort((left, right) => {
                const leftDate = Number(left.first_seen_at) || 0;
                const rightDate = Number(right.first_seen_at) || 0;
                return listingSort === 'first-oldest' ? leftDate - rightDate : rightDate - leftDate;
            });
        }

        function renderListings(listings) {
            if (!listings.length) {
                root.className = '';
                root.textContent = 'Keine Inserate gefunden.';
                return;
            }
            root.className = '';
            root.replaceChildren();
            const summary = document.createElement('p');
            summary.className = 'result-summary';
            summary.textContent = `${listings.length} Angebote`;
            root.append(summary);
            listings.forEach(item => {
                const card = document.createElement('article');
                card.className = 'card';

                const title = document.createElement('div');
                title.className = 'title';
                title.textContent = item.title || '';

                const price = document.createElement('div');
                price.className = 'meta';
                price.textContent = 'Preis: ';
                if (item.old_price && item.price && item.old_price !== item.price) {
                    const oldPrice = document.createElement('s');
                    oldPrice.textContent = item.old_price;
                    const newPrice = document.createElement('strong');
                    newPrice.textContent = ` ${item.price}`;
                    price.append(oldPrice, newPrice);
                } else {
                    price.append(document.createTextNode(item.price || 'nicht verfügbar'));
                }

                const area = document.createElement('div');
                area.className = 'meta';
                area.textContent = `Wohnfläche: ${item.area_sqm ? `${item.area_sqm} m²` : 'nicht verfügbar'}`;

                const location = document.createElement('div');
                location.className = 'meta';
                location.textContent = `Ort: ${item.location || 'nicht verfügbar'}`;

                const broker = document.createElement('div');
                broker.className = 'meta';
                if (item.brokerKey) {
                    broker.textContent = `Makler: ${formatLabel(item.brokerKey)}`;
                }

                const age = document.createElement('div');
                age.className = 'meta';
                const ageDays = Number(item.age_days);
                age.textContent = Number.isFinite(ageDays)
                    ? `Zum ersten Mal gefunden vor ${ageDays} ${ageDays === 1 ? 'Tag' : 'Tagen'}`
                    : 'Erstfunddatum nicht verfügbar';

                const note = document.createElement('div');
                note.className = 'meta';
                if (item.note) {
                    note.textContent = `Notiz: ${item.note}`;
                    note.style.color = '#b91c1c';
                    note.style.fontWeight = '600';
                }

                const linkRow = document.createElement('div');
                linkRow.className = 'meta';
                const link = document.createElement('a');
                try {
                    const parsedLink = new URL(item.link, window.location.origin);
                    if (parsedLink.protocol === 'https:') {
                        link.href = parsedLink.href;
                        link.target = '_blank';
                        link.rel = 'noopener noreferrer';
                        link.textContent = 'Zum Exposé';
                        linkRow.append(link);
                    }
                } catch (error) {
                }

                card.append(title, price, area, location, broker, age, note, linkRow);
                root.append(card);
            });
        }

        function renderActiveListings() {
            if (!cachedData) {
                return;
            }
            const blockedReason = blockedBrokerReasons[activeTab];
            if (blockedReason) {
                root.className = '';
                root.replaceChildren();
                const status = document.createElement('p');
                const label = document.createElement('strong');
                label.textContent = 'Status: ';
                status.append(label, document.createTextNode(blockedReason));
                root.append(status);
                return;
            }
            const brokerStatus = brokerStatuses[activeTab];
            if (brokerStatus && brokerStatus.status === 'loading') {
                root.className = '';
                root.textContent = `Lade Angebote von ${formatLabel(activeTab)}...`;
                return;
            }
            if (brokerStatus && brokerStatus.status === 'error') {
                root.className = '';
                root.textContent = `Fehler beim Laden von ${formatLabel(activeTab)}: ${brokerStatus.error || 'Unbekannter Fehler'}`;
                return;
            }
            const sourceListings = activeTab === allListingsTab
                ? flattenListings(cachedData)
                : (cachedData[activeTab] || []);
            const list = filterListings(sourceListings);
            root.dataset.resultCount = String(list.length);
            renderListings(list);
        }

        async function readJsonResponse(res) {
            const text = await res.text();
            if (!text.trim()) {
                throw new Error(`Serverantwort ist leer (HTTP ${res.status}).`);
            }
            try {
                return JSON.parse(text);
            } catch (error) {
                throw new Error(`Serverantwort ist kein gültiges JSON (HTTP ${res.status}).`);
            }
        }

        function renderTabs(data) {
            const keys = filterBrokerKeys(data);
            if (!keys.length && activeTab !== allListingsTab) {
                tabsRoot.textContent = 'Keine Makler gefunden.';
                return;
            }

            if (activeTab !== allListingsTab && !keys.includes(activeTab)) {
                activeTab = keys[0];
            }

            tabsRoot.replaceChildren();
            const allButton = document.createElement('button');
            allButton.className = `tab${activeTab === allListingsTab ? ' active' : ''}`;
            allButton.dataset.tab = allListingsTab;
            allButton.textContent = `Alle Angebote (${flattenListings(data).length})`;
            allButton.addEventListener('click', () => {
                tabsRoot.querySelectorAll('.tab').forEach(btn => btn.classList.remove('active'));
                allButton.classList.add('active');
                activeTab = allListingsTab;
                renderActiveListings();
            });
            tabsRoot.append(allButton);
            keys.forEach(key => {
                const count = (data[key] || []).length;
                const button = document.createElement('button');
                button.className = `tab${key === activeTab ? ' active' : ''}`;
                button.dataset.tab = key;
                const blockedReason = blockedBrokerReasons[key];
                const brokerStatus = brokerStatuses[key];
                let suffix = ` (${count})`;
                if (blockedReason) {
                    suffix = ` (${blockedReason})`;
                } else if (brokerStatus && brokerStatus.status === 'loading') {
                    suffix = ' (Lädt...)';
                } else if (brokerStatus && brokerStatus.status === 'error') {
                    suffix = ' (Fehler)';
                }
                button.textContent = `${formatLabel(key)}${suffix}`;
                button.addEventListener('click', () => {
                    tabsRoot.querySelectorAll('.tab').forEach(btn => btn.classList.remove('active'));
                    button.classList.add('active');
                    activeTab = button.dataset.tab;
                    renderActiveListings();
                });
                tabsRoot.append(button);
            });
        }

        function applyRefreshStatus(status) {
            brokerStatuses = status.brokers || {};
            if (status.listings) {
                cachedData = status.listings;
                cacheTimestamp = Date.now();
                renderLastUpdated(status.updated_at);
            }
            renderTabs(cachedData || {});
            renderActiveListings();
        }

        async function pollRefreshStatus() {
            try {
                const res = await fetch('/api/listings/refresh-status');
                const status = await readJsonResponse(res);
                applyRefreshStatus(status);
                refreshAllButton.disabled = status.active;
                if (status.active) {
                    refreshPollTimer = setTimeout(pollRefreshStatus, 1000);
                } else {
                    refreshPollTimer = null;
                }
            } catch (err) {
                refreshPollTimer = setTimeout(pollRefreshStatus, 2000);
            }
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
                const data = await readJsonResponse(res);
                if (!res.ok) {
                    throw new Error(data.error || `Laden fehlgeschlagen (HTTP ${res.status}).`);
                }
                cachedData = data;
                cacheTimestamp = now;
                renderLastUpdated(res.headers.get('X-Listings-Updated-At'));
                renderTabs(cachedData);
                renderActiveListings();
                if (res.headers.get('X-Listings-Refresh-Active') === 'true') {
                    refreshAllButton.disabled = true;
                    pollRefreshStatus();
                }
            } catch (err) {
                root.className = '';
                root.textContent = 'Die Inserate konnten nicht geladen werden.';
            }
        }

        async function refreshListings(endpoint) {
            refreshAllButton.disabled = true;
            setLoading();
            try {
                const res = await fetch(endpoint, { method: 'POST' });
                const result = await readJsonResponse(res);
                if (!res.ok || !result.ok) {
                    throw new Error(result.error || 'Aktualisierung fehlgeschlagen');
                }
                applyRefreshStatus(result.status || {});
                pollRefreshStatus();
            } catch (err) {
                root.className = '';
                root.textContent = err.message || 'Aktualisierung fehlgeschlagen.';
            } finally {
                refreshAllButton.disabled = false;
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

        [minPriceInput, maxPriceInput, minAreaInput, maxAreaInput].forEach(input => {
            input.addEventListener('input', () => renderActiveListings());
        });

        listingSortInput.addEventListener('change', event => {
            listingSort = event.target.value;
            renderActiveListings();
        });

        refreshAllButton.addEventListener('click', () => {
            refreshListings('/api/listings/refresh-all');
        });

        loadListings(true);
    </script>

</body>
        </html>"""
        body = (
            html
            .replace('__BROKER_LABELS__', json.dumps(BROKER_LABELS))
            .replace('__BLOCKED_BROKER_REASONS__', json.dumps(BLOCKED_BROKER_REASONS))
        ).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global LAST_REFRESH_REQUEST_TIME
        if self.path == '/internal/refresh':
            expected_token = INTERNAL_REFRESH_TOKEN
            authorization = self.headers.get('Authorization', '')
            if not expected_token or authorization != f'Bearer {expected_token}':
                body = json.dumps({'ok': False, 'error': 'Unauthorized'}).encode('utf-8')
                self.send_response(401)
            else:
                started = start_async_refresh()
                body = json.dumps({
                    'ok': True,
                    'active': True,
                    'started': started,
                    'status': refresh_status_payload(),
                }).encode('utf-8')
                self.send_response(202)
        elif self.path == '/api/listings/refresh-all':
            if not request_origin_allowed(self):
                body = json.dumps({'ok': False, 'error': 'Origin not allowed'}).encode('utf-8')
                self.send_response(403)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            now = time.time()
            if now - LAST_REFRESH_REQUEST_TIME < REFRESH_COOLDOWN_SECONDS:
                body = json.dumps({'ok': False, 'error': 'Refresh is temporarily unavailable'}).encode('utf-8')
                self.send_response(429)
                self.send_header('Retry-After', str(REFRESH_COOLDOWN_SECONDS))
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            LAST_REFRESH_REQUEST_TIME = now
            start_async_refresh()
            body = json.dumps({
                'ok': True,
                'active': True,
                'status': refresh_status_payload(),
            }).encode('utf-8')
            self.send_response(202)
        else:
            body = json.dumps({'ok': False, 'error': 'Unknown endpoint'}).encode('utf-8')
            self.send_response(404)

        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == '__main__':
    host = '0.0.0.0'
    port = int(os.environ.get('PORT', '8000'))
    server = HTTPServer((host, port), Handler)
    print(f'Server running on http://{host}:{port}')
    server.serve_forever()
