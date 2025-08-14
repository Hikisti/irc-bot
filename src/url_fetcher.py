import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

class URLFetcher:
    """Detects URLs in IRC messages and fetches their titles with service-specific handling."""

    MAX_IRC_MESSAGE_LENGTH = 400  # Safe maximum for IRC message length
    MAX_TITLE_LENGTH = 300        # Max allowed title length to avoid excess flood kicks

    def __init__(self, bot):
        """
        Initialize the URLFetcher with a reference to the IRC bot.
        Sets up a session with a common browser user-agent to avoid being blocked.
        """
        self.bot = bot
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })

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
        url_pattern = re.compile(r"https?://[^\s<>\"']+")
        return url_pattern.findall(text)

    def get_title(self, url):
        """
        Dispatches URL to service-specific handlers, or falls back to generic.
        Skips known services (like X and Reddit) where scraping is unreliable or blocked.
        """
        domain = urlparse(url).netloc.lower()

        try:
            if "youtube.com" in domain or "youtu.be" in domain:
                return self.get_youtube_info(url)
            elif "instagram.com" in domain:
                return self.get_instagram_title(url)
            elif "x.com" in domain or "twitter.com" in domain:
                return None  # Skip X/Twitter due to scraping restrictions
            elif "reddit.com" in domain:
                return None  # Skip Reddit due to inconsistent content structure
            else:
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

        except requests.exceptions.Timeout:
            return "Error: The request timed out."
        except requests.exceptions.ConnectionError:
            return "Error: Could not connect to the server."
        except requests.exceptions.HTTPError:
            return f"Error: HTTP {response.status_code}"
        except requests.exceptions.RequestException:
            return "Error: Failed to fetch webpage."
        except Exception:
            return "Error: Unexpected error while fetching the title."

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

        except requests.exceptions.HTTPError as e:
            return f"Error: HTTP {e.response.status_code} while retrieving YouTube details."
        except requests.exceptions.Timeout:
            return "Error: YouTube request timed out."
        except requests.exceptions.ConnectionError:
            return "Error: Could not connect to YouTube."
        except requests.exceptions.RequestException:
            return "Error: Failed to fetch YouTube video details."
        except Exception:
            return "Error: Unexpected issue while fetching YouTube info."

    def get_instagram_title(self, url):
        """
        Instagram often blocks scraping.
        Fall back to generic title method, which may return a basic page title.
        """
        return self.get_generic_title(url)

    def get_x_title(self, url):
        """Fallback for X (Twitter). Currently unused due to unreliable access."""
        return self.get_generic_title(url)

    def get_reddit_title(self, url):
        """Fallback for Reddit. Currently unused due to inconsistent structure."""
        return self.get_generic_title(url)

    def trim_message(self, text):
        """
        Trims message length to avoid flooding the IRC server.
        Adds ellipsis if trimmed.
        """
        if len(text) <= self.MAX_TITLE_LENGTH:
            return text
        return text[:self.MAX_TITLE_LENGTH - 3].rstrip() + "..."
