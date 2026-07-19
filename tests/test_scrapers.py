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
    for key in ['graf', 'riedel', 'engel', 'weichselgartner', 'sopart', 'jalea', 'sedlmayr', 'kaiserreich', 'sis', 'ede', 'nikki', 'tsc', 'imothek', 'vr', 'stolze', 'realwert', 'eden', 'imliving', 'webau', 'mar', 'seeimmo', 'heidinger', 'funer', 'weiherer', 'mb', 'fischer', 'heimhuber', 'citigrund', 'georgi', 'akurat', 'hegerich', 'eder', 'gerschlauer', 'dahler', 'krimbacher', 'klatt', 'ft', 'tesch', 'ritter', 'hirschmann', 'rohrer', 'mrlodge']:
        assert key in app.BROKER_LABELS
        assert app.BROKER_LABELS[key]
    assert 'aigner' in app.IGNORED_BROKERS
    assert any(key == 'sopart' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'jalea' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'sedlmayr' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'kaiserreich' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'sis' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'ede' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'nikki' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'tsc' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'imothek' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'vr' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'stolze' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'realwert' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'eden' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'imliving' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'webau' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'mar' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'seeimmo' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'heidinger' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'funer' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'weiherer' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'mb' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'fischer' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'heimhuber' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'citigrund' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'georgi' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'akurat' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'hegerich' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'eder' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'gerschlauer' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'dahler' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'krimbacher' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'klatt' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'ft' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'tesch' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'ritter' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'hirschmann' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'rohrer' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'mrlodge' for key, _ in app.BROKER_SOURCES)


def test_missing_brokers_are_registered():
    missing_keys = [
        'teambim', 'sozius', 'andreasschmid', 'muenchnerimmobilien', 'ausdemhaeuschen', 'hallinger',
        'cki', 'windisch', 'im7', 'seimmobilien', 'happyimmo', 'wandl', 'emslander', 'hoser',
        'feuerlein', 'lebenstraum', 'gschwender', 'maurer', 'pscheidt', 'bechler', 'isarestate',
        'wesoly', 'westend', 'stierling', 'finestep', 'fairhomes', 'chalet', 'kraftziller', 'wolf',
        'bayergrund', 'immops', 'rsi', 'siemax', 'joseffrei', 'luenendonk', 'harinali', 'aufrecht',
        'ramonaneckar', 'mytropper', 'sriimmo', 'zg', 'zardini', 'reischl', 'gattinger',
    ]
    source_keys = {key for key, _ in app.BROKER_SOURCES}
    for key in missing_keys:
        assert key in app.BROKER_LABELS
        assert app.BROKER_LABELS[key]
        assert key in source_keys


def test_new_source_parsers_handle_source_specific_markup():
    graf_overview = '''<div class="col-12 col-md-6 col-lg-4 object-item"><a href="/expose/1250-2/charmante-wohnung-in-polzow/" class="card border-0 h-100 card-hover"><div class="my-3"><div class="fw-bold">Charmante Wohnung in Polzow</div><div class="">Etagenwohnung <br> Polzow</div><div class="mb-3 fs-7 fw-light mt-3">60,00&nbsp;m² – 390.000,00&nbsp;&euro;</div></div></a></div>'''
    graf_page = '''<!doctype html><html><head><meta property="og:title" content="Charmante Wohnung in Polzow" /></head><body><h3 class="el-title">Kaufpreis:</h3><div class="el-content uk-panel uk-h3">390.000€</div><h3 class="el-title">Wohnfläche:</h3><div class="el-content uk-panel uk-h3">60m²</div><p>Zur Vermietung steht eine Wohnung gelegen in 17309 Polzow.</p></body></html>'''
    riedel_page = '''<ul><li class="listEntry listEntryObject-immoObject listEntryObject-immoObject_var"><div class="listEntryInner clickable" data-url="/objekte/test.php"><div class="listEntryContentInner"><div class="listEntryLocationShort">Voralpenland – Isarwinkel</div><h3 class="listEntryTitle"><a href="/objekte/test.php">Spektakuläres Anwesen: 46 Hektar Landsitz mit Reitanlage &amp; Schwimmhalle</a></h3><div class="listEntryObjektdaten">Grd. 462.057 m² - Wfl. ca. 1.783 m² - Kaufpreis auf Anfrage</div></div></div></li></ul>'''
    engel_page = '''<html><body><article data-testid="search-components_result-card_test"><p data-testid="search-components_result-card_location">Ludwigsvorstadt-Isarvorstadt, München, Bayern, Deutschland</p><h2 data-testid="search-components_result-card_headline">Moderne City-Wohnung direkt an der Theresienwiese</h2><p data-testid="search-components_result-card_price">595.000 €</p><ul><li><span data-testid="search-components_result-card_attribute_test-livingArea">~52 m² Wohnfläche</span></li></ul><a href="/de/de/exposes/test-id"></a></article></body></html>'''
    weichselgartner_page = '''<html><body><div class="property-container" id="1936"><div class="property-details col-sm-12 vertical"><div class="property-location">München - Denning</div><h3 class="property-title"><a href="https://www.weichselgartner-immo.de/immobilien/haus-einfamilienhaus-in-muenchen-kaufen-1936/">Exklusive Einfamilienhaus-Villa mit Pool und höchsten Sicherheitsstandards in bester Wohnlage</a></h3><div class="property-data"><div class="property-data-keyvalue">Angebots-Nr: 1936<br>Wohnfläche: ca. 323 m² | Grund: 704 m²<br>Baujahr: 2013<br>Kaufpreis: 4.900.000 €</div></div></div><div class="clearfix"></div></div></body></html>'''
    sopart_page = '''<html><body><div class="infiniteresults"><div class="result"><h3>Charmantes Reihenhaus mit Südgarten</h3><a href="index.php4?cmd=expose&amp;id=12345">Zum Exposé</a><div>Ort: Germering</div><div>Wohnfläche: ca. 132,50 m²</div><div>Kaufpreis: 1.120.000 €</div></div></div></body></html>'''
    jalea_overview = """<html><body><article class=\"frymo-listing-item post-id-4059\"><div class=\"frymo-listing-content\"><h3 class='frymo-listing-title'><a href='https://jalea-immobilien.de/immobilie/wohnen-an-den-amperauen/' aria-label='Wohnen an den Amperauen'>Wohnen an den Amperauen</a></h3><div class=\"frymo-listing-location\"><i class=\"frymo-icon frymo-icon-location-2\"></i>Olching / Geiselbullach</div></div></article></body></html>"""
    jalea_detail = """<html><body><div class=\"frymo-data-item content-inline\" data-key=\"kaufpreis\"><div class=\"frymo-data-item-label\">Kaufpreis</div><div class=\"frymo-data-item-value\">2.000.000 €</div></div><div class=\"frymo-data-item content-inline\" data-key=\"grundstuecksflaeche\"><div class=\"frymo-data-item-label\">Grundstücksfläche</div><div class=\"frymo-data-item-value\">ca. 1.456,00 m²</div></div><div class=\"frymo-data-item content-inline\" data-key=\"wohnflaeche\"><div class=\"frymo-data-item-label\">Wohnfläche</div><div class=\"frymo-data-item-value\">ca. 234,50 m²</div></div></body></html>"""
    sedlmayr_overview = """<html><body><article class=\"frymo-listing-item post-id-987\"><div class=\"frymo-listing-content\"><h3 class='frymo-listing-title'><a href='https://www.sedlmayr-immo.de/immobilie/charmantes-landhaus/' aria-label='Charmantes Landhaus'>Charmantes Landhaus</a></h3><div class=\"frymo-listing-location\"><i class=\"frymo-icon frymo-icon-location-2\"></i>Andechs</div></div></article></body></html>"""
    sedlmayr_detail = """<html><body><div class=\"frymo-data-item content-inline\" data-key=\"kaufpreis\"><div class=\"frymo-data-item-label\">Kaufpreis</div><div class=\"frymo-data-item-value\">1.350.000 €</div></div><div class=\"frymo-data-item content-inline\" data-key=\"wohnflaeche\"><div class=\"frymo-data-item-label\">Wohnfläche</div><div class=\"frymo-data-item-value\">ca. 186,40 m²</div></div></body></html>"""
    kaiserreich_overview = """<html><body><article class='slide-entry flex_column slide-entry-overview'><div class='slide-content'><header class="entry-content-header"><h2 class='slide-entry-title entry-title'><a href='https://immo-kaiserreich.de/immobilienangebote/moderne-villa-am-see/' title='Moderne Villa am See'>Moderne Villa am See</a></h2></header><div class='avia-icon-list-container'><div class='av_iconlist_title iconlist_title_small'>Starnberg</div></div></div></article></body></html>"""
    kaiserreich_detail = """<html><body><div class='ak-tablerow'><div class='ak-tablecell'>Ort / Region:</div><div class='ak-tablecell'>Starnberg</div></div><div class='ak-tablerow'><div class='ak-tablecell'>Wohnfläche:</div><div class='ak-tablecell'>ca. 188 m²/61 m²</div></div><div class='ak-tablerow'><div class='ak-tablecell'>Kaufpreis:</div><div class='ak-tablecell'>1,250.000.--</div></div></body></html>"""
    sis_page = '''<html><body><div class="estate-card"><h3>Familienhaus mit großem Garten</h3><a href="https://immobilien-sis.com/immobilie/familienhaus-mit-garten/">Zum Objekt</a><div>Ort: Starnberg</div><div>Wohnfläche: 145,50 m²</div><div>Kaufpreis: 1.250.000 €</div></div></body></html>'''
    ede_page = '''<html><body><div class="all_objects_row mb-50 mt-50"><div class="dm_all_objects_info"><div class="list_title">Exklusive Dachgeschosswohnung in Top-Lage</div><div class="dm_all_objects_list mt-10 date_row desc"><p class="mb-0"><span class="fl_c"><strong>Ort</strong></span> <span>München</span></p><p class="mb-0"><span class="fl_c"><strong>Wohnfläche</strong></span> <span>ca. 101 m²</span></p><p class="mb-0"><span class="fl_c"><strong>Kaufpreis</strong></span> <span>1.290.000,00 €</span></p><div class="theme_btn mt-30"><a class="oo-details-btn" href="https://www.ede-invest.com/objekt/12345-exklusive-dachgeschosswohnung/">Details anzeigen</a></div></div></div></div></body></html>'''

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
        if 'sopart-immobilien.de/haeuser-zum-kauf' in url:
            return FakeResponse(sopart_page)
        if 'jalea-immobilien.de/angebote/' in url:
            return FakeResponse(jalea_overview)
        if 'jalea-immobilien.de/immobilie/wohnen-an-den-amperauen/' in url:
            return FakeResponse(jalea_detail)
        if 'sedlmayr-immo.de/immobilien-kauf-und-miete-in-andechs-und-umgebung-sedlmayr-immobilien/' in url:
            return FakeResponse(sedlmayr_overview)
        if 'sedlmayr-immo.de/immobilie/charmantes-landhaus/' in url:
            return FakeResponse(sedlmayr_detail)
        if 'immo-kaiserreich.de/immobilienangebote/' in url and 'moderne-villa-am-see' not in url:
            return FakeResponse(kaiserreich_overview)
        if 'immo-kaiserreich.de/immobilienangebote/moderne-villa-am-see/' in url:
            return FakeResponse(kaiserreich_detail)
        if 'immobilien-sis.com/kaufen/' in url:
            return FakeResponse(sis_page)
        if 'ede-invest.com/angebote/' in url:
            return FakeResponse(ede_page)
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app.urllib.request, 'urlopen', side_effect=fake_urlopen):
        graf = app.fetch_graf_listings()
        riedel = app.fetch_riedel_listings()
        engel = app.fetch_engel_listings()
        weichselgartner = app.fetch_weichselgartner_listings()
        sopart = app.fetch_sopart_listings()
        jalea = app.fetch_jalea_listings()
        sedlmayr = app.fetch_sedlmayr_listings()
        kaiserreich = app.fetch_kaiserreich_listings()
        sis = app.fetch_sis_listings()
        ede = app.fetch_ede_listings()

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

    assert sopart and sopart[0]['title'] == 'Charmantes Reihenhaus mit Südgarten'
    assert sopart[0]['price'] == '1.120.000 €'
    assert sopart[0]['area_sqm'] == '132.50'
    assert sopart[0]['location'] == 'Germering'
    assert sopart[0]['link'].endswith('cmd=expose&id=12345')

    assert jalea and jalea[0]['title'] == 'Wohnen an den Amperauen'
    assert jalea[0]['price'] == '2.000.000 €'
    assert jalea[0]['area_sqm'] == '234.50'
    assert jalea[0]['location'] == 'Olching / Geiselbullach'
    assert jalea[0]['link'] == 'https://jalea-immobilien.de/immobilie/wohnen-an-den-amperauen/'

    assert sedlmayr and sedlmayr[0]['title'] == 'Charmantes Landhaus'
    assert sedlmayr[0]['price'] == '1.350.000 €'
    assert sedlmayr[0]['area_sqm'] == '186.40'
    assert sedlmayr[0]['location'] == 'Andechs'
    assert sedlmayr[0]['link'] == 'https://www.sedlmayr-immo.de/immobilie/charmantes-landhaus/'

    assert kaiserreich and kaiserreich[0]['title'] == 'Moderne Villa am See'
    assert kaiserreich[0]['price'] == '1.250.000 €'
    assert kaiserreich[0]['area_sqm'] == '188'
    assert kaiserreich[0]['location'] == 'Starnberg'
    assert kaiserreich[0]['link'] == 'https://immo-kaiserreich.de/immobilienangebote/moderne-villa-am-see/'

    assert sis and sis[0]['title'] == 'Familienhaus mit großem Garten'
    assert sis[0]['price'] == '1.250.000 €'
    assert sis[0]['area_sqm'] == '145.50'
    assert sis[0]['location'] == 'Starnberg'
    assert sis[0]['link'] == 'https://immobilien-sis.com/immobilie/familienhaus-mit-garten/'

    assert ede and ede[0]['title'] == 'Exklusive Dachgeschosswohnung in Top-Lage'
    assert ede[0]['price'] == '1.290.000 €'
    assert ede[0]['area_sqm'] == '101'
    assert ede[0]['location'] == 'München'
    assert ede[0]['link'] == 'https://www.ede-invest.com/objekt/12345-exklusive-dachgeschosswohnung/'


def test_jalea_area_requires_wohnflaeche_not_grundstuecksflaeche():
    jalea_overview = """<html><body><article class=\"frymo-listing-item post-id-4059\"><div class=\"frymo-listing-content\"><h3 class='frymo-listing-title'><a href='https://jalea-immobilien.de/immobilie/test-objekt/' aria-label='Test Objekt'>Test Objekt</a></h3><div class=\"frymo-listing-location\"><i class=\"frymo-icon frymo-icon-location-2\"></i>München</div></div></article></body></html>"""
    jalea_detail = """<html><body><div class=\"frymo-data-item content-inline\" data-key=\"kaufpreis\"><div class=\"frymo-data-item-label\">Kaufpreis</div><div class=\"frymo-data-item-value\">1.000.000 €</div></div><div class=\"frymo-data-item content-inline\" data-key=\"grundstuecksflaeche\"><div class=\"frymo-data-item-label\">Grundstücksfläche</div><div class=\"frymo-data-item-value\">ca. 999,00 m²</div></div></body></html>"""

    class FakeResponse:
        def __init__(self, text):
            self._payload = text.encode('utf-8')

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout=20):
        url = getattr(request, 'full_url', request)
        if 'jalea-immobilien.de/angebote/' in url:
            return FakeResponse(jalea_overview)
        if 'jalea-immobilien.de/immobilie/test-objekt/' in url:
            return FakeResponse(jalea_detail)
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app.urllib.request, 'urlopen', side_effect=fake_urlopen):
        jalea = app.fetch_jalea_listings()

    assert jalea and jalea[0]['title'] == 'Test Objekt'
    assert jalea[0]['area_sqm'] == ''


def test_new_broker_scrapers_and_price_threshold_rule():
    pages = {
        'nikki-livings.de/immobilienportfolio/': '''<html><body><article><h3><a href="https://nikki-livings.de/immobilie/penthouse-am-see/">Penthouse am See</a></h3><div>Ort: Starnberg</div><div>Wohnfläche: 120 m²</div><div>Kaufpreis: 1.450.000 €</div></article></body></html>''',
        'tsc-immobilien.de/category/kaufen/haeuser_und_villen/': '''<html><body><article><h3><a href="https://tsc-immobilien.de/immobilie/villa-am-park/">Villa am Park</a></h3><div>Ort: München</div><div>Wohnfläche: 210 m²</div><div>Kaufpreis: 2.100.000 €</div></article><article><h3><a href="https://tsc-immobilien.de/immobilie/kleines-haus/">Kleines Haus</a></h3><div>Ort: Dachau</div><div>Wohnfläche: 75 m²</div><div>Kaufpreis: 95.000 €</div></article></body></html>''',
        'tsc-immobilien.de/category/kaufen/wohnungen/': '''<html><body><article><h3><a href="https://tsc-immobilien.de/immobilie/design-wohnung/">Design Wohnung</a></h3><div>Ort: Augsburg</div><div>Wohnfläche: 98 m²</div><div>Kaufpreis: auf Anfrage</div></article></body></html>''',
        'imothek.de/kaufangebote-2/': '''<html><body><div><a href="https://www.imothek.de/objekt/charmante-wohnung/">Charmante Wohnung am See</a><div>Ort: Herrsching</div><div>Wohnfläche: 88 m²</div><div>Preis: 640.000 €</div></div></body></html>''',
        'vr-starnberg-zugspitze.de/alle-immobilien/haeuser/': '''<html><body><article><a href="https://immobilien.vr-starnberg-zugspitze.de/immobilie/haus-123/">Einfamilienhaus mit Garten</a><div>Ort: Weilheim</div><div>Wohnfläche: 165 m²</div><div>Kaufpreis: 1.150.000 €</div></article></body></html>''',
        'vr-starnberg-zugspitze.de/alle-immobilien/wohnungen/': '''<html><body><article><a href="https://immobilien.vr-starnberg-zugspitze.de/immobilie/wohnung-321/">Helle Stadtwohnung</a><div>Ort: Garmisch</div><div>Wohnfläche: 79 m²</div><div>Kaufpreis: 490.000 €</div></article></body></html>''',
        'stolze-immobilien.com/ff/immobilien/': '''<html><body><article><h2><a href="https://www.stolze-immobilien.com/objekt/familienhaus/">Familienhaus in ruhiger Lage</a></h2><div>Ort: Fürstenfeldbruck</div><div>Wohnfläche: 154 m²</div><div>Kaufpreis: 980.000 €</div></article></body></html>''',
        'realwert-bayern.de/angebote/': '''<html><body><article><a href="https://realwert-bayern.de/immobilie/loft-innenstadt/">Loft in der Innenstadt</a><div>Ort: München</div><div>Wohnfläche: 101 m²</div><div>Kaufpreis: 1.020.000 €</div></article></body></html>''',
        'eden-living.de/angebote/': '''<html><body><article><a href="https://eden-living.de/objekt/gartenwohnung/">Gartenwohnung mit Terrasse</a><div>Ort: Starnberg</div><div>Wohnfläche: 92 m²</div><div>Kaufpreis: 820.000 €</div></article></body></html>''',
        'i-m-living.de/immobilien/haeuser-/-wohnungen/': '''<html><body><div class="immo_offers_wrapper"><div class="immo_offers_item" data-location="Bad Tölz" data-price="1240000"><a href="/immobilien/haus-am-bach/"><div class="immo_offers_item_text"><h3>Haus am Bach</h3><h5><span class="location">Bad Tölz</span></h5><div class="info"><ul><li>Wfl.: 140,00 m²</li></ul><ul><li>Kaufpreis: 1.240.000,00€</li></ul></div></div></a></div></div></body></html>''',
        'webau-immobilien.de/index.php4?cmd=searchDetails': '''<html><body><div><a href="index.php4?cmd=expose&id=7788">Doppelhaushälfte mit Weitblick</a><div>Ort: Penzberg</div><div>Wohnfläche: 132 m²</div><div>Kaufpreis: 795.000 €</div></div></body></html>''',
        'mar-immobilien.de/angebote': '''<html><body><article><a href="https://www.mar-immobilien.de/immobilie/penthouse-zentrum/">Penthouse im Zentrum</a><div>Ort: Landsberg</div><div>Wohnfläche: 117 m²</div><div>Kaufpreis: 910.000 €</div></article></body></html>''',
        'see-immo.de/aktuelle-immobilienangebote.html': '''<html><body><article><a href="https://www.see-immo.de/objekt/haus-see/">Haus mit Seezugang</a><div>Ort: Tutzing</div><div>Wohnfläche: 189 m²</div><div>Kaufpreis: 2.450.000 €</div></article></body></html>''',
        'heidinger-immobilien.de/kaufobjekte/': '''<html><body><article><a href="https://www.heidinger-immobilien.de/immobilie/stadtvilla/">Stadtvilla mit Einliegerwohnung</a><div>Ort: München</div><div>Wohnfläche: 230 m²</div><div>Kaufpreis: 3.200.000 €</div></article></body></html>''',
        'funer-immobilien-starnberg.de/aktuelle-immobilien/': '''<html><body><article><a href="https://funer-immobilien-starnberg.de/objekt/chalet/">Alpenchalet in Bestlage</a><div>Ort: Gmund</div><div>Wohnfläche: 176 m²</div><div>Kaufpreis: 1.980.000 €</div></article></body></html>''',
    }

    class FakeResponse:
        def __init__(self, text):
            self._payload = text.encode('utf-8')

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout=20):
        url = getattr(request, 'full_url', request)
        for key, payload in pages.items():
            if key in url:
                return FakeResponse(payload)
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app.urllib.request, 'urlopen', side_effect=fake_urlopen):
        nikki = app.fetch_nikki_listings()
        tsc = app.fetch_tsc_listings()
        imothek = app.fetch_imothek_listings()
        vr = app.fetch_vr_listings()
        stolze = app.fetch_stolze_listings()
        realwert = app.fetch_realwert_listings()
        eden = app.fetch_eden_listings()
        imliving = app.fetch_imliving_listings()
        webau = app.fetch_webau_listings()
        mar = app.fetch_mar_listings()
        seeimmo = app.fetch_seeimmo_listings()
        heidinger = app.fetch_heidinger_listings()
        funer = app.fetch_funer_listings()

    assert nikki and nikki[0]['title'] == 'Penthouse am See'
    assert imothek and imothek[0]['title'] == 'Charmante Wohnung am See'
    assert vr and len(vr) == 2
    assert stolze and stolze[0]['title'] == 'Familienhaus in ruhiger Lage'
    assert realwert and realwert[0]['price'] == '1.020.000 €'
    assert eden and eden[0]['location'] == 'Starnberg'
    assert imliving and imliving[0]['area_sqm'] == '140'
    assert webau and webau[0]['link'].endswith('cmd=expose&id=7788')
    assert mar and mar[0]['location'] == 'Landsberg'
    assert seeimmo and seeimmo[0]['title'] == 'Haus mit Seezugang'
    assert heidinger and heidinger[0]['price'] == '3.200.000 €'
    assert funer and funer[0]['location'] == 'Gmund'

    # 95.000 EUR must be excluded, while "auf Anfrage" remains included.
    assert tsc and len(tsc) == 2
    titles = {item['title'] for item in tsc}
    assert 'Kleines Haus' not in titles
    assert 'Design Wohnung' in titles


def test_webau_duplicate_expose_urls_are_deduplicated():
    webau_page = '''<html><body>
        <div><a href="index.php4?cmd=expose&id=7788">Doppelhaushälfte mit Weitblick</a><div>Ort: Penzberg</div><div>Wohnfläche: 132 m²</div><div>Kaufpreis: 795.000 €</div></div>
        <div><a href="index.php4?cmd=expose&id=7788">Zum Exposé</a><div>Ort: Penzberg</div><div>Wohnfläche: 132 m²</div><div>Kaufpreis: 795.000 €</div></div>
        <div><a href="index.php4?cmd=expose&id=7788">Mehr Infos</a><div>Ort: Penzberg</div><div>Wohnfläche: 132 m²</div><div>Kaufpreis: 795.000 €</div></div>
    </body></html>'''

    class FakeResponse:
        def __init__(self, text):
            self._payload = text.encode('utf-8')

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout=20):
        url = getattr(request, 'full_url', request)
        if 'webau-immobilien.de/index.php4?cmd=searchDetails' in url:
            return FakeResponse(webau_page)
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app.urllib.request, 'urlopen', side_effect=fake_urlopen):
        webau = app.fetch_webau_listings()

    assert len(webau) == 1
    assert webau[0]['link'].endswith('cmd=expose&id=7788')


def test_imliving_extracts_clean_location_name():
    imliving_page = '''<html><body><div class="immo_offers_wrapper"><div class="immo_offers_item" data-location="München" data-price="890000"><a href="/immobilien/stadtwohnung/"><div class="immo_offers_item_text"><h3>Stadtwohnung mit Balkon</h3><h5><span class="location"><span class="pin"></span> München</span></h5><div class="info"><ul><li>Wfl.: 91,50 m²</li></ul><ul><li>Kaufpreis: 890.000,00€</li></ul></div></div></a></div></div></body></html>'''

    class FakeResponse:
        def __init__(self, text):
            self._payload = text.encode('utf-8')

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout=20):
        url = getattr(request, 'full_url', request)
        if 'i-m-living.de/immobilien/haeuser-/-wohnungen/' in url:
            return FakeResponse(imliving_page)
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app.urllib.request, 'urlopen', side_effect=fake_urlopen):
        rows = app.fetch_imliving_listings()

    assert rows and rows[0]['title'] == 'Stadtwohnung mit Balkon'
    assert rows[0]['location'] == 'München'
    assert rows[0]['price'] == '890.000 €'


def test_apply_listing_rules_enforces_global_requirements_for_all_brokers():
    raw = [
        {
            'title': 'Zum Exposé',
            'price': '450.000 €',
            'area_sqm': '95',
            'location': 'München',
            'link': 'https://example.com/expose/1',
        },
        {
            'title': 'Altbauwohnung mit Balkon',
            'price': '95.000 €',
            'area_sqm': '73',
            'location': 'München',
            'link': 'https://example.com/expose/2',
        },
        {
            'title': 'Doppelhaushälfte am Park',
            'price': 'Preis auf Anfrage',
            'area_sqm': '141',
            'location': 'class="location">Starnberg',
            'link': 'https://example.com/expose/3',
        },
        {
            'title': 'Doppelhaushälfte am Park',
            'price': 'Preis auf Anfrage',
            'area_sqm': '141',
            'location': 'Starnberg',
            'link': 'https://example.com/expose/3',
        },
        {
            'title': 'Angebote',
            'price': 'Preis auf Anfrage',
            'area_sqm': '',
            'location': 'München',
            'link': 'https://example.com/angebote/',
        },
        {
            'title': 'Verfügbar Kauf',
            'price': '1.200.000 €',
            'area_sqm': '110',
            'location': 'München',
            'link': 'https://example.com/immobilie/charmante-wohnung-mit-balkon/',
        },
    ]

    rows = app.apply_listing_rules(raw)

    assert len(rows) == 2
    assert rows[0]['title'] == 'Doppelhaushälfte am Park'
    assert rows[0]['price'] == 'Preis auf Anfrage'
    assert rows[0]['location'] == 'Starnberg'
    assert rows[0]['link'] == 'https://example.com/expose/3'
    assert rows[1]['title'] == 'charmante wohnung mit balkon'
    assert rows[1]['price'] == '1.200.000 €'
    assert rows[1]['location'] == 'München'
    assert rows[1]['link'] == 'https://example.com/immobilie/charmante-wohnung-mit-balkon/'


def test_fetch_listings_applies_rules_uniformly_across_brokers():
    app.LISTINGS_CACHE = None
    app.LISTINGS_CACHE_TIME = 0

    source_data = {
        'broker_a': [
            {
                'title': 'Zum Exposé',
                'price': '450.000 €',
                'area_sqm': '95',
                'location': 'München',
                'link': 'https://a.example/expose/1',
            },
            {
                'title': 'Haus mit Garten',
                'price': '99000 €',
                'area_sqm': '120',
                'location': 'München',
                'link': 'https://a.example/expose/2',
            },
            {
                'title': 'Haus mit Garten',
                'price': 'Preis auf Anfrage',
                'area_sqm': '120',
                'location': 'class="loc">München',
                'link': 'https://a.example/expose/3',
            },
        ],
        'broker_b': [
            {
                'title': 'Villa am See',
                'price': '1.200.000 €',
                'area_sqm': '250',
                'location': 'Starnberg',
                'link': 'https://b.example/expose/10',
            },
            {
                'title': 'Villa am See',
                'price': '1.200.000 €',
                'area_sqm': '250',
                'location': 'Starnberg',
                'link': 'https://b.example/expose/10',
            },
        ],
    }

    broker_sources = [
        ('broker_a', lambda: source_data['broker_a']),
        ('broker_b', lambda: source_data['broker_b']),
    ]

    class FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn):
            return FakeFuture(fn())

    def fake_as_completed(futures):
        return list(futures.keys())

    with patch.object(app, 'BROKER_SOURCES', broker_sources), \
         patch.object(app, 'ThreadPoolExecutor', FakeExecutor), \
         patch.object(app, 'as_completed', fake_as_completed):
        data = app.fetch_listings()

    assert list(data.keys()) == ['broker_a', 'broker_b']
    assert len(data['broker_a']) == 1
    assert data['broker_a'][0]['title'] == 'Haus mit Garten'
    assert data['broker_a'][0]['price'] == 'Preis auf Anfrage'
    assert data['broker_a'][0]['location'] == 'München'

    assert len(data['broker_b']) == 1
    assert data['broker_b'][0]['title'] == 'Villa am See'
    assert data['broker_b'][0]['price'] == '1.200.000 €'


def test_fetch_listings_uses_source_specific_retry_on_zero_results():
    app.LISTINGS_CACHE = None
    app.LISTINGS_CACHE_TIME = 0

    broker_sources = [
        ('zero_broker', lambda: []),
    ]

    retry_calls = {'count': 0}

    def retry_fetcher():
        retry_calls['count'] += 1
        return [
            {
                'title': 'Haus mit Seeblick',
                'price': '1.250.000 €',
                'area_sqm': '145',
                'location': 'Starnberg',
                'link': 'https://retry.example/immobilie/haus-mit-seeblick/',
            }
        ]

    class FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn):
            return FakeFuture(fn())

    def fake_as_completed(futures):
        return list(futures.keys())

    with patch.object(app, 'BROKER_SOURCES', broker_sources), \
         patch.object(app, 'ZERO_RESULT_RETRY_FETCHERS', {'zero_broker': retry_fetcher}), \
         patch.object(app, 'ThreadPoolExecutor', FakeExecutor), \
         patch.object(app, 'as_completed', fake_as_completed):
        data = app.fetch_listings()

    assert retry_calls['count'] == 1
    assert len(data['zero_broker']) == 1
    assert data['zero_broker'][0]['title'] == 'Haus mit Seeblick'
    assert data['zero_broker'][0]['price'] == '1.250.000 €'


def test_zero_broker_retry_map_has_dedicated_alternative_handlers():
    expected_alt_retry = {
        'fischer': 'fetch_fischer_listings_retry_alt',
        'citigrund': 'fetch_citigrund_listings_retry_alt',
        'akurat': 'fetch_akurat_listings_retry_alt',
        'hegerich': 'fetch_hegerich_listings_retry_alt',
        'gerschlauer': 'fetch_gerschlauer_listings_retry_alt',
        'dahler': 'fetch_dahler_listings_retry_alt',
        'krimbacher': 'fetch_krimbacher_listings_retry_alt',
        'ft': 'fetch_ft_listings_retry_alt',
        'hirschmann': 'fetch_hirschmann_listings_retry_alt',
    }

    for broker, handler_name in expected_alt_retry.items():
        assert broker in app.ZERO_RESULT_RETRY_FETCHERS
        handler = app.ZERO_RESULT_RETRY_FETCHERS[broker]
        assert callable(handler)
        assert getattr(handler, '__name__', '') == handler_name


def test_newly_added_broker_keys_are_registered():
    expected = [
        'reichenberger', 'heidtmann', 'muellerenglisch', 'strobl', 'aundowohnbau', 'graef', 'roethig',
        'wangenheim', 'egger', 'neuesnest', 'parkavenue', 'weber', 'wurmseder', 'elvira', 'sothebys',
        'duerrenberger', 'woehry', 'vonrodenhausen', 'martinaschwarz', 'pienzenauer', 'friedlmaier',
        'windhausen', 'maier', 'riedl', 'heimmobilien', 'seebauer', 'zippold', 'muellergroscurth',
        'bunzco', 'immosmart', 'lehmannhueber', 'drescher', 'sqmeter', 'wegener', 'hackerglass',
        'wohnref', 'herrmann', 'schmidtmuenchen', 'davidjacques', 'dalexis', 'gg', 'marte',
        'dawonia', 'orange', 'vorstadtmakler',
    ]
    source_keys = {key for key, _ in app.BROKER_SOURCES}
    for key in expected:
        assert key in app.BROKER_LABELS
        assert app.BROKER_LABELS[key]
        assert key in source_keys


def test_fetch_listings_invokes_broker_specific_retries_for_critical_zero_brokers():
    app.LISTINGS_CACHE = None
    app.LISTINGS_CACHE_TIME = 0

    brokers = ['fischer', 'citigrund', 'akurat', 'hegerich', 'gerschlauer', 'dahler', 'krimbacher', 'ft', 'hirschmann']
    broker_sources = [(key, lambda: []) for key in brokers]
    retry_calls = {key: 0 for key in brokers}

    def make_retry(broker_key):
        def _retry():
            retry_calls[broker_key] += 1
            return [{
                'title': f'{broker_key} Objekt',
                'price': '1.250.000 €',
                'area_sqm': '120',
                'location': 'München',
                'link': f'https://retry.example/{broker_key}/objekt-1',
            }]
        return _retry

    retry_map = {key: make_retry(key) for key in brokers}

    class FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn):
            return FakeFuture(fn())

    def fake_as_completed(futures):
        return list(futures.keys())

    with patch.object(app, 'BROKER_SOURCES', broker_sources), \
         patch.object(app, 'ZERO_RESULT_RETRY_FETCHERS', retry_map), \
         patch.object(app, 'ThreadPoolExecutor', FakeExecutor), \
         patch.object(app, 'as_completed', fake_as_completed):
        data = app.fetch_listings()

    for key in brokers:
        assert retry_calls[key] == 1
        assert key in data
        assert len(data[key]) == 1
        assert data[key][0]['title'] == f'{key} Objekt'


def test_eden_sis_webau_location_fallback_from_links_when_markup_is_dirty():
    eden_page = '''<html><body><article><a href="https://eden-living.de/angebote/wohnung-eigentumswohnung-in-muenchen-kaufen-81479/">Top Wohnung</a><div>Kaufpreis: 1.200.000 €</div><div>Wohnfläche: 84 m²</div><div>Ort: aria-label="broken"</div></article></body></html>'''
    sis_page = '''<html><body><article><a href="https://immobilien-sis.com/immobilie/penthouse-in-tutzing/">Penthouse mit Seeblick</a><div>Kaufpreis: 1.050.000 €</div><div>Wohnfläche: 120 m²</div><div>Ort: aria-label="broken"</div></article></body></html>'''
    webau_page = '''<html><body><article><a href="https://www.webau-immobilien.de/Muenchen/obj-A419.html">Objekt A419</a><div>Kaufpreis: 685.000 €</div><div>Wohnfläche: 75,64 m²</div><div>Ort: srcset="broken"</div></article></body></html>'''

    class FakeResponse:
        def __init__(self, text):
            self._payload = text.encode('utf-8')

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout=20):
        url = getattr(request, 'full_url', request)
        if 'eden-living.de/angebote/' in url:
            return FakeResponse(eden_page)
        if 'immobilien-sis.com/kaufen/' in url:
            return FakeResponse(sis_page)
        if 'webau-immobilien.de/index.php4?cmd=searchDetails' in url:
            return FakeResponse(webau_page)
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app.urllib.request, 'urlopen', side_effect=fake_urlopen):
        eden = app.fetch_eden_listings()
        sis = app.fetch_sis_listings()
        webau = app.fetch_webau_listings()

    assert eden and eden[0]['location'] == 'München'
    assert sis and sis[0]['location'] == 'Tutzing'
    assert webau and webau[0]['location'] == 'München'


def test_apply_listing_rules_blocks_common_cta_titles():
    raw = [
        {
            'title': 'Zum Expose',
            'price': '450.000 €',
            'area_sqm': '80',
            'location': 'München',
            'link': 'https://example.com/immobilie/test-1',
        },
        {
            'title': 'Mehr Infos',
            'price': '550.000 €',
            'area_sqm': '90',
            'location': 'München',
            'link': 'https://example.com/immobilie/test-2',
        },
        {
            'title': 'Details ansehen',
            'price': '650.000 €',
            'area_sqm': '95',
            'location': 'München',
            'link': 'https://example.com/immobilie/test-3',
        },
        {
            'title': 'Helle Wohnung mit Balkon',
            'price': '750.000 €',
            'area_sqm': '99',
            'location': 'München',
            'link': 'https://example.com/immobilie/test-4',
        },
    ]

    rows = app.apply_listing_rules(raw)
    assert len(rows) == 1
    assert rows[0]['title'] == 'Helle Wohnung mit Balkon'


def test_apply_listing_rules_uses_na_when_location_cannot_be_verified():
    raw = [
        {
            'title': 'Luxuswohnung mit Balkon',
            'price': '750.000 €',
            'area_sqm': '99',
            'location': 'Potenzial fuer Familie',
            'link': 'https://example.com/expose/unknown-1',
        },
    ]

    rows = app.apply_listing_rules(raw)
    assert len(rows) == 1
    assert rows[0]['location'] == 'N/A'


def test_fetch_listings_attempts_krimbacher_retry_when_primary_is_empty():
    app.LISTINGS_CACHE = None
    app.LISTINGS_CACHE_TIME = 0

    calls = {'retry': 0}

    def krimbacher_retry_probe():
        calls['retry'] += 1
        return []

    class FakeFuture:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class FakeExecutor:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn):
            return FakeFuture(fn())

    def fake_as_completed(futures):
        return list(futures.keys())

    with patch.object(app, 'BROKER_SOURCES', [('krimbacher', lambda: [])]), \
         patch.object(app, 'ThreadPoolExecutor', FakeExecutor), \
         patch.object(app, 'as_completed', fake_as_completed), \
         patch.dict(app.ZERO_RESULT_RETRY_FETCHERS, {'krimbacher': krimbacher_retry_probe}, clear=True):
        data = app.fetch_listings()

    assert calls['retry'] == 1
    assert 'krimbacher' in data
    assert data['krimbacher'] == []


def test_new_19_brokers_global_rule_spotchecks():
    brokers = [
        'weiherer', 'mb', 'fischer', 'heimhuber', 'citigrund', 'georgi', 'akurat', 'hegerich', 'eder',
        'gerschlauer', 'dahler', 'krimbacher', 'klatt', 'ft', 'tesch', 'ritter', 'hirschmann', 'rohrer', 'mrlodge',
    ]

    raw = []
    for broker in brokers:
        raw.append({
            'title': f'{broker} Familienhaus',
            'price': '1.250.000 €',
            'area_sqm': '140',
            'location': 'class="location">München',
            'link': f'https://example.com/{broker}/immobilie/familienhaus',
        })
        raw.append({
            'title': f'{broker} Niedrigpreis',
            'price': '95.000 €',
            'area_sqm': '75',
            'location': 'München',
            'link': f'https://example.com/{broker}/immobilie/niedrigpreis',
        })
        raw.append({
            'title': f'{broker} Anfrageobjekt',
            'price': 'Preis auf Anfrage',
            'area_sqm': '110',
            'location': 'München',
            'link': f'https://example.com/{broker}/immobilie/anfrageobjekt',
        })

    rows = app.apply_listing_rules(raw)
    kept_links = {row['link'] for row in rows}

    for broker in brokers:
        assert f'https://example.com/{broker}/immobilie/familienhaus' in kept_links
        assert f'https://example.com/{broker}/immobilie/anfrageobjekt' in kept_links
        assert f'https://example.com/{broker}/immobilie/niedrigpreis' not in kept_links

    for row in rows:
        assert app.matches_price_rule(row['price'])
        assert app.has_explicit_price(row['price'])


def test_weiherer_replaces_generic_card_labels_with_detail_title():
    overview_html = '''<html><body>
    <a href="https://www.weiherer-immobilien.de/objekt/haidhausen/">Ort</a>
    <div>Kaufpreis: 960.000,00 €</div>
    <div>Wohnfläche: 84,00 m²</div>
    </body></html>'''
    detail_html = '''<html><body>
    <h1>Stilvolle Altbauwohnung - urbanes Wohnen im Herzen von Haidhausen</h1>
    </body></html>'''

    def fake_fetch_html(url, timeout=20):
        if 'kaufobjekte' in url:
            return overview_html
        if '/objekt/haidhausen/' in url:
            return detail_html
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app, 'fetch_html', side_effect=fake_fetch_html):
        rows = app.fetch_weiherer_listings()

    assert rows
    assert rows[0]['title'].startswith('Stilvolle Altbauwohnung')
    assert rows[0]['title'] != 'Ort'
    assert rows[0]['price'] == '960.000 €'
    assert rows[0]['location'] == 'Haidhausen'


def test_rohrer_source_specific_parser_collects_multiple_sale_exposes_and_skips_rent_links():
        rohrer_page_1 = '''<html><body>
        <div class="angebote-teaser-slider-item">
            <a class="angebote-teaser-slider-link" href="https://rohrer-firmengruppe.de/immobilien-vermarktung/immobilien/wohnung-zum-kaufen-in-muenchen-r-7001.html">29 Aktuelle Angebote</a>
            <div>Ort: München</div><div>Wohnfläche: 84,0</div><div>Kaufpreis: 495.000,00 €</div>
        </div>
        <div class="angebote-teaser-slider-item">
            <a class="angebote-teaser-slider-link" href="https://rohrer-firmengruppe.de/immobilien-vermarktung/immobilien/buero-zum-mieten-in-muenchen-r-7002.html">Büro zur Miete</a>
            <div>Ort: München</div><div>Wohnfläche: 55,0</div><div>Kaufpreis: 200.000,00 €</div>
        </div>
        </body></html>'''
        rohrer_page_2 = '''<html><body>
        <div class="angebote-teaser-slider-item">
            <a class="angebote-teaser-slider-link" href="https://rohrer-firmengruppe.de/immobilien-vermarktung/immobilien/haus-zum-kaufen-in-grasbrunn-r-7003.html">Hausangebot</a>
            <div>Ort: Grasbrunn</div><div>Wohnfläche: 143,5</div><div>Kaufpreis: 1.250.000,00 €</div>
        </div>
        </body></html>'''

        def fake_fetch_html(url, timeout=20):
                if url.endswith('immobilien.html'):
                        return rohrer_page_1
                if '__yPage=2' in url:
                        return rohrer_page_2
                raise AssertionError(f'unexpected url: {url}')

        with patch.object(app, 'fetch_html', side_effect=fake_fetch_html):
                rows = app.fetch_rohrer_listings_source_specific()

        assert len(rows) == 2
        links = {row['link'] for row in rows}
        assert 'https://rohrer-firmengruppe.de/immobilien-vermarktung/immobilien/wohnung-zum-kaufen-in-muenchen-r-7001.html' in links
        assert 'https://rohrer-firmengruppe.de/immobilien-vermarktung/immobilien/haus-zum-kaufen-in-grasbrunn-r-7003.html' in links
        assert all('zum-mieten' not in row['link'] for row in rows)
        assert all(row['title'] != '29 Aktuelle Angebote' for row in rows)


def test_searchdetails_retry_parser_uses_detail_pages_for_clean_titles():
    hegerich_overview = '''<html><body>
    <a href="index.php4?cmd=searchDetails&amp;objq[cursor]=0&amp;kaufartids=1">objq[cursor]=0&amp;kaufartids=1\">ZUM EXPOSÉ 450.000,- €</a>
    </body></html>'''
    hegerich_detail = '''<html><head><title>Charmante 2-Zimmer-Wohnung in München-Sendling | Hegerich</title></head><body>
    <h1>Charmante 2-Zimmer-Wohnung in München-Sendling</h1>
    <div>Kaufpreis: 450.000 €</div>
    <div>Wohnfläche: 58,5 m²</div>
    <div>Adresse: 81369 München</div>
    </body></html>'''

    gerschlauer_overview = '''<html><body>
    <a href="index.php4?cmd=searchDetails&amp;objq[cursor]=3&amp;kaufartids=1">Details&amp;objq[cursor]=3\">ZUM EXPOSÉ 1.100.000,- €</a>
    </body></html>'''
    gerschlauer_detail = '''<html><head><title>Freistehendes Haus in Tutzing | Gerschlauer</title></head><body>
    <h1>Freistehendes Haus in Tutzing</h1>
    <div>Kaufpreis: 1.100.000 €</div>
    <div>Wohnfläche: 198 m²</div>
    <div>Ort: Tutzing</div>
    </body></html>'''

    def fake_fetch_html(url, timeout=20):
        if 'hegerich-immobilien.de/index.php4?cmd=searchResults' in url:
            return hegerich_overview
        if 'hegerich-immobilien.de/index.php4?cmd=searchDetails' in url:
            return hegerich_detail
        if 'gerschlauer.de/Haeuser-zum-Kauf.htm' in url:
            return gerschlauer_overview
        if 'gerschlauer.de/index.php4?cmd=searchDetails' in url:
            return gerschlauer_detail
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app, 'fetch_html', side_effect=fake_fetch_html):
        hegerich_rows = app.fetch_hegerich_listings_retry_alt()
        gerschlauer_rows = app.fetch_gerschlauer_listings_retry_alt()

    assert hegerich_rows and hegerich_rows[0]['title'] == 'Charmante 2-Zimmer-Wohnung in München-Sendling'
    assert hegerich_rows[0]['location'] == 'München'
    assert 'objq[cursor]' not in hegerich_rows[0]['title']

    assert gerschlauer_rows and gerschlauer_rows[0]['title'] == 'Freistehendes Haus in Tutzing'
    assert gerschlauer_rows[0]['location'] == 'Tutzing'
    assert 'objq[cursor]' not in gerschlauer_rows[0]['title']


def test_klatt_source_specific_parser_fills_location_and_skips_rent_links():
    klatt_overview = '''<html><body>
    <a href="https://alexander-klatt.de/immobilien/haus-einfamilienhaus-in-gilching-kaufen-111/">Zum Objekt</a>
    <a href="https://alexander-klatt.de/immobilien/wohnung-etagenwohnung-in-muenchen-mieten-222/">Miete Objekt</a>
    </body></html>'''
    klatt_detail = '''<html><head><title>Haus in Gilching | Alexander Klatt Immoconcept München</title></head><body>
    <h1>Haus in Gilching</h1>
    <div>Kaufpreis: 1.250.000 €</div>
    <div>Wohnfläche: 144 m²</div>
    </body></html>'''

    def fake_fetch_html(url, timeout=20):
        if url.rstrip('/') == 'https://alexander-klatt.de/immobilien':
            return klatt_overview
        if 'haus-einfamilienhaus-in-gilching-kaufen-111' in url:
            return klatt_detail
        if 'mieten-222' in url:
            raise AssertionError('rent listing must not be fetched')
        raise AssertionError(f'unexpected url: {url}')

    with patch.object(app, 'fetch_html', side_effect=fake_fetch_html):
        rows = app.fetch_klatt_listings_source_specific()

    assert len(rows) == 1
    assert rows[0]['title'] == 'Haus in Gilching'
    assert rows[0]['location'] == 'Gilching'
    assert rows[0]['price'] == '1.250.000 €'
    assert rows[0]['area_sqm'] == '144'
    assert 'mieten' not in rows[0]['link']


def test_extract_location_from_title_handles_searchdetails_style_noise():
    assert app.extract_location_from_title('Johanneskirchen - Entzckendes RMH in gnstiger Erbpacht inkl. Stellplatz') == 'Johanneskirchen'
    assert app.extract_location_from_title('Ruhiges Mehrfamilienhaus mit 10 Parteien nahe S-Bahn-Station Grafing') == 'Grafing'
    assert app.extract_location_from_title('HEGERICH: 3-Zi.-Wohnung plus 1-Zi.-Appartement mit vielseitigen Nutzungsmöglichkeiten in Mnchen') == 'München'
    assert app.extract_location_from_title('HEGERICH: Vermietete 2-Zimmer-Wohnung mit Blick ins Grne in gefragter Lage am Olympiapark') == 'München'
    assert app.extract_location_from_title('Krailling am Rand zu Planegg - Helle, vermietete 3-Zimmer-Wohnung mit zwei Balkonen') == 'Krailling'
    assert app.extract_location_from_title('HEGERICH: Helle, vermietete 3-Zimmer-Wohnung mit zwei Balkonen - Krailling am Rand zu Planegg') == 'Krailling'


def test_location_hardening_rejects_funer_false_tokens_and_keeps_real_two_token_city():
    assert app.extract_location_from_link('https://funer-immobilien-starnberg.de/immobilie/muenchen-an-den-isarauen-freie-25-zi-whg-grosser-loggia-im-3-ogm/') == ''
    assert app.extract_location_from_link('https://funer-immobilien-starnberg.de/immobilie/starnberg-besondere-3-zi-dg-galeriewohnung-mit-sep-hellen-hobbyraum/') == ''
    assert app.extract_location_from_link('https://funer-immobilien-starnberg.de/immobilie/freie-wohnung-mit-2-loggien/') == ''
    assert app.extract_location_from_title('Im Herzen von Bad Tölz: herrlich wohnen, arbeiten und Freizeit genießen') == 'Bad Tölz'
    assert app.resolve_listing_location('Hobbyraum', 'starnberg besondere 3 zi dg galeriewohnung', 'https://funer-immobilien-starnberg.de/immobilie/starnberg-besondere-3-zi-dg-galeriewohnung-mit-sep-hellen-hobbyraum/') == 'N/A'
