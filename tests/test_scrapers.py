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
    for key in ['graf', 'riedel', 'engel', 'weichselgartner', 'sopart', 'jalea', 'sedlmayr', 'kaiserreich', 'sis', 'ede']:
        assert key in app.BROKER_LABELS
        assert app.BROKER_LABELS[key]
    assert 'aigner' in app.IGNORED_BROKERS
    assert any(key == 'sopart' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'jalea' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'sedlmayr' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'kaiserreich' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'sis' for key, _ in app.BROKER_SOURCES)
    assert any(key == 'ede' for key, _ in app.BROKER_SOURCES)


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
