"""Reader presentation checks use immutable local views, never external models."""
from html.parser import HTMLParser
import pytest

from pulsefeed.assets import reader_date, source_origin, reader_created, reader_excerpt
from pulsefeed.auth import issue_link
from pulsefeed.db import dump
from test_jobs import finish
from test_view_feed import source


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def reader_html(response):
    # Bootstrap intentionally retains the complete source snapshot; only visible
    # card markup is restricted to selected illustrations.
    return response.text.split('<script id="bootstrap"', 1)[0]


@pytest.mark.parametrize('value,expected', [
    ('Thu, 15 Oct 2026 16:45:00 GMT', '15 Oct 2026'),
    ('2026-10-15T00:30:00+02:00', '14 Oct 2026'),
    ('Thu, 15 Oct 2026 23:30:00 -0200', '16 Oct 2026'),
    ('2026-10-15', '15 Oct 2026'),
    ('2026-10-15T12:00:00', '15 Oct 2026'),
    ('15 Oct 2026 12:00:00', '15 Oct 2026'),
    ('2026-10-15T12:00:00Z', '15 Oct 2026'),
    ('garbled feed date', ''), ('2026-02-30', ''), ('', ''), (None, ''),
])
def test_reader_date_is_short_utc_and_hides_invalid_input(value, expected):
    assert reader_date(value) == expected


@pytest.mark.parametrize('url,expected', [
    ('https://WWW.Example.COM:443/path?q=one#hash', 'https://www.example.com'),
    ('https://www.bücher.example/path', 'https://www.xn--bcher-kva.example'),
    ('http://[::1]:8080/path', 'http://[::1]:8080'),
    ('https://user:password@example.com/path', ''),
    ('https://example.com:bad/path', ''), ('http://[', ''),
    ('javascript:alert(1)', ''), ('//example.com/path', ''),
    ('https://example.com/\nunsafe', ''), ('https://example.com\\evil', ''), (None, ''),
])
def test_source_origin_is_safe_and_drops_article_path(url, expected):
    assert source_origin(url) == expected


def test_ssr_only_selected_image_slots_with_readable_dates_and_safe_sources(env):
    fetched = source(tuple('story-'+str(index) for index in range(10)))
    fetched['items'][0].update(published='Thu, 15 Oct 2026 16:45:00 GMT', link='https://WWW.Example.COM:443/article?x=1')
    fetched['items'][1].update(published='bad ugly date', link='javascript:alert(1)')
    fetched['items'][2].update(link='https://user:secret@example.com/article')
    saved = env['app'].state.service.view_feed(env['owner'], 'https://example.com/rss', 'presentation-view', fetched)
    response = env['client'].get(saved['view']['url'])
    html = reader_html(response)
    tags = Elements(html).tags
    cards = [attrs for tag, attrs in tags if tag=='article' and 'reader-card' in attrs.get('class','').split()]
    assert [card['data-item-id'] for card in cards] == [item['id'] for item in saved['items'][:10]]
    assert html.count('class="image-area"') == 10
    assert len(response.context['initial_feed']['items']) == 10
    assert '<h3>story-9</h3>' in html
    assert '>15 Oct 2026</time>' in html and 'bad ugly date' not in html
    sources = [attrs for tag, attrs in tags if tag=='a' and attrs.get('class')=='article-source']
    assert [item['href'] for item in sources] == ['https://www.example.com'] + ['https://example.com'] * 7
    ctas = [attrs for tag, attrs in tags if tag=='a' and 'reader-story-cta' in attrs.get('class','')]
    assert len(ctas) == 8 and ctas[0]['class'] == 'button button-dark source-link reader-story-cta'
    assert any(tag=='a' and attrs.get('id')=='feed-source' and attrs['href']=='https://example.com/rss' for tag, attrs in tags)
    assert 'Download' not in html and 'Check a new feed' not in html
    assert env['provider'].submissions == []


@pytest.mark.parametrize('admin_mode', [False, True])
def test_failed_reader_notice_is_compact_while_admin_keeps_details(env, admin_mode):
    owner = env['owner']
    if admin_mode:
        owner = env['admin']
        env['client'].get(issue_link(env['db'], env['config'], owner).replace(env['config'].base_url, ''))
    saved = env['app'].state.service.view_feed(owner, 'demo', 'failed-presentation', source())
    failed = saved['runs'][0]
    with env['db'].transaction() as conn:
        conn.execute('UPDATE runs SET state=?,error=? WHERE id=?', ('failed', dump({'code':'MODEL_NOT_READY','message':'private diagnostic','retryable':True,'stage':'qwen'}), failed['id']))
    response = env['client'].get(saved['view']['url'] + ('?admin' if admin_mode else ''))
    html = reader_html(response)
    tags = Elements(html).tags
    notices = [attrs for tag, attrs in tags if tag=='aside' and 'reader-illustration-notice' in attrs.get('class','')]
    assert len(notices) == (0 if admin_mode else 1)
    assert html.count('class="image-area"') == (5 if admin_mode else 4)
    assert ('private diagnostic' in html) == admin_mode
    assert '↻ Retry illustration' in html
    assert ('data-run-details=' in html) == admin_mode


async def test_completed_expired_provider_hint_keeps_square_until_browser_copy_checked(env):
    saved = env['app'].state.service.view_feed(env['owner'], 'demo', 'cached-presentation', source())
    await finish(env)
    with env['db'].transaction() as conn:
        conn.execute('UPDATE runs SET result_expires_at=1,expiry_estimated=0')
    response = env['client'].get(saved['view']['url'])
    assert response.context['initial_feed']['runs'][0]['availability'] == 'unavailable'
    html = reader_html(response)
    assert html.count('class="image-area"') == 5
    assert 'reader-illustration-notice notice' not in html
    assert 'Checking for a saved image in this browser.' in html


def test_blocked_view_and_home_do_not_promise_unselected_articles_or_downloads(env):
    service = env['app'].state.service
    service.view_feed(env['other'], 'other', 'busy-other-owner', source(('active',)))
    blocked = service.view_feed(env['owner'], 'demo', 'blocked-presentation', source())
    assert blocked['generation']['status'] == 'blocked'
    response = env['client'].get(blocked['view']['url'])
    html = reader_html(response)
    assert 'No illustrations started for this page.' in html
    assert 'read every story below' not in html
    assert 'class="image-area"' not in html
    assert 'Download' not in html
    home = env['client'].get('/').text
    for removed in ('Fresh illustrations for selected stories.', 'Your favorite stories,', 'Paste a feed URL', 'Up to 10 stories', 'Check a new feed'):
        assert removed not in home
    assert 'id="fun-loader"' not in home
    assert 'action="/logout"' not in home
    assert 'ambient.js' not in home


@pytest.mark.parametrize('value,expected', [
    ('Comments', ''), ('  COMMENTS  ', ''),
    ('Comments https://example.com/story#comments', ''),
    ('A short summary. Comments', 'A short summary.'),
    ('A short summary! Comments https://example.com/comments', 'A short summary!'),
    ('A short summary\nComments', 'A short summary'),
    ('A short summary\r\n Comments  ', 'A short summary'),
    ('We welcome comments.', 'We welcome comments.'),
    ('Officials declined Comments', 'Officials declined Comments'),
    ('Comments can improve a story.', 'Comments can improve a story.'),
    ('The comments were thoughtful. More text.', 'The comments were thoughtful. More text.'),
])
def test_reader_excerpt_removes_only_terminal_standalone_comment_boilerplate(value, expected):
    assert reader_excerpt(value) == expected


def test_reader_created_has_explicit_utc_and_rejects_invalid_epoch():
    assert reader_created(1792082700) == '15 Oct 2026 at 16:45 UTC'
    for value in (None, '', True, float('nan'), float('inf')):
        assert reader_created(value) == ''


def test_compact_feed_header_canvas_and_excerpt_cleaning(env):
    fetched = source()
    fetched['items'][0]['excerpt'] = 'A thoughtful story. Comments'
    fetched['items'][1]['excerpt'] = 'We welcome comments.'
    saved = env['app'].state.service.view_feed(env['owner'], 'https://example.com/rss', 'compact-header-view', fetched)
    response = env['client'].get(saved['view']['url'])
    html = reader_html(response)
    assert 'reader-feed-heading reader-feed-header-card' in html
    assert 'reader-feed-created' in html and reader_created(saved['view']['created_at']) in html
    assert '5 feed items · 5 selected illustrations.' in html
    assert 'Your one-hour reading room' not in html
    assert 'class="view-retention' not in html
    assert '<div class="reader-canvas"><div id="fun-loader"' in html
    assert html.index('class="reader-canvas"') < html.index('id="fun-loader"') < html.index('id="item-list"')
    assert '<p class="article-excerpt">A thoughtful story.</p>' in html
    assert '<p class="article-excerpt">We welcome comments.</p>' in html
    assert 'action="/logout"' not in html
    assert 'ambient.js' not in response.text


def test_admin_keeps_explicit_text_signout(env):
    env['client'].get(issue_link(env['db'], env['config'], env['admin']).replace(env['config'].base_url, ''))
    response = env['client'].get('/?admin')
    assert 'action="/logout"' in response.text
    assert '>Sign out</button>' in response.text
    assert 'title="Sign out"' not in response.text


def test_pending_landing_has_loader_skeletons_and_no_fetch_or_admission(env):
    tables = ('accounts','sessions','feeds','feed_views','runs','invocations','rate_events')
    before = {table:env['db'].all('SELECT * FROM '+table) for table in tables}
    response = env['client'].get('/feeds/new')
    assert response.status_code == 200
    assert response.context['pending_feed'] is True
    assert response.context['view'] is None and response.context['initial_feed'] is None
    html = reader_html(response)
    tags = Elements(html).tags
    assert 'Opening your feed' in html
    assert 'id="feed-form"' not in html and 'id="item-list"' not in html
    assert html.count('class="reader-card reader-skeleton"') == 3
    loader = next(attrs for tag, attrs in tags if attrs.get('id')=='fun-loader')
    assert loader['data-active']=='true' and loader['aria-hidden']=='true'
    retry = next(attrs for tag, attrs in tags if attrs.get('id')=='pending-retry')
    assert retry['type']=='button' and 'hidden' in retry and 'data-mutation' in retry
    assert any(attrs.get('id')=='pending-status' and attrs.get('role')=='status' for tag, attrs in tags)
    assert any(attrs.get('id')=='pending-home' and attrs.get('href')=='/' for tag, attrs in tags)
    assert '"pending_feed": true' in response.text
    assert before == {table:env['db'].all('SELECT * FROM '+table) for table in tables}
    assert env['provider'].submissions == []


@pytest.mark.parametrize('excerpt', ['', '   ', 'Comments', ' COMMENTS https://example.com/comments'])
def test_missing_reader_description_omits_empty_paragraph_and_keeps_story(env, excerpt):
    fetched = source(('Without a description',))
    fetched['items'][0].update(excerpt=excerpt, link='https://example.com/story')
    saved = env['app'].state.service.view_feed(env['owner'], 'demo', 'empty-description-view', fetched)
    response = env['client'].get(saved['view']['url'])
    html = reader_html(response)
    assert 'article-excerpt' not in html
    assert '<h3>Without a description</h3>' in html
    assert html.count('class="image-area"') == 1
    assert 'Read original story' in html
    assert env['provider'].submissions == []


def test_long_reader_description_remains_complete_escaped_text(env):
    from html import escape
    excerpt = '<script>alert("source")</script> ' + 'A detailed source description. ' * 120
    fetched = source(('Long description',))
    fetched['items'][0]['excerpt'] = excerpt
    saved = env['app'].state.service.view_feed(env['owner'], 'demo', 'long-description-view', fetched)
    html = reader_html(env['client'].get(saved['view']['url']))
    # Truncation belongs to the card's CSS; retain source text for accessibility and selection.
    assert '<p class="article-excerpt">' + escape(excerpt.rstrip()).replace('&#x27;', '&#39;').replace('&quot;', '&#34;') + '</p>' in html
    assert '<script>alert(' not in html
    assert env['provider'].submissions == []
