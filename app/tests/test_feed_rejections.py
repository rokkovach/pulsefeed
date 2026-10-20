"""Feed structure is validated before any view or inference admission is saved."""
import pytest
from fastapi.testclient import TestClient
from pulsefeed.errors import AppError
from pulsefeed.main import create_app
from pulsefeed.rss import parse_feed

BASE = 'https://example.com/rss'

REJECTED = [
    (b'<rss>', 'INVALID_FEED'),
    (b'<html><body>not a feed</body></html>', 'INVALID_FEED'),
    (b'<document><item><title>Not RSS</title></item></document>', 'INVALID_FEED'),
    (b'<rss/>', 'INVALID_FEED'),
    (b'<rss><item><title>Missing channel</title></item></rss>', 'INVALID_FEED'),
    (b'<rss><channel/><channel/></rss>', 'INVALID_FEED'),
    (b'<feed xmlns="urn:not-atom"><entry><title>Wrong namespace</title></entry></feed>', 'INVALID_FEED'),
    (b'<rss xmlns="urn:fake"><channel><item><title>Wrong namespace</title></item></channel></rss>', 'INVALID_FEED'),
    (b'<feed><entry><title>Missing Atom namespace</title></entry></feed>', 'INVALID_FEED'),
    (b'<rss><channel><title>Empty</title></channel></rss>', 'EMPTY_FEED'),
    (b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Empty</title></feed>', 'EMPTY_FEED'),
    (b'<rss><channel><item><description>No readable title</description></item></channel></rss>', 'EMPTY_FEED'),
    (b'<rss><channel><description><item><title>Nested non-item</title></item></description></channel></rss>', 'EMPTY_FEED'),
    (b'<feed xmlns="http://www.w3.org/2005/Atom"><wrapper><entry><title>Nested</title></entry></wrapper></feed>', 'EMPTY_FEED'),
    (b'<rss><channel xmlns:e="urn:foreign"><e:item><e:title>Foreign item</e:title></e:item></channel></rss>', 'EMPTY_FEED'),
    (b'<rss><channel xmlns:e="urn:foreign"><item><e:title>Foreign title</e:title></item></channel></rss>', 'EMPTY_FEED'),
    (b'<rss><channel><title>F</title><item><title>&lt;![bogus]&gt;story</title></item></channel></rss>', 'INVALID_FEED'),
    (b'<rss><channel><title>F</title><item><title>Story</title><description>&lt;![bogus]&gt;text</description></item></channel></rss>', 'INVALID_FEED'),
]


@pytest.mark.parametrize('body,code', REJECTED)
def test_rejects_nonfeed_empty_or_malformed_embedded_text(body, code):
    with pytest.raises(AppError) as error:
        parse_feed(body, BASE)
    assert (error.value.status, error.value.code) == (400, code)


@pytest.mark.parametrize('body,code', REJECTED)
def test_rejected_feed_never_creates_view_or_provider_work(env, body, code):
    async def fetcher(url):
        return parse_feed(body, url)
    app = create_app(env['config'], env['provider'], feed_fetcher=fetcher)
    tables = ('feeds', 'items', 'feed_views', 'view_runs', 'batches', 'runs', 'invocations')
    before = {table: env['db'].all('SELECT * FROM '+table) for table in tables}
    with TestClient(app) as client:
        client.cookies.update(env['client'].cookies)
        response = client.post('/api/view-feed', json={'url': BASE, 'idempotency_key': 'rejected-feed-key'}, headers=env['headers'])
    assert response.status_code == 400
    assert response.json()['error']['code'] == code
    assert before == {table: env['db'].all('SELECT * FROM '+table) for table in tables}
    assert env['provider'].submissions == []


@pytest.mark.parametrize('body', [
    b'<rss version="2.0"><channel><item><title>RSS story</title></item></channel></rss>',
    b'<rss><channel xmlns:content="http://purl.org/rss/1.0/modules/content/"><title>Feed</title><item><title>Story</title><content:encoded>Rich text</content:encoded></item></channel></rss>',
    b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title><entry><title>Story</title><link href="https://example.com/story"/><summary>Text</summary></entry></feed>',
    b'<feed xmlns="http://purl.org/atom/ns#"><entry><title>Older Atom</title></entry></feed>',
    b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"><channel rdf:about="https://example.com/rss"><title>RSS1</title></channel><item rdf:about="https://example.com/story"><title>Story</title><link>https://example.com/story</link><description>Text</description></item></rdf:RDF>',
])
def test_supported_feed_envelopes_remain_compatible_without_strict_metadata(body):
    result = parse_feed(body, BASE)
    assert len(result['items']) == 1 and result['items'][0]['title']
    assert result['title']


def test_first_ten_source_positions_precede_title_filtering_and_duplicates():
    invalid = '<item><description>Missing title</description></item>'
    late = '<item><title>Must not backfill position eleven</title></item>'
    with pytest.raises(AppError) as error:
        parse_feed(('<rss><channel>'+invalid*10+late+'</channel></rss>').encode(), BASE)
    assert error.value.code == 'EMPTY_FEED'
    duplicate = '<item><guid>same</guid><title>Duplicate</title></item>'
    result = parse_feed(('<rss><channel>'+duplicate*10+late+'</channel></rss>').encode(), BASE)
    assert len(result['items']) == 10
    assert len({item['guid'] for item in result['items']}) == 1
    assert all(item['title'] == 'Duplicate' for item in result['items'])
