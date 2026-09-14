# VERSION: 3.2
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

import base64
import concurrent.futures
import logging
import random
import re
import socket
import struct
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape
from html.parser import HTMLParser

try:
    from helpers import retrieve_url
except ImportError:
    retrieve_url = None

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
        'size', 'pub_date', 'categories', 'seeds', 'leech'
    )

    def __init__(self, title='', page_url='', magnet=None, torrent_url=None,
                 infohash=None, size='-1', pub_date=-1, categories=None,
                 seeds='-1', leech='-1'):
        self.title = title or ''
        self.page_url = page_url or ''
        self.magnet = magnet
        self.torrent_url = torrent_url
        self.infohash = infohash
        self.size = size or '-1'
        self.pub_date = pub_date if pub_date is not None else -1
        self.categories = tuple(categories or ())
        self.seeds = str(seeds) if seeds is not None else '-1'
        self.leech = str(leech) if leech is not None else '-1'


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
        try:
            return base64.b32decode(match.group(1), casefold=True).hex().lower()
        except Exception:
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

    def get_bytes(self, url, timeout=None):
        url = str(url or '').strip()
        if urllib.parse.urlsplit(url).scheme not in ('http', 'https'):
            return b'', url
        timeout = timeout if timeout is not None else self.timeout
        for attempt in range(self.retries + 1):
            try:
                request = urllib.request.Request(url, headers=self.headers, method='GET')
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read(), response.geturl()
            except urllib.error.HTTPError as exc:
                logger.debug('HTTP %s for %s, attempt %d', exc.code, url, attempt + 1)
                if exc.code not in (408, 429) and exc.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.debug('HTTP/network error for %s, attempt %d: %s', url, attempt + 1, exc)
        return b'', url

    def get(self, url, timeout=None):
        url = str(url or '').strip()
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
        if retrieve_url is not None:
            try:
                data = retrieve_url(url)
                if data:
                    if isinstance(data, bytes):
                        data = data.decode('utf-8', errors='replace')
                    return data, url
            except Exception as exc:
                logger.debug('retrieve_url fallback failed for %s: %s', url, exc)
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
    ARTICLE_RE = re.compile(r'<article\b[^>]*>(.*?)</article>', re.I | re.S)
    TITLE_RE = re.compile(
        r'''<h[12]\b[^>]*class=["'][^"']*entry-title[^"']*["'][^>]*>\s*'''
        r'''(?:<a\b[^>]*href=["']([^"']+)["'][^>]*>)?(.*?)'''
        r'''(?:</a>)?\s*</h[12]>''', re.I | re.S)
    MAGNET_RE = re.compile(r'''href\s*=\s*["'](magnet:\?[^"']+)["']''', re.I)
    TORRENT_RE = re.compile(r'''href\s*=\s*["']([^"']+?\.torrent(?:\?[^"']*)?)["']''', re.I)
    HASH_RE = re.compile(
        r'''(?:torrage\.info/torrent\.php\?h=|itorrents\.org/torrent/|btcache\.me/torrent/|infohash=|\bbtih:)'''
        r'''([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?![a-zA-Z0-9])''', re.I)
    SIZE_RE = re.compile(
        r'''Repack\s+Size[^\d]*?(?:from\s*)?([\d.,]+\s*(?:TB|GB|MB|KB|B))''', re.I)

    def parse(self, html, base_url, query=''):
        html = html or ''
        results = []
        parser = _SearchPageParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception as exc:
            logger.debug('Search HTML parse failed: %s', exc)
            parser.articles = []

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

        # Compatibility fallback close to the original working parser.
        if not results:
            for article_html in self.ARTICLE_RE.findall(html):
                candidate = self._parse_regex_article(article_html, base_url, query)
                if candidate and self.is_valid(candidate):
                    results.append(candidate)
        return results

    def _parse_regex_article(self, article_html, base_url, query):
        match = self.TITLE_RE.search(article_html)
        if not match:
            return None
        title = clean_text(re.sub(r'<[^>]+>', '', match.group(2)))
        if not title:
            return None
        page_url = normalize_url(match.group(1) or base_url, base_url)
        magnet_match = self.MAGNET_RE.search(article_html)
        magnet = unescape(magnet_match.group(1)).strip() if magnet_match else None
        hash_match = self.HASH_RE.search(article_html)
        infohash = normalize_infohash(hash_match.group(1)) if hash_match else None
        torrent_match = self.TORRENT_RE.search(article_html)
        torrent_url = normalize_url(torrent_match.group(1), page_url) if torrent_match else None
        size_match = self.SIZE_RE.search(article_html)
        size = size_to_bytes(size_match.group(1)) if size_match else '-1'
        date_match = re.search(
            r'''<time\b[^>]*class=["'][^"']*entry-date[^"']*["'][^>]*datetime=["']([^"']+)''',
            article_html, re.I
        )
        pub_date = parse_pub_date(date_match.group(1)) if date_match else -1
        categories = [
            clean_text(re.sub(r'<[^>]+>', '', value))
            for value in re.findall(
                r'''rel=["'][^"']*category tag[^"']*["'][^>]*>(.*?)</a>''',
                article_html, re.I | re.S
            )
        ]
        return Candidate(
            title=ensure_query_in_title(title, query), page_url=page_url,
            magnet=magnet, torrent_url=torrent_url, infohash=infohash,
            size=size, pub_date=pub_date, categories=categories,
        )

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


class TrackerStatsResolver(object):
    """Best-effort BitTorrent tracker scrape resolver.

    FitGirl pages do not reliably expose live swarm counts. When a magnet
    contains tracker URLs, or when configured public trackers are available,
    this resolver asks trackers for a scrape of the torrent's infohash.

    Both HTTP(S) scrape endpoints and UDP tracker scrape (BEP 15) are
    supported. Failure is deliberately non-fatal: qBittorrent receives -1
    when no tracker can provide a trustworthy count.
    """

    UDP_PROTOCOL_ID = 0x41727101980
    UDP_CONNECT_ACTION = 0
    UDP_SCRAPE_ACTION = 2

    def __init__(self, http_client, trackers, timeout=1.2, max_trackers=4, max_workers=16):
        self.http = http_client
        self.trackers = tuple(trackers or ())
        self.timeout = max(0.8, float(timeout))
        self.max_trackers = max(1, int(max_trackers))
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def resolve(self, candidate):
        if not candidate:
            return candidate

        infohash = candidate.infohash or infohash_from_magnet(candidate.magnet)
        if not infohash:
            return candidate

        infohash = normalize_infohash(infohash)
        if not infohash or not re.fullmatch(r'[0-9a-fA-F]{40}', infohash):
            return candidate
        candidate.infohash = infohash

        trackers = self._candidate_trackers(candidate.magnet)
        if not trackers:
            return candidate

        trackers_to_query = trackers[:self.max_trackers]
        futures = [self._executor.submit(self._scrape, tracker, infohash)
                   for tracker in trackers_to_query]

        max_seeds = -1
        max_leech = -1
        try:
            for future in concurrent.futures.as_completed(futures, timeout=self.timeout + 0.5):
                try:
                    stats = future.result()
                except Exception as exc:
                    logger.debug('Tracker scrape failed: %s', exc)
                    continue
                if stats and stats[0] >= 0 and stats[1] >= 0:
                    seeds, leech = stats
                    if seeds > max_seeds or (seeds == max_seeds and leech > max_leech):
                        max_seeds = seeds
                        max_leech = leech
                    if max_seeds >= 10:
                        break
        except concurrent.futures.TimeoutError:
            pass

        if max_seeds >= 0 and max_leech >= 0:
            candidate.seeds = str(max_seeds)
            candidate.leech = str(max_leech)

        return candidate

    def _candidate_trackers(self, magnet):
        trackers = []
        if magnet:
            try:
                parsed = urllib.parse.urlsplit(magnet)
                for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                    if key.lower() == 'tr' and value:
                        trackers.append(value)
            except (ValueError, TypeError):
                pass
        trackers.extend(self.trackers)

        seen = set()
        result = []
        for tracker in trackers:
            tracker = unescape(str(tracker)).strip()
            if not tracker or tracker in seen:
                continue
            scheme = urllib.parse.urlsplit(tracker).scheme.lower()
            if scheme not in ('http', 'https', 'udp'):
                continue
            seen.add(tracker)
            result.append(tracker)
        return result

    def _scrape(self, tracker, infohash):
        scheme = urllib.parse.urlsplit(tracker).scheme.lower()
        if scheme in ('http', 'https'):
            return self._scrape_http(tracker, infohash)
        if scheme == 'udp':
            return self._scrape_udp(tracker, infohash)
        return None

    def _scrape_http(self, tracker, infohash):
        try:
            parsed = urllib.parse.urlsplit(tracker)
            path = parsed.path or '/announce'
            if path.endswith('/announce'):
                path = path[:-len('/announce')] + '/scrape'
            elif not path.endswith('/scrape'):
                path = path.rstrip('/') + '/scrape'

            infohash_bytes = bytes.fromhex(infohash)
            existing = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            existing = [(k, v) for k, v in existing if k.lower() != 'info_hash']
            existing.append(('info_hash', urllib.parse.quote_from_bytes(infohash_bytes)))

            parts = []
            for key, value in existing:
                if key == 'info_hash':
                    parts.append(urllib.parse.quote(key, safe='') + '=' + value)
                else:
                    parts.append(urllib.parse.quote(key, safe='') + '=' + urllib.parse.quote(value, safe=''))
            scrape_url = urllib.parse.urlunsplit((
                parsed.scheme, parsed.netloc, path, '&'.join(parts), parsed.fragment
            ))

            raw, _ = self.http.get_bytes(scrape_url, timeout=self.timeout)
            if not raw or b'5:files' not in raw:
                return None

            decoded, _ = bdecode(raw, 0)
            files = decoded.get(b'files', {}) if isinstance(decoded, dict) else {}
            entry = files.get(infohash_bytes)
            if isinstance(entry, dict):
                # BEP 48 standard keys are complete (seeds) and incomplete (leechers)
                seeds = int(entry.get(b'complete', entry.get(b'seeds', entry.get(b'seeders', -1))))
                leech = int(entry.get(b'incomplete', entry.get(b'peers', entry.get(b'leechers', -1))))
                if seeds >= 0 and leech >= 0:
                    return seeds, leech
        except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
            logger.debug('HTTP scrape failed for %s: %s', tracker, exc)
        return None

    def _scrape_udp(self, tracker, infohash):
        parsed = urllib.parse.urlsplit(tracker)
        host = parsed.hostname
        port = parsed.port or 6969
        if not host:
            return None

        try:
            infohash_bytes = bytes.fromhex(infohash)
        except ValueError:
            return None

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            transaction = random.randint(0, 0x7FFFFFFF)
            packet = struct.pack('!QII', self.UDP_PROTOCOL_ID,
                                 self.UDP_CONNECT_ACTION, transaction)
            address = (host, port)
            sock.sendto(packet, address)
            data, address = sock.recvfrom(2048)
            if len(data) < 16:
                return None
            action, response_transaction = struct.unpack('!II', data[:8])
            if action != self.UDP_CONNECT_ACTION or response_transaction != transaction:
                return None
            connection_id = data[8:16]
            scrape_transaction = random.randint(0, 0x7FFFFFFF)
            request = (connection_id + struct.pack('!II', self.UDP_SCRAPE_ACTION,
                                                     scrape_transaction) +
                       infohash_bytes)
            sock.sendto(request, address)
            data, _ = sock.recvfrom(2048)
            if len(data) < 20:
                return None
            action, response_transaction = struct.unpack('!II', data[:8])
            if action != self.UDP_SCRAPE_ACTION or response_transaction != scrape_transaction:
                return None
            seeders, completed, leechers = struct.unpack('!III', data[8:20])
            return int(seeders), int(leechers)
        except (OSError, ValueError, struct.error, socket.timeout):
            return None
        finally:
            sock.close()


class DownloadResolver(object):
    """Resolves a Candidate into a usable download link and swarm stats."""

    def __init__(self, http_client, trackers):
        self.http = http_client
        self.trackers = tuple(trackers or ())
        self.stats = TrackerStatsResolver(http_client, self.trackers)
        self.post_parser = SearchParser()

    def resolve(self, candidate):
        if not candidate:
            return None

        if candidate.magnet:
            candidate.infohash = candidate.infohash or infohash_from_magnet(candidate.magnet)
            candidate.magnet = enrich_magnet(candidate.magnet, self.trackers)
            return self.stats.resolve(candidate)

        if candidate.infohash:
            candidate.magnet = enrich_magnet(
                build_magnet(candidate.infohash, candidate.title), self.trackers
            )
            return self.stats.resolve(candidate)

        if candidate.page_url:
            html, final_url = self.http.get(candidate.page_url, timeout=6)
            if html:
                parsed = self.post_parser.parse_document(html, final_url, candidate.title)
                if parsed:
                    merge_candidates(candidate, parsed)

        if candidate.magnet:
            candidate.infohash = candidate.infohash or infohash_from_magnet(candidate.magnet)
            candidate.magnet = enrich_magnet(candidate.magnet, self.trackers)
            return self.stats.resolve(candidate)

        if candidate.infohash:
            candidate.magnet = enrich_magnet(
                build_magnet(candidate.infohash, candidate.title), self.trackers
            )
            return self.stats.resolve(candidate)

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
            'seeds': candidate.seeds,
            'leech': candidate.leech,
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
        'udp://opentor.net:6969',
        'udp://open.stealth.si:80/announce',
        'udp://explodie.org:6969/announce',
        'udp://tracker.openbittorrent.com:6969/announce',
        'udp://tracker.torrent.eu.org:451/announce',
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
    MAX_WORKERS = 8
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
            for candidate in self.resolve_candidates(candidates):
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
            return

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            futures = [executor.submit(self.resolver.resolve, candidate) for candidate in candidates]
            for future in concurrent.futures.as_completed(futures):
                try:
                    candidate = future.result()
                except Exception as exc:
                    logger.debug('Candidate resolution failed: %s', exc)
                    continue
                if candidate:
                    yield candidate

    def is_search_result_page(self, final_url, query):
        # A redirected post normally has no '?s=' search parameter.
        parsed = urllib.parse.urlsplit(final_url or '')
        query_params = urllib.parse.parse_qs(parsed.query)
        return 's' in query_params


# ----------------------------------------------------------------------
# Pure helper functions: easy to unit-test independently.
# ----------------------------------------------------------------------


def bdecode(data, index=0):
    """Minimal bencode decoder for tracker scrape responses."""
    if index >= len(data):
        raise ValueError('Unexpected end of bencode')
    token = data[index:index + 1]
    if token == b'i':
        end = data.index(b'e', index + 1)
        return int(data[index + 1:end]), end + 1
    if token == b'l':
        values = []
        index += 1
        while data[index:index + 1] != b'e':
            value, index = bdecode(data, index)
            values.append(value)
        return values, index + 1
    if token == b'd':
        result = {}
        index += 1
        while data[index:index + 1] != b'e':
            key, index = bdecode(data, index)
            value, index = bdecode(data, index)
            result[key] = value
        return result, index + 1
    if token.isdigit():
        colon = data.index(b':', index)
        length = int(data[index:colon])
        start = colon + 1
        end = start + length
        return data[start:end], end
    raise ValueError('Invalid bencode token')


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
    if source.seeds != '-1':
        target.seeds = source.seeds
    if source.leech != '-1':
        target.leech = source.leech
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
        infohash.lower(),
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
