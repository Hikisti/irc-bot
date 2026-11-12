import codecs
import ipaddress
import queue
import re
import socket
import threading
import time
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from urllib3.exceptions import LocationParseError
from urllib3.util import parse_url

from http_session import DEFAULT_TIMEOUT_SECONDS, make_session
from request_errors import format_request_error


class _RefusedURL(Exception):
    """A link the bot will not fetch (a non-public address, or not http(s))."""


class URLFetcher:
    """Detects URLs in IRC messages and fetches their titles with service-specific handling."""

    MAX_TITLE_LENGTH = 300        # Max allowed title length to avoid excess flood kicks
    # An IRC line is at most 512 bytes with the command and channel; a title of four-byte characters
    # (emoji) can pass that at 300 characters. The same figure as the scoreboard replies' line limit.
    MAX_TITLE_BYTES = 380

    URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")

    # Domains that should be ignored (no title fetching)
    BLACKLISTED_DOMAINS = {
        "maps.google.com",
        "maps.app.goo.gl",
        "x.com",
        "twitter.com",
        "reddit.com",
        "nettiauto.com"
    }

    # Wikimedia sites throttle by User-Agent and address: the generic browser string below, sent from the
    # server's datacenter address, got "HTTP 403 Too many requests. Please respect our robot policy" for an
    # ordinary article link (issue #40). Their policy asks bots for an agent that says who they are and where
    # to read about them, so these domains get one; every other site keeps the browser string, which some
    # sites require.
    WIKIMEDIA_DOMAINS = (
        "wikipedia.org", "wikimedia.org", "wikidata.org", "wiktionary.org", "wikibooks.org", "wikiquote.org",
        "wikisource.org", "wikinews.org", "wikiversity.org", "wikivoyage.org", "mediawiki.org",
    )
    WIKIMEDIA_USER_AGENT = "KukistiBot/1.0 (+https://github.com/Hikisti/irc-bot; IRC link-title bot)"

    # A link anyone in a channel can post is fetched from the server, so the fetch is limited: it
    # only goes to public addresses (a title would otherwise be a window onto services on the server
    # itself or its network, and the answer a port scanner), and it reads a bounded amount in a bounded
    # time (a huge file or a server that drips bytes would otherwise tie up the thread and the memory
    # of a small machine). Titles sit at the top of a page, so the cap costs nothing real.
    MAX_PAGE_BYTES = 2 * 1024 * 1024
    MAX_FETCH_SECONDS = 10
    MAX_REDIRECTS = 5
    REDIRECT_STATUSES = (301, 302, 303, 307, 308)

    # Titles are fetched on a worker thread, never on the thread that reads the IRC socket: a slow page there
    # stopped every channel and the answers to the server's PING until the server dropped the bot (#83).
    # MAX_FETCH_SECONDS only bounds the body between chunks; the lookup, the hops and a body dripped below a
    # chunk are not, so each link also gets a hard wall-clock budget. A fetch that overruns it is left to
    # finish on its own thread and its title is never posted; while MAX_ABANDONED_FETCHES of those are still
    # running, further links are skipped, so a hostile site cannot pile up threads.
    MAX_URLS_PER_MESSAGE = 3
    LINK_BUDGET_SECONDS = 20
    MAX_PENDING_MESSAGES = 5
    MAX_ABANDONED_FETCHES = 3

    def __init__(self, bot):
        """
        Initialize the URLFetcher with a reference to the IRC bot.
        Sets up a session with a common browser user-agent to avoid being blocked.
        """
        self.bot = bot
        self.session = make_session("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", accept_json=False)
        self._pending = queue.Queue(maxsize=self.MAX_PENDING_MESSAGES)
        self._worker = None
        self._abandoned = []  # fetches that overran LINK_BUDGET_SECONDS and are still running

    def submit(self, nick, channel, message):
        """Called on the listener thread: hands a message with links to the worker and returns at once."""
        if not self.extract_urls(message):
            return
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="url-titles", daemon=True)
            self._worker.start()
        try:
            self._pending.put_nowait((nick, channel, message))
        except queue.Full:
            print(f"URL title: busy, skipping the links of a message in {channel}")

    def _work(self):
        while True:
            nick, channel, message = self._pending.get()
            try:
                self.detect_and_fetch(nick, channel, message)
            finally:
                self._pending.task_done()

    def detect_and_fetch(self, nick, channel, message):
        """Fetches the title of each link in `message` (at most MAX_URLS_PER_MESSAGE) and posts it."""
        for url in self.extract_urls(message)[:self.MAX_URLS_PER_MESSAGE]:
            title_info = self._title_within_budget(url)
            if title_info:
                self.bot.send_message(channel, self.trim_message(title_info))

    def _title_within_budget(self, url):
        """get_title(url) if it finishes within LINK_BUDGET_SECONDS, else None (see MAX_ABANDONED_FETCHES)."""
        self._abandoned = [t for t in self._abandoned if t.is_alive()]
        if len(self._abandoned) >= self.MAX_ABANDONED_FETCHES:
            print(f"URL title: skipping a link, {len(self._abandoned)} slow fetches still running")
            return None
        result = {}

        def fetch():
            # Anyone in the channel chooses this text: an exception here fails this link, nothing else.
            try:
                result["title"] = self.get_title(url)
            except Exception as e:
                print(f"URL title: failed: {type(e).__name__}")

        thread = threading.Thread(target=fetch, name="url-title-fetch", daemon=True)
        thread.start()
        thread.join(self.LINK_BUDGET_SECONDS)
        if thread.is_alive():
            self._abandoned.append(thread)
            print(f"URL title: gave up after {self.LINK_BUDGET_SECONDS} s on {self._host_for_log(url)}")
            return None
        return result.get("title")

    def _host_for_log(self, url):
        try:
            return urlparse(url).netloc.lower()
        except ValueError:
            return "an address that does not parse"

    def extract_urls(self, text):
        """
        Extracts valid HTTP/HTTPS URLs using regex.
        This avoids issues with malformed or weird characters.
        """
        return self.URL_PATTERN.findall(text)

    def _matches_domain(self, domain: str, candidate: str) -> bool:
        """True if `domain` is exactly `candidate` or one of its
        subdomains - not just a substring match, which would also treat
        e.g. "notyoutube.com" as youtube.com."""
        return domain == candidate or domain.endswith("." + candidate)

    def is_blacklisted(self, domain: str) -> bool:
        """
        Returns True if the domain is blacklisted.
        Matches both exact domains and their subdomains.
        """
        domain = domain.lower()
        return any(self._matches_domain(domain, blocked) for blocked in self.BLACKLISTED_DOMAINS)

    def get_title(self, url):
        """
        Dispatches URL to service-specific handlers, or falls back to generic.
        Skips known services (like X and Reddit) where scraping is unreliable or blocked.
        Also skips any URL whose domain is in the blacklist.
        """
        try:
            domain = urlparse(url).netloc.lower()
        except ValueError as e:  # "http://[" and the like: not an address at all
            print(f"URL title: not fetching an address that does not parse ({e})")
            return None

        # Check blacklist first
        if self.is_blacklisted(domain):
            return None  # Silently skip blacklisted domains

        try:
            if self._matches_domain(domain, "youtube.com") or self._matches_domain(domain, "youtu.be"):
                return self.get_youtube_info(url)
            # Instagram often blocks scraping - falling through to the
            # generic handler (which may return a basic page title) is
            # already the whole "Instagram-specific" behavior, so there's
            # no separate method for it.
            return self.get_generic_title(url)
        except Exception as e:
            return self._no_title(url, f"{type(e).__name__}: {e}")

    def _no_title(self, url, reason):
        """Nobody asked for a title, so a page that gives none is not answered in the channel: the reason goes
        to the journal (the domain only) and the caller posts nothing."""
        print(f"URL title: none for {urlparse(url).netloc.lower()}: {reason.removeprefix('Error: ')}")
        return None

    def get_generic_title(self, url):
        """
        Fetches a title from a regular webpage.
        Tries Open Graph <meta property="og:title"> first, falls back to <title>.
        Ensures encoding is properly handled.
        """
        try:
            domain = urlparse(url).netloc.lower()
            wikimedia = any(self._matches_domain(domain, d) for d in self.WIKIMEDIA_DOMAINS)
            headers = {"User-Agent": self.WIKIMEDIA_USER_AGENT} if wikimedia else None
            response = self._fetch_page(url, headers)

            soup = BeautifulSoup(self._page_text(response), "html.parser")

            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                return og_title["content"].strip()

            if soup.title and soup.title.string:
                return soup.title.string.strip()

            return None  # No usable title found

        except _RefusedURL as e:
            print(f"URL title: not fetching {e}")
            return None  # like a blacklisted domain: nothing is said in the channel
        except Exception as e:
            return self._no_title(url, format_request_error(e, "the webpage"))

    def _resolve(self, host):
        """The IP addresses `host` resolves to (a literal address resolves to itself)."""
        return {info[4][0] for info in socket.getaddrinfo(host, None)}

    def _is_public_host(self, host) -> bool:
        """False if `host` is, or resolves to, a loopback, private, link-local or other non-public
        address (any one of several addresses is enough to refuse). A name that does not resolve is
        let through: the request then fails with its own, ordinary error."""
        try:
            addresses = self._resolve(host)
        except OSError:
            return True
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%")[0])
            ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:127.0.0.1 is 127.0.0.1
            if not ip.is_global:
                return False
        return True

    def _connect_host(self, url):
        """The host requests will actually connect to, read with the parser requests itself uses (urllib3),
        or None if it cannot read one. The standard library reads some addresses differently:
        "http://127.0.0.1:8080\\@example.com/" is example.com to urlparse but 127.0.0.1 to urllib3, so
        checking urlparse's host would let the request reach an address the check never saw."""
        try:
            host = parse_url(url).host
        except LocationParseError:
            return None
        return host.strip("[]") if host else None

    def _fetch_page(self, url, headers):
        """The page at `url` as a response whose body is at most MAX_PAGE_BYTES, read for at most
        MAX_FETCH_SECONDS. Redirects are followed by hand so that every hop is checked, not only the
        first: a public address can redirect to an internal one."""
        deadline = time.monotonic() + self.MAX_FETCH_SECONDS
        for _ in range(self.MAX_REDIRECTS + 1):
            parsed = urlparse(url)
            host = self._connect_host(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or not host:
                raise _RefusedURL(f"{url[:80]} (not an http(s) address)")
            if not self._is_public_host(host):
                raise _RefusedURL(f"{host} (not a public address)")
            response = self.session.get(
                url, headers=headers, timeout=DEFAULT_TIMEOUT_SECONDS, allow_redirects=False, stream=True
            )
            location = response.headers.get("location")
            if response.status_code in self.REDIRECT_STATUSES and location:
                response.close()
                url = urljoin(url, location)
                continue
            try:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=16384):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= self.MAX_PAGE_BYTES or time.monotonic() > deadline:
                        break
            finally:
                response.close()
            # The rest of the code reads response.content/.text; hand it the bounded body.
            response._content = b"".join(chunks)[:self.MAX_PAGE_BYTES]
            response._content_consumed = True
            return response
        raise requests.exceptions.TooManyRedirects(f"more than {self.MAX_REDIRECTS} redirects")

    CHARSET_PATTERN = re.compile(r"charset=([^;\s]+)", re.IGNORECASE)
    # Single-byte charsets that servers send by default or by mistake for pages that are in fact UTF-8.
    SINGLE_BYTE_CHARSETS = ("iso-8859-1", "latin-1", "latin1", "windows-1252", "cp1252", "us-ascii", "ascii")

    def _page_text(self, response) -> str:
        """The page decoded the way its server says, not the way a guess
        says: the statistical guess (`apparent_encoding`) read a UTF-8 Yle
        page as mac_greek and turned every ä/ö into Greek letters (issue
        #24). With no declared charset, UTF-8 if the bytes are valid UTF-8
        (requests would otherwise default to ISO-8859-1), and only then
        the guess - the case it was added for. One exception to trusting
        the declaration: a default single-byte charset (ISO-8859-1 and
        its kin) on bytes that are valid UTF-8 is a UTF-8 page labelled
        wrongly - non-ASCII text in a single-byte charset is practically
        never valid UTF-8 - and reading it as declared would double every
        ä/ö."""
        declared = self.CHARSET_PATTERN.search(response.headers.get("content-type", ""))
        if declared:
            name = declared.group(1).strip("\"'")
            if name.lower() in self.SINGLE_BYTE_CHARSETS:
                try:
                    text = response.content.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
                if text is not None:
                    return text
            try:
                codecs.lookup(name)
            except LookupError:
                print(f"URL title: unknown charset {name!r} declared, decoding with replacement characters")
            response.encoding = name
            return response.text
        try:
            return response.content.decode("utf-8")
        except UnicodeDecodeError:
            response.encoding = response.apparent_encoding
            return response.text

    def get_youtube_info(self, url):
        """
        Handles both regular YouTube and Shorts URLs.
        Uses YouTube's oEmbed API to fetch video title and author safely.
        """
        try:
            video_id = None
            parsed_url = urlparse(url)

            if "youtube.com" in parsed_url.netloc:
                if "/shorts/" in parsed_url.path:
                    video_id = parsed_url.path.split("/shorts/")[1]
                else:
                    query = parse_qs(parsed_url.query)
                    video_id = query.get("v", [None])[0]
            elif "youtu.be" in parsed_url.netloc:
                video_id = parsed_url.path.lstrip('/')

            if not video_id:
                return self._no_title(url, "Invalid YouTube URL.")

            api_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            response = self.session.get(api_url, timeout=DEFAULT_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()

            return f"YouTube: {data['title']} (by {data['author_name']})"

        except Exception as e:
            return self._no_title(url, format_request_error(e, "YouTube"))

    def trim_message(self, text):
        """
        Trims message length to avoid flooding the IRC server.
        Adds ellipsis if trimmed.
        """
        if len(text) <= self.MAX_TITLE_LENGTH and len(text.encode("utf-8")) <= self.MAX_TITLE_BYTES:
            return text
        text = text[:self.MAX_TITLE_LENGTH - 3]
        # Cut on a character boundary: whatever half-character the byte cut leaves is dropped.
        text = text.encode("utf-8")[:self.MAX_TITLE_BYTES - 3].decode("utf-8", errors="ignore")
        return text.rstrip() + "..."
