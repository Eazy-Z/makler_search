import app, re, sys

def probe(name, url, extra_patterns=None):
    try:
        html = app.fetch_html(url)
    except Exception as exc:
        print(f'{name}: ERROR {exc}')
        return
    hrefs = re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I)
    iframes = re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I)
    price_sigs = re.findall(r'(?:EUR|€|Kaufpreis|Preis auf Anfrage)', html, re.I)
    wohnfl = re.findall(r'Wohnfl', html, re.I)
    print(f'--- {name} ({len(html)} bytes, {len(hrefs)} hrefs, {len(price_sigs)} price, {len(wohnfl)} Wohnfl) ---')
    if iframes:
        print(f'  iframes: {iframes[:3]}')
    # sample hrefs that look like listings
    listing_hrefs = [h for h in hrefs if re.search(r'(immobil|expose|objekt|kauf|haus|wohnung|angebot|object)', h, re.I)]
    print(f'  listing hrefs ({len(listing_hrefs)}): {listing_hrefs[:5]}')
    # sample price context
    for pm in re.finditer(r'(?:Kaufpreis|€).{0,80}', html, re.I):
        print(f'  price ctx: {repr(pm.group()[:120])}')
        break
    if extra_patterns:
        for label, pat in extra_patterns:
            m = re.findall(pat, html, re.I | re.S)
            print(f'  {label}: {len(m)} -> {str(m[:2])[:200]}')

brokers = [
    ('mar', app.MAR_URL),
    ('dahler', app.DAHLER_URL),
    ('krimbacher', app.KRIMBACHER_URL),
    ('egger', app.EGGER_URL),
    ('neuesnest', app.NEUESNEST_URL),
    ('dalexis', app.DALEXIS_URL),
    ('ausdemhaeuschen', app.AUSDEMHAEUSCHEN_URL),
    ('feuerlein', app.FEUERLEIN_URL),
    ('lebenstraum', app.LEBENSTRAUM_URL),
    ('reischl', app.REISCHL_URL),
    ('gattinger', app.GATTINGER_URL),
]
target = sys.argv[1] if len(sys.argv) > 1 else None
for name, url in brokers:
    if target and name != target:
        continue
    probe(name, url)
    print()
