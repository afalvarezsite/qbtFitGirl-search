# VERSION: 2.0
# AUTHORS: Spidy, afalvarezsite
# Robust qBittorrent search plugin for FitGirl Repacks.

import concurrent.futures
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import unescape

try:
    from novaprinter import prettyPrinter, anySizeToBytes
except ImportError:
    def prettyPrinter(res):
        pass
    anySizeToBytes = None

logger = logging.getLogger(__name__)


class fitgirl_repacks(object):
    url = 'https://fitgirl-repacks.site/'
    name = 'FitGirl Repacks'
    supported_categories = {'all': '', 'games': ''}

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

    # Kept regex-based for zero-dependency compatibility with old qBittorrent.
    _re_articles = re.compile(r'<article\b[^>]*>(.*?)</article>', re.I | re.S)
    _re_title = re.compile(
        r'''<h1\b[^>]*class=["'][^"']*entry-title[^"']*["'][^>]*>\s*'''
        r'''(?:<a\b[^>]*href=["']([^"']+)["'][^>]*>)?(.*?)'''
        r'''(?:</a>)?\s*</h1>''', re.I | re.S)
    _re_tags = re.compile(r'<[^>]+>')
    _re_category = re.compile(
        r'''rel=["'][^"']*category tag[^"']*["'][^>]*>(.*?)</a>''', re.I | re.S)
    _re_date = re.compile(
        r'''<time\b[^>]*class=["'][^"']*entry-date[^"']*["'][^>]*datetime=["']([^"']+)["']''', re.I)
    _re_magnet = re.compile(r'''href\s*=\s*["'](magnet:\?[^"']+)["']''', re.I)
    _re_torrent_url = re.compile(
        r'''href\s*=\s*["']([^"']+?\.torrent(?:\?[^"']*)?)["']''', re.I)
    _re_hash = re.compile(
        r'''(?:torrage\.info/torrent\.php\?h=|itorrents\.org/torrent/|'''
        r'''btcache\.me/torrent/|infohash=|\bbtih:)'''
        r'''([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})(?![a-zA-Z0-9])''', re.I)
    _re_size = re.compile(
        r'''Repack\s+Size[^\d]*?(?:from\s*)?'''
        r'''(\d+(?:[.,]\d+)?\s*(?:TB|GB|MB|KB|B))''', re.I)
    _re_size_units = re.compile(r'([\d.,]+)\s*(TB|GB|MB|KB|B)\b', re.I)
    _re_pages = re.compile(
        r'''class=["'][^"']*page-numbers[^"']*["'][^>]*>\s*(\d+)\s*</a>''', re.I)
    _re_canonical = re.compile(
        r'''<link\b[^>]*rel=["'][^"']*\bcanonical\b[^"']*["'][^>]*href=["']([^"']+)["']''', re.I)
    _re_og_title = re.compile(
        r'''<meta\b[^>]*property=["']og:title["'][^>]*content=["']([^"']+)["']''', re.I)

    _ignored_titles = (
        'updates digest', 'updates list', 'faq', 'donate', 'day of requests'
    )

    def _safe_decode(self, data):
        if isinstance(data, bytes):
            return data.decode('utf-8', errors='replace')
        return str(data) if data is not None else ''

    def _normalize_url(self, value, base_url=None):
        if not value:
            return ''
        return urllib.parse.urljoin(base_url or self.url, unescape(str(value)).strip())

    def _fetch(self, target_url, timeout=8):
        target_url = self._normalize_url(target_url)
        if not target_url:
            return '', ''
        parsed = urllib.parse.urlsplit(target_url)
        if parsed.scheme not in ('http', 'https'):
            logger.debug('Unsupported URL scheme: %s', target_url)
            return '', target_url

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(target_url, headers=self.headers, method='GET')
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return self._safe_decode(resp.read()), resp.geturl()
            except urllib.error.HTTPError as exc:
                logger.debug('HTTP %s fetching %s (attempt %d)', exc.code, target_url, attempt + 1)
                if exc.code not in (408, 429) and exc.code < 500:
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.debug('Network error fetching %s (attempt %d): %s', target_url, attempt + 1, exc)
        return '', target_url

    def _strip_html(self, value):
        return unescape(self._re_tags.sub('', value or '')).strip()

    def _format_size_to_bytes(self, size_str):
        if not size_str or str(size_str).strip() == '-1':
            return '-1'
        size_str = str(size_str).strip()
        if anySizeToBytes:
            try:
                return str(anySizeToBytes(size_str))
            except Exception as exc:
                logger.debug('anySizeToBytes failed for %r: %s', size_str, exc)
        match = self._re_size_units.search(size_str)
        if not match:
            return '-1'
        try:
            value = Decimal(match.group(1).replace(',', '.'))
        except InvalidOperation:
            return '-1'
        multipliers = {
            'B': Decimal(1), 'KB': Decimal(1024), 'MB': Decimal(1024) ** 2,
            'GB': Decimal(1024) ** 3, 'TB': Decimal(1024) ** 4,
        }
        return str(int(value * multipliers[match.group(2).upper()]))

    def _ensure_query_in_title(self, title, query):
        query = (query or '').strip()
        if not title or not query:
            return title
        clean = lambda s: re.sub(r'[^a-zA-Z0-9]+', '', s).lower()
        if clean(query) and clean(query) in clean(title) and query.lower() not in title.lower():
            return f'{title} [{query}]'
        return title

    def _parse_pub_date(self, article_html):
        match = self._re_date.search(article_html or '')
        if not match:
            return -1
        try:
            dt = datetime.fromisoformat(match.group(1).strip().replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except (ValueError, TypeError, OverflowError) as exc:
            logger.debug('Date parse failed: %s', exc)
            return -1

    def _extract_infohash(self, value):
        match = self._re_hash.search(unescape(str(value or '')))
        if not match:
            return None
        infohash = match.group(1)
        return infohash.lower() if len(infohash) == 40 else infohash.upper()

    def _extract_magnet(self, html):
        match = self._re_magnet.search(html or '')
        return unescape(match.group(1)).strip() if match else None

    def _extract_torrent_url(self, html, base_url):
        match = self._re_torrent_url.search(html or '')
        return self._normalize_url(match.group(1), base_url) if match else None

    def _build_magnet(self, infohash, title):
        if not infohash:
            return None
        return 'magnet:?xt=urn:btih:' + infohash + '&dn=' + urllib.parse.quote(title or '', safe='')

    def _enrich_magnet(self, magnet_link):
        if not magnet_link or not magnet_link.lower().startswith('magnet:'):
            return magnet_link
        try:
            parsed = urllib.parse.urlsplit(magnet_link)
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            existing = {v for k, v in query if k.lower() == 'tr'}
            for tracker in self.DEFAULT_TRACKERS:
                if tracker not in existing:
                    query.append(('tr', tracker))
            return urllib.parse.urlunsplit((
                parsed.scheme, parsed.netloc, parsed.path,
                urllib.parse.urlencode(query, doseq=True), parsed.fragment
            ))
        except (ValueError, TypeError) as exc:
            logger.debug('Magnet enrichment failed: %s', exc)
            return magnet_link

    def _extract_title_and_url(self, article_html, fallback_url=None):
        match = self._re_title.search(article_html or '')
        if match:
            title = self._strip_html(match.group(2))
            link = self._normalize_url(match.group(1) or fallback_url or '', fallback_url or self.url)
            if title:
                return title, link
        og = self._re_og_title.search(article_html or '')
        if og:
            title = self._strip_html(og.group(1))
            canonical = self._re_canonical.search(article_html or '')
            link = canonical.group(1) if canonical else fallback_url
            return title, self._normalize_url(link or '', fallback_url or self.url)
        return '', self._normalize_url(fallback_url or '', self.url)

    def _extract_article_data(self, article_html, raw_query, fallback_url=None):
        if not article_html:
            return None
        categories = tuple(self._strip_html(c) for c in self._re_category.findall(article_html))
        if any(c.lower() == 'updates digest' for c in categories):
            return None

        title, desc_link = self._extract_title_and_url(article_html, fallback_url)
        if not title or any(x in title.lower() for x in self._ignored_titles):
            return None

        pub_date = self._parse_pub_date(article_html)
        size = '-1'
        size_match = self._re_size.search(article_html)
        if size_match:
            size = size_match.group(1)

        download_link = self._extract_magnet(article_html)
        if not download_link:
            download_link = self._build_magnet(self._extract_infohash(article_html), title)

        if not download_link and desc_link:
            page_html, final_url = self._fetch(desc_link, timeout=self.POST_TIMEOUT)
            if page_html:
                download_link = self._extract_magnet(page_html)
                if not download_link:
                    download_link = self._build_magnet(self._extract_infohash(page_html), title)
                if not download_link:
                    download_link = self._extract_torrent_url(page_html, final_url)
                if size == '-1':
                    page_size = self._re_size.search(page_html)
                    if page_size:
                        size = page_size.group(1)
                desc_link = self._normalize_url(desc_link, final_url)

        if not download_link:
            return None
        if download_link.lower().startswith('magnet:'):
            download_link = self._enrich_magnet(download_link)

        return {
            'link': download_link,
            'name': self._ensure_query_in_title(title, raw_query),
            'size': self._format_size_to_bytes(size),
            'seeds': '-1',
            'leech': '-1',
            'engine_url': self.url,
            'desc_link': desc_link,
            'pub_date': pub_date,
        }

    def _result_key(self, result):
        link = result.get('link', '')
        infohash = self._extract_infohash(link)
        if infohash:
            return 'hash:' + infohash.lower()
        if link:
            return 'link:' + link
        return 'page:' + result.get('desc_link', '')

    def _extract_articles(self, html):
        return self._re_articles.findall(html or '')

    def _extract_total_pages(self, html):
        pages = self._re_pages.findall(html or '')
        try:
            return max(1, max(map(int, pages))) if pages else 1
        except (ValueError, TypeError):
            return 1

    def search(self, what, cat='all'):
        raw_query = urllib.parse.unquote(what or '').strip()
        if not raw_query or cat not in self.supported_categories:
            return

        seen_keys = set()
        total_results = 0
        page = 1
        total_pages = 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.MAX_WORKERS) as executor:
            while page <= total_pages and page <= self.MAX_PAGES and total_results < self.MAX_RESULTS:
                search_url = self.url + ('?s=' if page == 1 else f'page/{page}/?s=') + urllib.parse.quote(raw_query)
                html, final_url = self._fetch(search_url, timeout=self.SEARCH_TIMEOUT)
                if not html:
                    break

                articles = self._extract_articles(html)

                # WordPress can redirect exact matches directly to a post.
                if '?s=' not in final_url.lower():
                    candidates = (
                        [self._extract_article_data(a, raw_query, final_url) for a in articles]
                        if articles else [self._extract_article_data(html, raw_query, final_url)]
                    )
                    for result in candidates:
                        if not result:
                            continue
                        key = self._result_key(result)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        prettyPrinter(result)
                        total_results += 1
                        if total_results >= self.MAX_RESULTS:
                            break
                    break

                total_pages = min(self._extract_total_pages(html), self.MAX_PAGES)
                if not articles:
                    break

                remaining = self.MAX_RESULTS - total_results
                futures = [
                    executor.submit(self._extract_article_data, article, raw_query)
                    for article in articles[:remaining]
                ]

                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.debug('Article processing failed: %s', exc)
                        continue
                    if not result:
                        continue
                    key = self._result_key(result)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    prettyPrinter(result)
                    total_results += 1
                    if total_results >= self.MAX_RESULTS:
                        break

                page += 1
