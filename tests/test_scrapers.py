import json
import urllib.request
import sys
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
