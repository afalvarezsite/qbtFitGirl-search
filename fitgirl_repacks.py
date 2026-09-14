# VERSION: 3.0
# AUTHORS: Spidy, afalvarezsite
# Refactored qBittorrent search plugin for FitGirl Repacks.
#
# Architecture:
#   search -> HTTP client -> search parser -> Candidate objects
#          -> download resolver -> normalizer -> deduplicator -> prettyPrinter
#
# The implementation intentionally uses only the Python standard library plus
# qBittorrent's novaprinter module, so it remains suitable for classic search
# plugin environments.

import concurrent.futures
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser

try:
    from novaprinter import prettyPrinter, anySizeToBytes
except ImportError:
    def prettyPrinter(result):
        pass

    anySizeToBytes = None


logger = logging.getLogger(__name__)


class Candidate(object):
    """Intermediate representation used between parsing and qBittorrent output."""

    __slots__ = (
        'title', 'page_url', 'magnet', 'torrent_url', 'infohash',
        'size', 'pub_date', 'categories'
    )

    def __init__(self, title='', page_url='', magnet=None, torrent_url=None,
                 infohash=None, size='-1', pub_date=-1, categories=None):
        self.title = title or ''
        self.page_url = page_url or ''
        self.magnet = magnet
        self.torrent_url = torrent_url
        self.infohash = infohash
        self.size = size or '-1'
        self.pub_date = pub_date if pub_date is not None else -1
        self.categories = tuple(categories or ())


class _ArticleParser(HTMLParser):
    """Small HTML parser focused on the fields needed by this plugin."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.in_article = False
        self.article_depth = 0
        self.current_tag = None
        self.current_link = None
        self.title_parts = []
        self.title_link = ''
        self.categories = []
        self.date_value = ''
        self.magnets = []
        self.torrent_urls = []
        self.hashes = []
        self.text_parts = []
        self._time_depth = 0
        self._title_depth = 0
        self._category_depth = 0

    @staticmethod
    def _classes(attrs):
        return set((attrs.get('class') or '').lower().split())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag_lower = tag.lower()

        if tag_lower == 'article' and not self.in_article:
            self.in_article = True
            self.article_depth = 1
            return
        elif tag_lower == 'article' and self.in_article:
            self.article_depth += 1

        if not self.in_article:
            return

        classes = self._classes(attrs)

        if tag_lower in ('h1', 'h2') and 'entry-title' in classes:
            self._title_depth = 1
            self.title_parts = []
            self.title_link = ''
            return

        if self._title_depth and tag_lower == 'a':
            href = attrs.get('href')
            if href:
                self.title_link = href
            self.current_link = 'title'

        if tag_lower == 'time' and 'entry-date' in classes:
            self.date_value = attrs.get('datetime') or attrs.get('data-datetime') or ''
            self._time_depth = 1

        rel = (attrs.get('rel') or '').lower().split()
        if tag_lower == 'a' and 'category' in rel and 'tag' in rel:
            self._category_depth = 1
            self.current_link = 'category'

        href = attrs.get('href') or ''
        href_unescaped = unescape(href).strip()
        if href_unescaped.lower().startswith('magnet:?'):
            self.magnets.append(href_unescaped)
        elif re.search(r'\.torrent(?:\?|$)', href_unescaped, re.I):
            self.torrent_urls.append(href_unescaped)

        # Hashes can appear in visible text or hrefs on FitGirl posts.
        self._extract_hashes(href_unescaped)

        if tag_lower not in ('script', 'style'):
            self.current_tag = tag_lower

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if not self.in_article:
            return

        if self._title_depth and tag_lower in ('h1', 'h2'):
            self._title_depth = 0
        if self._time_depth and tag_lower == 'time':
            self._time_depth = 0
        if self._category_depth and tag_lower == 'a':
            self._category_depth = 0
            self.current_link = None

        if tag_lower == 'article':
            self.article_depth -= 1
            if self.article_depth <= 0:
                self.in_article = False

        self.current_tag = None

    def handle_data(self, data):
        if not self.in_article:
            return
        if not data:
            return

        if self._title_depth:
            self.title_parts.append(data)
        if self._category_depth:
            value = data.strip()
            if value:
                self.categories.append(value)
        if self.current_tag not in ('script', 'style'):
            self.text_parts.append(data)
            self._extract_hashes(data)

    def _extract_hashes(self, value):
        if not value:
            return
        patterns = (
            r'(?:torrage\.info/torrent\.php\?h=|'
            r'itorrents\.org/torrent/|btcache\.me/torrent/|'
            r'infohash=|\bbtih:)'
            r'([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?![a-zA-Z0-9])',
            r'\b([a-fA-F0-9]{40})\b',
        )
        for pattern in patterns:
            for match in re.finditer(pattern, value, re.I):
                self.hashes.append(match.group(1))

    def candidate(self, base_url, fallback_url=''):
        title = clean_text(' '.join(self.title_parts))
        page_url = self.title_link or fallback_url
        page_url = normalize_url(page_url, base_url)
        infohash = normalize_infohash(self.hashes[0]) if self.hashes else None
        magnet = self.magnets[0] if self.magnets else None
        torrent_url = normalize_url(self.torrent_urls[0], page_url or base_url) if self.torrent_urls else None
        return Candidate(
            title=title,
            page_url=page_url,
            magnet=magnet,
            torrent_url=torrent_url,
            infohash=infohash,
            size=extract_size_from_text(' '.join(self.text_parts)),
            pub_date=parse_pub_date(self.date_value),
            categories=self.categories,
        )


class _SearchPageParser(HTMLParser):
    """Extracts post-like articles from a WordPress search page."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.articles = []
        self._article_chunks = []
        self._depth = 0
        self._capture = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'article':
            if self._capture:
                self._depth += 1
            else:
                self._capture = True
                self._depth = 1
                self._article_chunks = ['<article>']
            return
        if self._capture:
            self._article_chunks.append(self.get_starttag_text() or '<%s>' % tag)

    def handle_startendtag(self, tag, attrs):
        if self._capture:
            self._article_chunks.append(self.get_starttag_text() or '<%s />' % tag)

    def handle_endtag(self, tag):
        if not self._capture:
            return
        self._article_chunks.append('</%s>' % tag)
        if tag.lower() == 'article':
            self._depth -= 1
            if self._depth <= 0:
                self.articles.append(''.join(self._article_chunks))
                self._article_chunks = []
                self._capture = False

    def handle_data(self, data):
        if self._capture:
            self._article_chunks.append(data)


class _DocumentParser(HTMLParser):
    """Parses a standalone post when WordPress redirects an exact search."""

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.title_parts = []
        self.title_link = ''
        self.og_title = ''
        self.canonical = ''
        self.date_value = ''
        self.categories = []
        self.magnets = []
        self.torrent_urls = []
        self.hashes = []
        self.text_parts = []
        self._title_depth = 0
        self._category_depth = 0
        self._time_depth = 0
        self._active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag_lower = tag.lower()
        classes = set((attrs.get('class') or '').lower().split())
        rel = (attrs.get('rel') or '').lower().split()

        if tag_lower in ('h1', 'h2') and 'entry-title' in classes:
            self._title_depth = 1
            self.title_parts = []
            self._active = 'title'
        elif tag_lower == 'a' and self._title_depth:
            self.title_link = attrs.get('href') or self.title_link
        elif tag_lower == 'time' and 'entry-date' in classes:
            self.date_value = attrs.get('datetime') or attrs.get('data-datetime') or ''
            self._time_depth = 1
        elif tag_lower == 'a' and 'category' in rel and 'tag' in rel:
            self._category_depth = 1
            self._active = 'category'

        property_name = (attrs.get('property') or '').lower()
        if tag_lower == 'meta' and property_name == 'og:title':
            self.og_title = attrs.get('content') or ''
        if tag_lower == 'link' and 'canonical' in rel:
            self.canonical = attrs.get('href') or ''

        href = unescape(attrs.get('href') or '').strip()
        if href.lower().startswith('magnet:?'):
            self.magnets.append(href)
        elif re.search(r'\.torrent(?:\?|$)', href, re.I):
            self.torrent_urls.append(href)
        self._extract_hashes(href)

    def handle_endtag(self, tag):
        tag_lower = tag.lower()
        if self._title_depth and tag_lower in ('h1', 'h2'):
            self._title_depth = 0
            self._active = None
        if self._category_depth and tag_lower == 'a':
            self._category_depth = 0
            self._active = None
        if self._time_depth and tag_lower == 'time':
            self._time_depth = 0

    def handle_data(self, data):
        if self._title_depth:
            self.title_parts.append(data)
        if self._category_depth:
            value = data.strip()
            if value:
                self.categories.append(value)
        self.text_parts.append(data)
        self._extract_hashes(data)

    def _extract_hashes(self, value):
        if not value:
            return
        for match in re.finditer(
            r'(?:torrage\.info/torrent\.php\?h=|itorrents\.org/torrent/|'
            r'btcache\.me/torrent/|infohash=|\bbtih:)'
            r'([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?![a-zA-Z0-9])',
            value, re.I
        ):
            self.hashes.append(match.group(1))
        for match in re.finditer(r'\b([a-fA-F0-9]{40})\b', value):
            self.hashes.append(match.group(1))

    def candidate(self, base_url):
        title = clean_text(' '.join(self.title_parts)) or clean_text(self.og_title)
        page_url = normalize_url(self.title_link or self.canonical or base_url, base_url)
        infohash = normalize_infohash(self.hashes[0]) if self.hashes else None
        magnet = self.magnets[0] if self.magnets else None
        torrent_url = normalize_url(self.torrent_urls[0], page_url or base_url) if self.torrent_urls else None
        return Candidate(
            title=title,
            page_url=page_url,
            magnet=magnet,
            torrent_url=torrent_url,
            infohash=infohash,
            size=extract_size_from_text(' '.join(self.text_parts)),
            pub_date=parse_pub_date(self.date_value),
            categories=self.categories,
        )


def clean_text(value):
    value = unescape(value or '')
    value = re.sub(r'\s+', ' ', value)
    return value.strip()


def normalize_url(value, base_url='https://fitgirl-repacks.site/'):
    if not value:
        return ''
    return urllib.parse.urljoin(base_url, unescape(str(value)).strip())


def normalize_infohash(value):
    if not value:
        return None
    value = unescape(str(value)).strip()
    match = re.search(r'\b([a-fA-F0-9]{40})\b', value)
    if match:
        return match.group(1).lower()
    match = re.search(r'\b([a-zA-Z2-7]{32})\b', value)
    if match:
        return match.group(1).upper()
    return None


def infohash_from_magnet(magnet):
    if not magnet:
        return None
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
        values = query.get('xt') or []
        for value in values:
            match = re.search(r'urn:btih:([a-zA-Z0-9]+)', value, re.I)
            if match:
                return normalize_infohash(match.group(1))
    except (ValueError, TypeError):
        pass
    return None


class HttpClient(object):
    """HTTP boundary: URL validation, timeout, retries and decoding."""

    def __init__(self, headers=None, timeout=8, retries=1):
        self.headers = headers or {}
        self.timeout = timeout
        self.retries = max(0, int(retries))

    def get(self, url, timeout=None):
        url = normalize_url(url)
        if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
            return '', url

        timeout = timeout if timeout is not None else self.timeout
        for attempt in range(self.retries + 1):
            try:
                request = urllib.request.Request(url, headers=self.headers, method='GET')
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = response.read()
                    if isinstance(data, bytes):
                        data = data.decode('utf-8', errors='replace')
                    return data, response.geturl()
            except urllib.error.HTTPError as exc:
                logger.debug('HTTP %s for %s, attempt %d', exc.code, url, attempt + 1)
                if exc.code not in (408, 429) and exc.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.debug('HTTP/network error for %s, attempt %d: %s', url, attempt + 1, exc)
        return '', url


class SearchParser(object):
    """Turns search HTML into Candidate objects without network access."""

    IGNORED_TITLES = (
        'updates digest', 'updates list', 'faq', 'donate', 'day of requests'
    )

    PAGE_RE = re.compile(
        r'''class=["'][^"']*page-numbers[^"']*["'][^>]*>\s*(\d+)\s*</a>''',
        re.I
    )

    def parse(self, html, base_url, query=''):
        parser = _SearchPageParser()
        try:
            parser.feed(html or '')
            parser.close()
        except Exception as exc:
            logger.debug('Search HTML parse failed: %s', exc)
            return []

        results = []
        for chunk in parser.articles:
            article_parser = _ArticleParser()
            try:
                article_parser.feed(chunk)
                article_parser.close()
                candidate = article_parser.candidate(base_url)
            except Exception as exc:
                logger.debug('Article parse failed: %s', exc)
                continue
            if self.is_valid(candidate):
                candidate.title = ensure_query_in_title(candidate.title, query)
                results.append(candidate)
        return results

    def parse_document(self, html, url, query=''):
        parser = _DocumentParser()
        try:
            parser.feed(html or '')
            parser.close()
            candidate = parser.candidate(url)
        except Exception as exc:
            logger.debug('Document parse failed: %s', exc)
            return None
        if not self.is_valid(candidate):
            return None
        candidate.title = ensure_query_in_title(candidate.title, query)
        return candidate

    def total_pages(self, html):
        pages = self.PAGE_RE.findall(html or '')
        try:
            return max([1] + [int(value) for value in pages])
        except (ValueError, TypeError):
            return 1

    def is_valid(self, candidate):
        if not candidate or not candidate.title:
            return False
        title_lower = candidate.title.lower()
        if any(item in title_lower for item in self.IGNORED_TITLES):
            return False
        if any(category.lower() == 'updates digest' for category in candidate.categories):
            return False
        return bool(candidate.page_url)


class DownloadResolver(object):
    """Resolves a Candidate into a usable magnet/torrent download link."""

    def __init__(self, http_client, trackers):
        self.http = http_client
        self.trackers = tuple(trackers or ())

    def resolve(self, candidate):
        if not candidate:
            return None

        # Priority 1: an explicit magnet from the search/post HTML.
        if candidate.magnet:
            candidate.infohash = candidate.infohash or infohash_from_magnet(candidate.magnet)
            candidate.magnet = enrich_magnet(candidate.magnet, self.trackers)
            return candidate

        # Priority 2: an infohash already visible in the search/post HTML.
        if candidate.infohash:
            candidate.magnet = enrich_magnet(
                build_magnet(candidate.infohash, candidate.title), self.trackers
            )
            return candidate

        # Priority 3: fetch the post page and inspect it.
        if candidate.page_url:
            html, final_url = self.http.get(candidate.page_url, timeout=6)
            if html:
                parsed = SearchParser().parse_document(html, final_url, candidate.title)
                if parsed:
                    merge_candidates(candidate, parsed)

        # Priority 4: use a torrent URL if the page exposes one.
        if candidate.magnet:
            candidate.infohash = candidate.infohash or infohash_from_magnet(candidate.magnet)
            candidate.magnet = enrich_magnet(candidate.magnet, self.trackers)
            return candidate

        if candidate.infohash:
            candidate.magnet = enrich_magnet(
                build_magnet(candidate.infohash, candidate.title), self.trackers
            )
            return candidate

        if candidate.torrent_url:
            return candidate

        return None


class ResultNormalizer(object):
    """Converts Candidate objects into the qBittorrent novaprinter schema."""

    def __init__(self, category_name='FitGirl Repacks'):
        self.category_name = category_name

    def normalize(self, candidate):
        if not candidate:
            return None

        link = candidate.magnet or candidate.torrent_url
        if not link:
            return None

        size = size_to_bytes(candidate.size)
        if size == '-1':
            # If the resolver parsed the post, its visible text may contain a size.
            size = extract_size_from_text(getattr(candidate, '_text', ''))

        return {
            'link': link,
            'name': candidate.title,
            'size': size,
            'seeds': '-1',
            'leech': '-1',
            'engine_url': 'https://fitgirl-repacks.site/',
            'desc_link': candidate.page_url,
            'pub_date': candidate.pub_date,
        }


class Deduplicator(object):
    """Deduplicates by strongest stable identity available."""

    def __init__(self):
        self.seen = set()

    def key(self, candidate_or_result):
        if isinstance(candidate_or_result, Candidate):
            candidate = candidate_or_result
            infohash = candidate.infohash or infohash_from_magnet(candidate.magnet)
            if infohash:
                return 'hash:' + infohash.lower()
            if candidate.magnet:
                return 'magnet:' + normalize_magnet(candidate.magnet)
            if candidate.torrent_url:
                return 'torrent:' + normalize_url(candidate.torrent_url)
            return 'page:' + normalize_url(candidate.page_url)

        link = (candidate_or_result or {}).get('link', '')
        infohash = infohash_from_magnet(link)
        if infohash:
            return 'hash:' + infohash.lower()
        if link:
            return 'link:' + link
        return 'page:' + (candidate_or_result or {}).get('desc_link', '')

    def add(self, item):
        key = self.key(item)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True


class fitgirl_repacks(object):
    url = 'https://fitgirl-repacks.site/'
    name = 'FitGirl Repacks'
    supported_categories = {'all': ''}

    DEFAULT_TRACKERS = (
        'udp://tracker.opentrackr.org:1337/announce',
        'udp://open.stealth.si:80/announce',
        'udp://tracker.torrent.eu.org:451/announce',
        'udp://tracker.theoks.net:6969/announce',
        'udp://tracker.openbittorrent.com:6969/announce',
        'udp://exodus.desync.com:6969/announce',
        'udp://explodie.org:6969/announce',
        'udp://open.tracker.cl:1337/announce',
    )

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    }

    MAX_RESULTS = 30
    MAX_PAGES = 3
    MAX_WORKERS = 4
    SEARCH_TIMEOUT = 8
    POST_TIMEOUT = 6
    MAX_RETRIES = 1

    def __init__(self):
        self.http = HttpClient(
            headers=self.headers,
            timeout=self.SEARCH_TIMEOUT,
            retries=self.MAX_RETRIES,
        )
        self.parser = SearchParser()
        self.resolver = DownloadResolver(self.http, self.DEFAULT_TRACKERS)
        self.normalizer = ResultNormalizer(self.name)

    # ------------------------------------------------------------------
    # Public plugin API
    # ------------------------------------------------------------------

    def search(self, what, cat='all'):
        query = urllib.parse.unquote(what or '').strip()
        if not query or cat not in self.supported_categories:
            return

        deduplicator = Deduplicator()
        emitted = 0

        for page in range(1, self.MAX_PAGES + 1):
            if emitted >= self.MAX_RESULTS:
                break

            search_url = self.build_search_url(query, page)
            html, final_url = self.http.get(search_url, timeout=self.SEARCH_TIMEOUT)
            if not html:
                logger.debug('No response for search page %s', search_url)
                break

            # Exact WordPress matches can redirect directly to a post.
            if not self.is_search_result_page(final_url, query):
                candidate = self.parser.parse_document(html, final_url, query)
                candidates = [candidate] if candidate else []
                total_pages = 1
            else:
                candidates = self.parser.parse(html, final_url, query)
                total_pages = min(self.parser.total_pages(html), self.MAX_PAGES)

            if not candidates:
                break

            candidates = candidates[:self.MAX_RESULTS - emitted]
            resolved = self.resolve_candidates(candidates)

            for candidate in resolved:
                if not candidate or not deduplicator.add(candidate):
                    continue
                result = self.normalizer.normalize(candidate)
                if not result:
                    continue
                prettyPrinter(result)
                emitted += 1
                if emitted >= self.MAX_RESULTS:
                    break

            if page >= total_pages or not is_probable_search_page(final_url, self.url):
                break

    def build_search_url(self, query, page=1):
        encoded = urllib.parse.quote(query)
        if page <= 1:
            return self.url + '?s=' + encoded
        return self.url + 'page/%d/?s=%s' % (page, encoded)

    def resolve_candidates(self, candidates):
        if not candidates:
            return []

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            futures = [executor.submit(self.resolver.resolve, candidate) for candidate in candidates]
            for future in concurrent.futures.as_completed(futures):
                try:
                    candidate = future.result()
                except Exception as exc:
                    logger.debug('Candidate resolution failed: %s', exc)
                    continue
                if candidate:
                    results.append(candidate)
        return results

    def is_search_result_page(self, final_url, query):
        # A redirected post normally has no '?s=' search parameter.
        parsed = urllib.parse.urlsplit(final_url or '')
        query_params = urllib.parse.parse_qs(parsed.query)
        return 's' in query_params


# ----------------------------------------------------------------------
# Pure helper functions: easy to unit-test independently.
# ----------------------------------------------------------------------


def merge_candidates(target, source):
    if source.title:
        target.title = source.title
    if source.page_url:
        target.page_url = source.page_url
    if source.magnet:
        target.magnet = source.magnet
    if source.torrent_url:
        target.torrent_url = source.torrent_url
    if source.infohash:
        target.infohash = source.infohash
    if source.size != '-1':
        target.size = source.size
    if source.pub_date != -1:
        target.pub_date = source.pub_date
    if source.categories:
        target.categories = source.categories
    return target


def ensure_query_in_title(title, query):
    title = clean_text(title)
    query = clean_text(query)
    if not title or not query:
        return title

    def compact(value):
        return re.sub(r'[^a-zA-Z0-9]+', '', value).lower()

    if compact(query) and compact(query) in compact(title) and query.lower() not in title.lower():
        return '%s [%s]' % (title, query)
    return title


def build_magnet(infohash, title):
    if not infohash:
        return None
    return 'magnet:?xt=urn:btih:%s&dn=%s' % (
        infohash,
        urllib.parse.quote(title or '', safe='')
    )


def normalize_magnet(magnet):
    if not magnet:
        return ''
    try:
        parsed = urllib.parse.urlsplit(magnet)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        # Trackers are order-insensitive; sort only the query representation
        # for deterministic deduplication while preserving all parameters.
        query = sorted(query, key=lambda item: (item[0].lower(), item[1]))
        return urllib.parse.urlunsplit((
            parsed.scheme.lower(), parsed.netloc, parsed.path,
            urllib.parse.urlencode(query, doseq=True), parsed.fragment
        ))
    except (ValueError, TypeError):
        return magnet.strip()


def enrich_magnet(magnet, trackers):
    if not magnet or not magnet.lower().startswith('magnet:'):
        return magnet
    try:
        parsed = urllib.parse.urlsplit(magnet)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        existing = set(value for key, value in query if key.lower() == 'tr')
        for tracker in trackers:
            if tracker not in existing:
                query.append(('tr', tracker))
        return urllib.parse.urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(query, doseq=True),
            parsed.fragment
        ))
    except (ValueError, TypeError) as exc:
        logger.debug('Magnet enrichment failed: %s', exc)
        return magnet


def parse_pub_date(value):
    if not value:
        return -1
    try:
        normalized = value.strip().replace('Z', '+00:00')
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError, OverflowError, AttributeError):
        return -1


def size_to_bytes(size_str):
    if not size_str or str(size_str).strip() == '-1':
        return '-1'
    value = str(size_str).strip()

    if anySizeToBytes:
        try:
            return str(anySizeToBytes(value))
        except Exception as exc:
            logger.debug('anySizeToBytes failed for %r: %s', value, exc)

    match = re.search(r'([\d.,]+)\s*(TB|GB|MB|KB|B)\b', value, re.I)
    if not match:
        return '-1'
    try:
        number = Decimal(match.group(1).replace(',', '.'))
    except InvalidOperation:
        return '-1'

    multipliers = {
        'B': Decimal(1),
        'KB': Decimal(1024),
        'MB': Decimal(1024) ** 2,
        'GB': Decimal(1024) ** 3,
        'TB': Decimal(1024) ** 4,
    }
    return str(int(number * multipliers[match.group(2).upper()]))


def extract_size_from_text(text):
    if not text:
        return '-1'
    match = re.search(
        r'Repack\s+Size[^\d]*?(?:from\s*)?'
        r'([\d.,]+\s*(?:TB|GB|MB|KB|B))',
        text, re.I
    )
    if not match:
        return '-1'
    return size_to_bytes(match.group(1))


def is_probable_search_page(url, base_url):
    try:
        parsed = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(base_url)
        return parsed.netloc.lower() == base.netloc.lower()
    except (ValueError, AttributeError):
        return False


# Keep this helper available for compatibility with code/tests that may use it.
def any_size_to_bytes(size_str):
    return size_to_bytes(size_str)
