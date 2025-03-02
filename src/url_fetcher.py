import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

class URLFetcher:
    """Fetches webpage titles and metadata for URLs posted in IRC channels."""

    def __init__(self, bot):
        """Initialize with a reference to the IRC bot to send messages back."""
        self.bot = bot
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    def detect_and_fetch(self, nick, channel, message):
        """Detects URLs in messages and fetches their titles."""
        urls = self.extract_urls(message)
        if not urls:
            return  # No URLs found, do nothing

        for url in urls:
            title_info = self.get_title(url)
            if title_info:
                self.bot.send_message(channel, title_info)  # Send title to IRC

    def extract_urls(self, text):
        """Extracts valid URLs from a message using a more refined regex."""
        url_pattern = re.compile(r"https?://[^\s<>\"']+")
        return url_pattern.findall(text)

    def get_title(self, url):
        """Fetches the title of a webpage, handling known services separately."""
        domain = urlparse(url).netloc.lower()

        try:
            if "youtube.com" in domain or "youtu.be" in domain:
                return self.get_youtube_info(url)
            elif "instagram.com" in domain:
                return self.get_instagram_title(url)
            elif "x.com" in domain or "twitter.com" in domain:
                return None  # Skip fetching titles for X (Twitter) links
            elif "reddit.com" in domain:
                return self.get_reddit_title(url)
            else:
                return self.get_generic_title(url)
        except Exception as e:
            return f"Error fetching title: {e}"

    def get_generic_title(self, url):
        """Fetches the title of a standard web page, preferring Open Graph metadata."""
        try:
            response = self.session.get(url, timeout=5)
            response.raise_for_status()

            # Handle encoding issues
            response.encoding = response.apparent_encoding

            soup = BeautifulSoup(response.text, "html.parser")

            # Prefer Open Graph title if available
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                return og_title["content"].strip()

            # Otherwise, fall back to the standard HTML title
            if soup.title and soup.title.string:
                return soup.title.string.strip()

            return None  # No title found, return nothing

        except requests.exceptions.Timeout:
            return "Error: The request timed out."
        except requests.exceptions.ConnectionError:
            return "Error: Could not connect to the server."
        except requests.exceptions.HTTPError as e:
            return f"Error: HTTP {response.status_code}"
        except requests.exceptions.RequestException:
            return "Error: Failed to fetch webpage."
        except Exception:
            return "Error: Unexpected error while fetching the title."

        
    def get_youtube_info(self, url):
        """Fetches the title and uploader name for YouTube videos using the oEmbed API."""
        try:
            video_id = None
            parsed_url = urlparse(url)
            if "youtube.com" in parsed_url.netloc:
                query = parse_qs(parsed_url.query)
                video_id = query.get("v", [None])[0]
            elif "youtu.be" in parsed_url.netloc:
                video_id = parsed_url.path.lstrip("/")

            if not video_id:
                return "Error: Invalid YouTube URL."

            api_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            response = self.session.get(api_url, timeout=5)
            response.raise_for_status()

            data = response.json()
            return f"YouTube: {data['title']} (by {data['author_name']})"

        except requests.exceptions.RequestException:
            return "Error: Could not retrieve YouTube video details."
        except Exception:
            return "Error: Unexpected issue while fetching YouTube info."

    def get_instagram_title(self, url):
        """Fetches the title of an Instagram post (fallback to generic since Instagram blocks scraping)."""
        return self.get_generic_title(url)

    def get_x_title(self, url):
        """Fetches the content of an X (formerly Twitter) post (fallback to generic since Twitter blocks scraping)."""
        return self.get_generic_title(url)

    def get_reddit_title(self, url):
        """Fetches the title of a Reddit post (fallback to generic since Reddit blocks scraping)."""
        return self.get_generic_title(url)
