import json
import urllib.request
import sys
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def test_format_price_handles_schneider_style_values():
    assert app.format_price('11.500.000,00 €') == '11.500.000 €'
    assert app.format_price('2.495.000 €') == '2.495.000 €'


def test_new_sources_return_data():
    data = app.fetch_listings()
    assert 'firstplace' in data
    assert 'bartsch' in data
    assert 'schneider' in data
    assert data['firstplace']
    assert data['bartsch']
    assert data['schneider']

    for key in ['firstplace', 'bartsch', 'schneider']:
        first = data[key][0]
        assert first['title']
        assert first['price']
        assert first['link']


def test_new_broker_labels_are_registered():
    for key in ['graf', 'riedel', 'engel', 'weichselgartner']:
        assert key in app.BROKER_LABELS
        assert app.BROKER_LABELS[key]
    assert 'aigner' in app.IGNORED_BROKERS


def test_new_source_parsers_handle_source_specific_markup():
    graf_overview = '''<div class="col-12 col-md-6 col-lg-4 object-item"><a href="/expose/1250-2/charmante-wohnung-in-polzow/" class="card border-0 h-100 card-hover"><div class="my-3"><div class="fw-bold">Charmante Wohnung in Polzow</div><div class="">Etagenwohnung <br> Polzow</div><div class="mb-3 fs-7 fw-light mt-3">60,00&nbsp;m² – 390.000,00&nbsp;&euro;</div></div></a></div>'''
    graf_page = '''<!doctype html><html><head><meta property="og:title" content="Charmante Wohnung in Polzow" /></head><body><h3 class="el-title">Kaufpreis:</h3><div class="el-content uk-panel uk-h3">390.000€</div><h3 class="el-title">Wohnfläche:</h3><div class="el-content uk-panel uk-h3">60m²</div><p>Zur Vermietung steht eine Wohnung gelegen in 17309 Polzow.</p></body></html>'''
    riedel_page = '''<ul><li class="listEntry listEntryObject-immoObject listEntryObject-immoObject_var"><div class="listEntryInner clickable" data-url="/objekte/test.php"><div class="listEntryContentInner"><div class="listEntryLocationShort">Voralpenland – Isarwinkel</div><h3 class="listEntryTitle"><a href="/objekte/test.php">Spektakuläres Anwesen: 46 Hektar Landsitz mit Reitanlage &amp; Schwimmhalle</a></h3><div class="listEntryObjektdaten">Grd. 462.057 m² - Wfl. ca. 1.783 m² - Kaufpreis auf Anfrage</div></div></div></li></ul>'''
    engel_page = '''<html><body><article data-testid="search-components_result-card_test"><p data-testid="search-components_result-card_location">Ludwigsvorstadt-Isarvorstadt, München, Bayern, Deutschland</p><h2 data-testid="search-components_result-card_headline">Moderne City-Wohnung direkt an der Theresienwiese</h2><p data-testid="search-components_result-card_price">595.000 €</p><ul><li><span data-testid="search-components_result-card_attribute_test-livingArea">~52 m² Wohnfläche</span></li></ul><a href="/de/de/exposes/test-id"></a></article></body></html>'''
    weichselgartner_page = '''<html><body><div class="property-container" id="1936"><div class="property-details col-sm-12 vertical"><div class="property-location">München - Denning</div><h3 class="property-title"><a href="https://www.weichselgartner-immo.de/immobilien/haus-einfamilienhaus-in-muenchen-kaufen-1936/">Exklusive Einfamilienhaus-Villa mit Pool und höchsten Sicherheitsstandards in bester Wohnlage</a></h3><div class="property-data"><div class="property-data-keyvalue">Angebots-Nr: 1936<br>Wohnfläche: ca. 323 m² | Grund: 704 m²<br>Baujahr: 2013<br>Kaufpreis: 4.900.000 €</div></div></div><div class="clearfix"></div></div></body></html>'''

    class FakeResponse:
        def __init__(self, text):
            self._payload = text.encode('utf-8')

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout=20):
        url = getattr(request, 'full_url', request)
        if 'www.grafimmo.de/angebote/' in url:
            return FakeResponse(graf_overview)
        if 'www.grafimmo.de/expose/1250-2/charmante-wohnung-in-polzow/' in url:
            return FakeResponse(graf_page)
        if 'riedel-immobilien.de/angebote/' in url:
            return FakeResponse(riedel_page)
        if 'engelvoelkers.com/de/de/immobilien/res/kaufen' in url:
            return FakeResponse(engel_page)
        if 'weichselgartner-immobilien.de/kaufen/haeuser/' in url:
            return FakeResponse(weichselgartner_page)
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app.urllib.request, 'urlopen', side_effect=fake_urlopen):
        graf = app.fetch_graf_listings()
        riedel = app.fetch_riedel_listings()
        engel = app.fetch_engel_listings()
        weichselgartner = app.fetch_weichselgartner_listings()

    assert graf and graf[0]['title'] == 'Charmante Wohnung in Polzow'
    assert graf[0]['price'] == '390.000 €'
    assert graf[0]['area_sqm'] == '60'
    assert graf[0]['location'] == 'Polzow'
    assert graf[0]['link'] == 'https://www.grafimmo.de/expose/1250-2/charmante-wohnung-in-polzow/'

    assert riedel and riedel[0]['title'].startswith('Spektakuläres Anwesen')
    assert riedel[0]['price'] == 'Preis auf Anfrage'
    assert riedel[0]['area_sqm'] == '1.783'
    assert riedel[0]['location'] == 'Voralpenland – Isarwinkel'

    assert engel and engel[0]['title'] == 'Moderne City-Wohnung direkt an der Theresienwiese'
    assert engel[0]['price'] == '595.000 €'
    assert engel[0]['area_sqm'] == '52'
    assert engel[0]['location'].startswith('Ludwigsvorstadt-Isarvorstadt')
    assert engel[0]['link'] == 'https://www.engelvoelkers.com/de/de/exposes/test-id'

    assert weichselgartner and weichselgartner[0]['title'].startswith('Exklusive Einfamilienhaus-Villa')
    assert weichselgartner[0]['price'] == '4.900.000 €'
    assert weichselgartner[0]['area_sqm'] == '323'
    assert weichselgartner[0]['location'] == 'München - Denning'
