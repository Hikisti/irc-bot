from unittest.mock import MagicMock, patch

import pytest
import requests

from url_fetcher import URLFetcher


def make_response(text="", status_code=200, apparent_encoding="utf-8"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.apparent_encoding = apparent_encoding
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


@pytest.fixture
def bot():
    return MagicMock()


@pytest.fixture
def fetcher(bot):
    return URLFetcher(bot)


class TestExtractUrls:
    def test_finds_single_url(self, fetcher):
        assert fetcher.extract_urls("check this out https://example.com/page") == [
            "https://example.com/page"
        ]

    def test_finds_multiple_urls(self, fetcher):
        text = "https://a.com and http://b.com/x"
        assert fetcher.extract_urls(text) == ["https://a.com", "http://b.com/x"]

    def test_no_url_returns_empty_list(self, fetcher):
        assert fetcher.extract_urls("no links here") == []

    def test_stops_at_whitespace_and_quotes(self, fetcher):
        text = 'link "https://example.com/page" end'
        assert fetcher.extract_urls(text) == ["https://example.com/page"]


class TestIsBlacklisted:
    @pytest.mark.parametrize("domain", ["x.com", "nettiauto.com"])
    def test_exact_blacklisted_domain(self, fetcher, domain):
        assert fetcher.is_blacklisted(domain) is True

    def test_subdomain_of_blacklisted_domain(self, fetcher):
        assert fetcher.is_blacklisted("old.reddit.com") is True

    def test_case_insensitive(self, fetcher):
        assert fetcher.is_blacklisted("X.COM") is True

    def test_non_blacklisted_domain(self, fetcher):
        assert fetcher.is_blacklisted("example.com") is False

    def test_similar_but_not_matching_domain_is_not_blacklisted(self, fetcher):
        # "notnettiauto.com" must not be treated as a subdomain of nettiauto.com
        assert fetcher.is_blacklisted("notnettiauto.com") is False


class TestGetTitle:
    def test_blacklisted_domain_returns_none_without_request(self, fetcher):
        with patch.object(fetcher.session, "get") as mock_get:
            result = fetcher.get_title("https://x.com/some/post")
        mock_get.assert_not_called()
        assert result is None

    def test_generic_page_uses_og_title(self, fetcher):
        html = '<html><head><meta property="og:title" content="OG Title"><title>Fallback</title></head></html>'
        with patch.object(fetcher.session, "get", return_value=make_response(html)):
            result = fetcher.get_title("https://example.com/page")
        assert result == "OG Title"

    def test_generic_page_falls_back_to_title_tag(self, fetcher):
        html = "<html><head><title>Plain Title</title></head></html>"
        with patch.object(fetcher.session, "get", return_value=make_response(html)):
            result = fetcher.get_title("https://example.com/page")
        assert result == "Plain Title"

    def test_generic_page_without_title_returns_none(self, fetcher):
        html = "<html><head></head></html>"
        with patch.object(fetcher.session, "get", return_value=make_response(html)):
            result = fetcher.get_title("https://example.com/page")
        assert result is None

    def test_timeout_returns_friendly_error(self, fetcher):
        with patch.object(fetcher.session, "get", side_effect=requests.exceptions.Timeout):
            result = fetcher.get_title("https://example.com/page")
        assert "timed out" in result

    def test_connection_error_returns_friendly_error(self, fetcher):
        with patch.object(
            fetcher.session, "get", side_effect=requests.exceptions.ConnectionError
        ):
            result = fetcher.get_title("https://example.com/page")
        assert "Could not connect" in result

    def test_youtube_url_uses_oembed(self, fetcher):
        oembed_data = {"title": "Cool Video", "author_name": "Some Channel"}
        with patch.object(fetcher.session, "get", return_value=make_response()) as mock_get:
            mock_get.return_value.json.return_value = oembed_data
            result = fetcher.get_title("https://www.youtube.com/watch?v=abc123")
        assert result == "YouTube: Cool Video (by Some Channel)"

    def test_youtube_shorts_url_extracts_video_id(self, fetcher):
        with patch.object(fetcher.session, "get") as mock_get:
            mock_get.return_value = make_response()
            mock_get.return_value.json.return_value = {"title": "T", "author_name": "A"}
            fetcher.get_title("https://www.youtube.com/shorts/xyz789")
        called_url = mock_get.call_args[0][0]
        assert "v=xyz789" in called_url

    def test_youtube_invalid_url_returns_error(self, fetcher):
        result = fetcher.get_title("https://www.youtube.com/watch")
        assert "Invalid YouTube URL" in result

    def test_youtu_be_short_link_uses_oembed(self, fetcher):
        with patch.object(fetcher.session, "get") as mock_get:
            mock_get.return_value = make_response()
            mock_get.return_value.json.return_value = {"title": "T", "author_name": "A"}
            result = fetcher.get_title("https://youtu.be/xyz789")
        called_url = mock_get.call_args[0][0]
        assert "v=xyz789" in called_url
        assert result == "YouTube: T (by A)"

    def test_domain_containing_youtube_as_a_substring_is_not_treated_as_youtube(self, fetcher):
        # Regression test: "youtube.com" in domain (a plain substring
        # check) would also match "notyoutube.com" - must require an
        # exact match or a real subdomain, like is_blacklisted() already
        # does for its own domain list.
        html = "<html><head><title>Not YouTube At All</title></head></html>"
        with patch.object(fetcher.session, "get", return_value=make_response(html)) as mock_get:
            result = fetcher.get_title("https://notyoutube.com/watch?v=abc123")
        assert result == "Not YouTube At All"
        assert "oembed" not in mock_get.call_args[0][0]

    def test_youtube_oembed_request_failure_returns_friendly_error(self, fetcher):
        with patch.object(fetcher.session, "get", side_effect=requests.exceptions.Timeout):
            result = fetcher.get_title("https://www.youtube.com/watch?v=abc123")
        assert "timed out" in result

    def test_youtube_oembed_invalid_json_is_not_a_contact_failure(self, fetcher):
        # Regression test: requests.exceptions.JSONDecodeError (raised by
        # response.json() on a non-JSON body) is also a RequestException,
        # so without format_request_error's dedicated branch this used to
        # be misreported as "Failed to contact YouTube" even though the
        # server did respond.
        resp = make_response()
        resp.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "not json", 0)
        with patch.object(fetcher.session, "get", return_value=resp):
            result = fetcher.get_title("https://www.youtube.com/watch?v=abc123")
        assert result == "Error: Invalid response from YouTube."

    def test_unexpected_exception_in_dispatch_returns_error_message(self, fetcher):
        # get_title()'s own catch-all, distinct from get_generic_title's/
        # get_youtube_info's own (format_request_error-based) handling -
        # this one guards the dispatch/blacklist logic itself.
        with patch.object(fetcher, "get_generic_title", side_effect=RuntimeError("boom")):
            result = fetcher.get_title("https://example.com/page")
        assert "Error fetching title: boom" in result


class TestDetectAndFetch:
    def test_sends_title_for_each_url(self, fetcher, bot):
        html = "<html><head><title>Some Page</title></head></html>"
        with patch.object(fetcher.session, "get", return_value=make_response(html)):
            fetcher.detect_and_fetch("nick", "#chan", "check https://example.com/page")
        bot.send_message.assert_called_once_with("#chan", "Some Page")

    def test_no_url_does_not_send_message(self, fetcher, bot):
        fetcher.detect_and_fetch("nick", "#chan", "no links here")
        bot.send_message.assert_not_called()

    def test_blacklisted_url_does_not_send_message(self, fetcher, bot):
        with patch.object(fetcher.session, "get") as mock_get:
            fetcher.detect_and_fetch("nick", "#chan", "https://x.com/post")
        mock_get.assert_not_called()
        bot.send_message.assert_not_called()


class TestTrimMessage:
    def test_short_message_unchanged(self, fetcher):
        assert fetcher.trim_message("short") == "short"

    def test_long_message_is_trimmed_with_ellipsis(self, fetcher):
        text = "a" * 400
        result = fetcher.trim_message(text)
        assert len(result) == fetcher.MAX_TITLE_LENGTH
        assert result.endswith("...")
