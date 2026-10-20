"""Source descriptions remain useful plain text across supported feed formats."""
import re
import unicodedata
from html import escape

import pytest

from pulsefeed.errors import AppError
from pulsefeed.rss import normalize, parse_feed

BASE = "https://example.com/rss"


def rss_item(fields):
    body = ('<rss><channel xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            '<item><title>Story</title>' + fields + '</item></channel></rss>')
    return parse_feed(body.encode(), BASE)['items'][0]


@pytest.mark.parametrize('fields,expected', [
    ('<content:encoded>Full body</content:encoded><description>Short description</description>', 'Short description'),
    ('<description> </description><content:encoded>Useful body</content:encoded>', 'Useful body'),
    ('<description>&lt;script&gt;hidden&lt;/script&gt;</description><content:encoded>Useful body</content:encoded>', 'Useful body'),
    ('<description/><description>Second description</description>', 'Second description'),
    ('<description>Comments</description>', 'Comments'),
    ('', ''),
])
def test_rss_prefers_first_useful_description_without_inventing_missing_text(fields, expected):
    item = rss_item(fields)
    assert item['excerpt'] == item['article']['excerpt'] == expected


@pytest.mark.parametrize('summary', [
    '<summary type="text">First paragraph. Second paragraph.</summary>',
    '<summary type="html">&lt;p&gt;First paragraph.&lt;/p&gt;&lt;p&gt;Second paragraph.&lt;/p&gt;</summary>',
    '<summary type="xhtml"><div xmlns="http://www.w3.org/1999/xhtml"><p>First paragraph.</p><script>hidden</script><style>hidden</style><p>Second paragraph.</p></div></summary>',
])
def test_atom_plain_html_and_xhtml_keep_paragraph_boundaries_and_hide_scripts(summary):
    body = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Story</title>'
            '<content>Full body</content>' + summary + '</entry></feed>')
    item = parse_feed(body.encode(), BASE)['items'][0]
    assert item['excerpt'] == 'First paragraph. Second paragraph.'


@pytest.mark.parametrize('layers', [1, 2, 4])
def test_nested_html_entities_produce_stable_worker_compatible_plain_text(layers):
    source = '<p>Cafe\u0301 <b>story</b>.</p><script>hidden</script><style>hidden</style>'
    for _ in range(layers):
        source = escape(source)
    article = rss_item('<description>' + source + '</description>')['article']
    assert article['excerpt'] == 'Café story .'
    for value in article.values():
        assert normalize(value) == value
        assert value == ' '.join(unicodedata.normalize('NFC', value).split())
        assert not re.search(r'<[^>]+>', value)


def test_excessive_encoding_is_rejected_with_bounded_work():
    source = '<b>Story</b>'
    for _ in range(12):
        source = escape(source)
    with pytest.raises(AppError, match='excessively encoded'):
        normalize(source)


def test_saved_fields_and_model_context_have_separate_bounds():
    body = ('<rss><channel><item><title>' + '🌱 '*500 + '</title><description>'
            + '🌼 '*3000 + '</description></item></channel></rss>')
    item = parse_feed(body.encode(), BASE)['items'][0]
    assert 0 < len(item['title']) <= 500
    assert 3500 < len(item['excerpt']) <= 4000
    assert len(item['article']['title']) + len(item['article']['excerpt']) <= 4000
    assert item['article']['excerpt'] == normalize(item['article']['excerpt'])
