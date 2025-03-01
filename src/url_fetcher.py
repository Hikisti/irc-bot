import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

class URLFetcher:
    MAX_IRC_MSG_LENGTH = 400  # Adjust based on IRC limits

    def __init__(self, bot):
        """Initialize with a reference to the IRC bot to send messages back to channels."""
        self.bot = bot
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    def detect_and_fetch(self, nick, channel, message):
        """Detects URLs in the message and fetches their titles."""
        urls = self.extract_urls(message)
        if not urls:
            return  # No URLs, do nothing
        
        for url in urls:
            title_info = self.get_title(url)
            if title_info:
                self.bot.send_message(channel, title_info)  # Send title to IRC

    def extract_urls(self, text):
        """Extracts URLs from a message using regex."""
        url_pattern = re.compile(r'https?://\S+')
        return url_pattern.findall(text)

    def get_title(self, url):
        """Fetches the title of a webpage, with special handling for known services."""
        domain = urlparse(url).netloc.lower()

        if "youtube.com" in domain or "youtu.be" in domain:
            return self.get_youtube_info(url)
        elif "instagram.com" in domain:
            return self.get_instagram_title(url)
        elif "x.com" in domain or "twitter.com" in domain:
            return self.get_x_title(url)
        elif "reddit.com" in domain:
            return self.get_reddit_title(url)
        else:
            return self.get_generic_title(url)

    def get_generic_title(self, url):
        """Fetches the title of a standard web page."""
        try:
            response = self.session.get(url, timeout=5)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.string.strip() if soup.title else "No title found"
            return title[:URLFetcher.MAX_IRC_MSG_LENGTH]
        except requests.exceptions.RequestException:
            return None

    def get_youtube_info(self, url):
        """Fetches the title and uploader name for YouTube videos."""
        try:
            video_id = None
            if "youtube.com" in url:
                query = parse_qs(urlparse(url).query)
                video_id = query.get("v", [None])[0]
            elif "youtu.be" in url:
                video_id = urlparse(url).path.lstrip('/')
            
            if not video_id:
                return None
            
            api_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            response = self.session.get(api_url, timeout=5)
            response.raise_for_status()
            data = response.json()
            return f"YouTube: {data['title']} (by {data['author_name']})"
        except requests.exceptions.RequestException:
            return None

    def get_instagram_title(self, url):
        """Fetches the title of an Instagram post."""
        return self.get_generic_title(url)  # Instagram blocks scraping, fallback to page title

    def get_x_title(self, url):
        """Fetches the content of an X (formerly Twitter) post."""
        return self.get_generic_title(url)  # Twitter blocks scraping, fallback to page title

    def get_reddit_title(self, url):
        """Fetches the title of a Reddit post."""
        return self.get_generic_title(url)  # Reddit blocks scraping, fallback to page title
