import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

from http_session import make_session
from request_errors import format_request_error


class URLFetcher:
    """Detects URLs in IRC messages and fetches their titles with service-specific handling."""

    MAX_TITLE_LENGTH = 300        # Max allowed title length to avoid excess flood kicks

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

    def __init__(self, bot):
        """
        Initialize the URLFetcher with a reference to the IRC bot.
        Sets up a session with a common browser user-agent to avoid being blocked.
        """
        self.bot = bot
        self.session = make_session("Mozilla/5.0 (Windows NT 10.0; Win64; x64)", accept_json=False)

    def detect_and_fetch(self, nick, channel, message):
        """
        Extracts URLs from a message and fetches their titles.
        Sends shortened titles back to the IRC channel to avoid flooding.
        """
        urls = self.extract_urls(message)
        if not urls:
            return  # No URLs found in the message

        for url in urls:
            title_info = self.get_title(url)
            if title_info:
                safe_msg = self.trim_message(title_info)
                self.bot.send_message(channel, safe_msg)

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
        domain = urlparse(url).netloc.lower()

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
            return f"Error fetching title: {e}"

    def get_generic_title(self, url):
        """
        Fetches a title from a regular webpage.
        Tries Open Graph <meta property="og:title"> first, falls back to <title>.
        Ensures encoding is properly handled.
        """
        try:
            response = self.session.get(url, timeout=5)
            response.raise_for_status()

            response.encoding = response.apparent_encoding
            soup = BeautifulSoup(response.text, "html.parser")

            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                return og_title["content"].strip()

            if soup.title and soup.title.string:
                return soup.title.string.strip()

            return None  # No usable title found

        except Exception as e:
            return format_request_error(e, "the webpage")

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
                return "Error: Invalid YouTube URL."

            api_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            response = self.session.get(api_url, timeout=5)
            response.raise_for_status()
            data = response.json()

            return f"YouTube: {data['title']} (by {data['author_name']})"

        except Exception as e:
            return format_request_error(e, "YouTube")

    def trim_message(self, text):
        """
        Trims message length to avoid flooding the IRC server.
        Adds ellipsis if trimmed.
        """
        if len(text) <= self.MAX_TITLE_LENGTH:
            return text
        return text[:self.MAX_TITLE_LENGTH - 3].rstrip() + "..."
