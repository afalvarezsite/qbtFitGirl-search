# VERSION: 1.0
# AUTHORS: Spidy
import concurrent.futures
import re
import urllib.parse
import urllib.request
from datetime import datetime
from html import unescape

try:
    from helpers import retrieve_url
except ImportError:
    retrieve_url = None

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

    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    }

    # Pre-compiled regular expressions
    _re_pages = re.compile(r'page-numbers[^"]*"[^\n>]*>(\d+)</a>')
    _re_articles = re.compile(r'<article id="post-\d+".*?</article>', re.DOTALL)
    _re_title = re.compile(r'<h1 class="entry-title"><a href="([^"]+)"[^>]*>(.*?)</a></h1>')
    _re_tags = re.compile(r'<[^>]+>')
    _re_category = re.compile(r'rel="category tag">(.*?)</a>')
    _re_date = re.compile(r'<time class="entry-date[^"]*" datetime="([^"]+)"')
    _re_magnet = re.compile(r'href="(magnet:\?[^"]+)"')
    _re_torrent_url = re.compile(r'href="([^"]+?\.(?:torrent))"')
    _re_size = re.compile(r'Repack Size[^\d]*?(?:from\s*)?(\d+(?:\.\d+)?\s*(?:TB|GB|MB|KB))', re.IGNORECASE)
    _re_clean_alpha = re.compile(r'[^a-zA-Z0-9]')
    _re_size_units = re.compile(r'([\d\.]+)\s*(TB|GB|MB|KB|B)', re.IGNORECASE)

    _ignored_titles = ("updates digest", "updates list", "faq", "donate", "day of requests")

    def _safe_decode(self, data):
        if isinstance(data, bytes):
            return data.decode('utf-8', errors='ignore')
        return str(data) if data is not None else ""

    def _fetch(self, target_url, timeout=8):
        """Fetch URL content using qBittorrent helper or urllib fallback."""
        if retrieve_url:
            try:
                res = retrieve_url(target_url)
                if res:
                    return self._safe_decode(res)
            except Exception:
                pass

        try:
            req = urllib.request.Request(target_url, headers=self.headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return self._safe_decode(resp.read())
        except Exception:
            return ""

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

    def _extract_article_data(self, article_html, raw_query):
        """Parse individual article HTML and return the formatted result dict or None."""
        # 1. Skip non-game digest categories
        categories = self._re_category.findall(article_html)
        if 'Updates Digest' in categories:
            return None

        # 2. Extract title & URL
        title_match = self._re_title.search(article_html)
        if not title_match:
            return None

        desc_link = title_match.group(1)
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

        # 5. Extract download link (magnet URI or fallback .torrent link)
        download_link = None
        mag_match = self._re_magnet.search(article_html)
        if mag_match:
            download_link = unescape(mag_match.group(1))
        else:
            page_html = self._fetch(desc_link, timeout=6)
            if page_html:
                pmag = self._re_magnet.search(page_html)
                if pmag:
                    download_link = unescape(pmag.group(1))
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
            'link': download_link,
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
        max_pages_limit = 5

        page = 1
        total_pages = 1

        while page <= total_pages and page <= max_pages_limit and total_results < max_results_limit:
            if page == 1:
                search_url = f"{self.url}?s={urllib.parse.quote(raw_query)}"
            else:
                search_url = f"{self.url}page/{page}/?s={urllib.parse.quote(raw_query)}"

            html = self._fetch(search_url, timeout=8)
            if not html:
                break

            # Detect total page count from pagination
            pages_found = self._re_pages.findall(html)
            if pages_found:
                total_pages = max(int(p) for p in pages_found)

            articles = self._re_articles.findall(html)
            if not articles:
                break

            # Process articles in parallel per page to speed up detail fetches
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                futures = [
                    executor.submit(self._extract_article_data, art, raw_query)
                    for art in articles
                ]
                for future in futures:
                    try:
                        res = future.result()
                        if res and res['desc_link'] not in seen_links:
                            seen_links.add(res['desc_link'])
                            prettyPrinter(res)
                            total_results += 1
                    except Exception:
                        pass

            page += 1
