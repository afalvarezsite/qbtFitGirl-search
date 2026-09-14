# VERSION: 1.2
# AUTHORS: Spidy, afalvarezsite
import concurrent.futures
import re
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape

try:
    from novaprinter import prettyPrinter, anySizeToBytes
except ImportError:
    def prettyPrinter(res):
        pass
    anySizeToBytes = None


class fitgirl_repacks(object):
    url = 'https://fitgirl-repacks.site/'
    name = 'FitGirl Repacks'
    supported_categories = {'all': ''}

    # High-speed public & 1337x trackers injected into all magnet links
    DEFAULT_TRACKERS = [
        'udp://tracker.opentrackr.org:1337/announce',
        'udp://open.stealth.si:80/announce',
        'udp://tracker.torrent.eu.org:451/announce',
        'udp://tracker.theoks.net:6969/announce',
        'udp://tracker.openbittorrent.com:6969/announce',
        'udp://exodus.desync.com:6969/announce',
        'udp://explodie.org:6969/announce',
        'udp://open.tracker.cl:1337/announce'
    ]

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    }

    # Pre-compiled regular expressions
    _re_pages = re.compile(r'class="[^"]*page-numbers[^"]*"[^>]*>(\d+)</a>', re.IGNORECASE)
    _re_articles = re.compile(r'<article\b[^>]*>(.*?)</article>', re.DOTALL | re.IGNORECASE)
    _re_title = re.compile(r'<h1\b[^>]*class="[^"]*entry-title[^"]*"[^>]*>\s*(?:<a\b[^>]*href="([^"]+)"[^>]*>)?(.*?)(?:</a>)?\s*</h1>', re.DOTALL | re.IGNORECASE)
    _re_tags = re.compile(r'<[^>]+>')
    _re_category = re.compile(r'rel="[^"]*category tag[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE)
    _re_date = re.compile(r'<time\b[^>]*class="[^"]*entry-date[^"]*"[^>]*datetime="([^"]+)"', re.IGNORECASE)
    _re_magnet = re.compile(r'href="(magnet:\?[^"]+)"', re.IGNORECASE)
    _re_hash = re.compile(r'(?:torrage\.info/torrent\.php\?h=|itorrents\.org/torrent/|btcache\.me/torrent/|torrent/|infohash=|\bbtih:)([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})', re.IGNORECASE)
    _re_torrent_url = re.compile(r'href="([^"]+?\.(?:torrent))"', re.IGNORECASE)
    _re_size = re.compile(r'Repack Size[^\d]*?(?:from\s*)?(\d+(?:\.\d+)?\s*(?:TB|GB|MB|KB))', re.IGNORECASE)
    _re_clean_alpha = re.compile(r'[^a-zA-Z0-9]')
    _re_size_units = re.compile(r'([\d\.]+)\s*(TB|GB|MB|KB|B)', re.IGNORECASE)

    _ignored_titles = ("updates digest", "updates list", "faq", "donate", "day of requests")

    def _safe_decode(self, data):
        if isinstance(data, bytes):
            return data.decode('utf-8', errors='ignore')
        return str(data) if data is not None else ""

    def _fetch(self, target_url, timeout=8):
        """Fetch URL content using urllib, returning (html_content, final_url)."""
        try:
            req = urllib.request.Request(target_url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final_url = resp.geturl()
                html_content = self._safe_decode(resp.read())
                return html_content, final_url
        except Exception:
            return "", target_url

    def _format_size_to_bytes(self, size_str):
        """Convert human-readable file size strings to byte count."""
        if not size_str or size_str == "-1":
            return "-1"
        if anySizeToBytes:
            try:
                return anySizeToBytes(size_str)
            except Exception:
                pass
        match = self._re_size_units.search(size_str)
        if not match:
            return "-1"
        num = float(match.group(1))
        unit = match.group(2).upper()
        multipliers = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4}
        return str(int(num * multipliers.get(unit, 1)))

    def _ensure_query_in_title(self, title, query):
        """Ensure title matches qBittorrent UI filter regardless of hyphens/spaces."""
        q_clean = self._re_clean_alpha.sub('', query).lower()
        t_clean = self._re_clean_alpha.sub('', title).lower()
        if q_clean and q_clean in t_clean and query.lower() not in title.lower():
            return f"{title} [{query}]"
        return title

    def _parse_pub_date(self, article_html):
        """Extract and parse ISO publication date into a UNIX timestamp."""
        date_match = self._re_date.search(article_html)
        if date_match:
            try:
                dt = datetime.fromisoformat(date_match.group(1))
                return int(dt.timestamp())
            except Exception:
                pass
        return -1

    def _enrich_magnet(self, magnet_link):
        """Append high-speed trackers to magnet links if not already present."""
        if not magnet_link or not magnet_link.startswith('magnet:'):
            return magnet_link
        
        # Collect existing trackers
        existing = set(re.findall(r'tr=([^&]+)', magnet_link))
        extra_trs = []
        for tr in self.DEFAULT_TRACKERS:
            encoded_tr = urllib.parse.quote(tr, safe='')
            if encoded_tr not in existing and tr not in existing:
                extra_trs.append(f"tr={encoded_tr}")
        
        if extra_trs:
            delimiter = '&' if '?' in magnet_link else '?'
            return f"{magnet_link}{delimiter}{'&'.join(extra_trs)}"
        return magnet_link

    def _extract_article_data(self, article_html, raw_query, fallback_url=None):
        """Parse individual article HTML and return the formatted result dict or None."""
        # 1. Skip non-game digest categories
        categories = self._re_category.findall(article_html)
        if 'Updates Digest' in categories:
            return None

        # 2. Extract title & URL
        title_match = self._re_title.search(article_html)
        if not title_match:
            return None

        desc_link = title_match.group(1) or fallback_url or ""
        raw_title = title_match.group(2)
        title = unescape(self._re_tags.sub('', raw_title)).strip()

        # 3. Filter out non-game titles
        title_lower = title.lower()
        if any(ignored in title_lower for ignored in self._ignored_titles):
            return None

        pub_date = self._parse_pub_date(article_html)

        # 4. Extract size from snippet if present
        size = "-1"
        size_match = self._re_size.search(article_html)
        if size_match:
            size = size_match.group(1)

        # 5. Extract download link (magnet URI, infohash, or fallback .torrent link)
        download_link = None
        mag_match = self._re_magnet.search(article_html)
        if mag_match:
            download_link = unescape(mag_match.group(1))
        else:
            hash_match = self._re_hash.search(article_html)
            if hash_match:
                download_link = f"magnet:?xt=urn:btih:{hash_match.group(1)}&dn={urllib.parse.quote(title)}"

        # If not found in snippet and post URL is available, fetch full post page
        if not download_link and desc_link:
            page_html, _ = self._fetch(desc_link, timeout=6)
            if page_html:
                pmag = self._re_magnet.search(page_html)
                if pmag:
                    download_link = unescape(pmag.group(1))
                else:
                    phash = self._re_hash.search(page_html)
                    if phash:
                        download_link = f"magnet:?xt=urn:btih:{phash.group(1)}&dn={urllib.parse.quote(title)}"
                    else:
                        ptorrent = self._re_torrent_url.search(page_html)
                        if ptorrent:
                            download_link = ptorrent.group(1)

                if size == "-1":
                    psize = self._re_size.search(page_html)
                    if psize:
                        size = psize.group(1)

        if not download_link:
            return None

        return {
            'link': self._enrich_magnet(download_link),
            'name': self._ensure_query_in_title(title, raw_query),
            'size': self._format_size_to_bytes(size),
            'seeds': '-1',
            'leech': '-1',
            'engine_url': self.url,
            'desc_link': desc_link,
            'pub_date': pub_date
        }

    def search(self, what, cat='all'):
        raw_query = urllib.parse.unquote(what).strip()
        seen_links = set()
        total_results = 0
        max_results_limit = 30
        max_pages_limit = 3

        page = 1
        total_pages = 1

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            while page <= total_pages and page <= max_pages_limit and total_results < max_results_limit:
                if page == 1:
                    search_url = f"{self.url}?s={urllib.parse.quote(raw_query)}"
                else:
                    search_url = f"{self.url}page/{page}/?s={urllib.parse.quote(raw_query)}"

                html, final_url = self._fetch(search_url, timeout=8)
                if not html:
                    break

                # Handle direct WordPress redirect to a single post (e.g., exact match)
                if "?s=" not in final_url:
                    if articles:
                        for art in articles:
                            res = self._extract_article_data(art, raw_query, fallback_url=final_url)
                            if res and res['desc_link'] not in seen_links:
                                if not res['desc_link']:
                                    res['desc_link'] = final_url
                                seen_links.add(res['desc_link'])
                                prettyPrinter(res)
                                total_results += 1
                    else:
                        res = self._extract_article_data(html, raw_query, fallback_url=final_url)
                        if res and res['desc_link'] not in seen_links:
                            res['desc_link'] = final_url
                            seen_links.add(final_url)
                            prettyPrinter(res)
                            total_results += 1
                    break

                pages_found = self._re_pages.findall(html)
                if pages_found:
                    total_pages = max(int(p) for p in pages_found)

                articles = self._re_articles.findall(html)
                if not articles:
                    break

                # Concurrent dispatch of parsing/fetches for current page
                futures = [
                    executor.submit(self._extract_article_data, art, raw_query)
                    for art in articles
                ]

                # Stream results immediately as each worker finishes
                for future in concurrent.futures.as_completed(futures):
                    try:
                        res = future.result()
                        if res and res['desc_link'] not in seen_links:
                            seen_links.add(res['desc_link'])
                            prettyPrinter(res)
                            total_results += 1
                            if total_results >= max_results_limit:
                                break
                    except Exception:
                        pass

                page += 1
