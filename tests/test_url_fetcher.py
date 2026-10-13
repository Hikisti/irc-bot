from unittest.mock import MagicMock, patch

import pytest
import requests

from url_fetcher import URLFetcher

REAL_RESOLVE = URLFetcher._resolve  # the autouse public_dns fixture replaces it for every test


def make_response(text="", status_code=200, apparent_encoding="utf-8"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.apparent_encoding = apparent_encoding
    resp.content = text.encode("utf-8")
    resp.headers = {"content-type": "text/html; charset=utf-8"}
    resp.raise_for_status = MagicMock()
    resp.iter_content = lambda chunk_size=1: iter([resp.content])
    resp.close = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


@pytest.fixture
def bot():
    return MagicMock()


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """No test resolves a name for real: every host is a public address unless a test says otherwise."""
    monkeypatch.setattr(URLFetcher, "_resolve", lambda self, host: {"93.184.216.34"})


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

    @pytest.mark.parametrize("url", [
        "https://fi.wikipedia.org/wiki/Suomi",
        "https://en.wikipedia.org/wiki/Finland?wprov=sfla1",
        "https://commons.wikimedia.org/wiki/File:X.jpg",
        "https://www.wikidata.org/wiki/Q33",
    ])
    def test_wikimedia_links_send_a_descriptive_user_agent(self, fetcher, url):
        html = "<html><head><title>Suomi</title></head></html>"
        with patch.object(fetcher.session, "get", return_value=make_response(html)) as mock_get:
            assert fetcher.get_generic_title(url) == "Suomi"
        agent = mock_get.call_args.kwargs["headers"]["User-Agent"]
        assert agent == URLFetcher.WIKIMEDIA_USER_AGENT
        assert "KukistiBot" in agent and "github.com" in agent

    @pytest.mark.parametrize("url", [
        "https://example.com/wiki/Suomi",
        "https://notwikipedia.org/wiki/Suomi",
        "https://wikipedia.org.example.com/wiki/Suomi",
    ])
    def test_other_links_keep_the_browser_user_agent(self, fetcher, url):
        html = "<html><head><title>Suomi</title></head></html>"
        with patch.object(fetcher.session, "get", return_value=make_response(html)) as mock_get:
            fetcher.get_generic_title(url)
        assert mock_get.call_args.kwargs["headers"] is None
        assert fetcher.session.headers["User-Agent"].startswith("Mozilla/5.0")

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


def real_response(body: bytes, content_type=None):
    """A real requests.Response, so the charset handling under test is
    requests' own and not a mock's."""
    resp = requests.Response()
    resp.status_code = 200
    resp._content = body
    resp._content_consumed = True  # as after a real read; close() then has no connection to release
    if content_type:
        resp.headers["content-type"] = content_type
    return resp


def resolving_to(mapping):
    """A _resolve() replacement: host -> the addresses it resolves to."""
    def resolve(self, host):
        return set(mapping[host]) if host in mapping else {"93.184.216.34"}
    return resolve


def redirect(location, status=302):
    resp = make_response("", status_code=status)
    resp.headers = {"location": location}
    resp.raise_for_status = MagicMock()  # a redirect is not an error
    return resp


class TestOnlyPublicAddressesAreFetched:
    """A link anyone can post must not make the bot read services on its own machine or network."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1/", "http://127.0.0.1:8080/admin", "http://localhost/", "http://0.0.0.0/",
        "http://10.1.2.3/", "http://192.168.0.10/", "http://172.16.5.5/", "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/", "http://[::1]/", "http://[fe80::1]/", "http://[fc00::1]/", "http://[::ffff:127.0.0.1]/",
    ])
    def test_non_public_addresses_are_never_requested(self, fetcher, url):
        # No fixture override here: _resolve() is the real one, a literal address resolves to itself;
        # "localhost" is the one name resolved for real, and always to a loopback address.
        with patch.object(URLFetcher, "_resolve", REAL_RESOLVE), \
                patch.object(fetcher.session, "get") as mock_get:
            assert fetcher.get_title(url) is None  # nothing is said in the channel
        mock_get.assert_not_called()

    def test_a_name_that_resolves_to_a_private_address_is_refused(self, fetcher, monkeypatch):
        monkeypatch.setattr(URLFetcher, "_resolve", resolving_to({"intranet.example": ["10.0.0.7"]}))
        with patch.object(fetcher.session, "get") as mock_get:
            assert fetcher.get_title("https://intranet.example/page") is None
        mock_get.assert_not_called()

    def test_one_private_address_among_public_ones_is_enough_to_refuse(self, fetcher, monkeypatch):
        monkeypatch.setattr(URLFetcher, "_resolve", resolving_to({"mixed.example": ["93.184.216.34", "127.0.0.1"]}))
        with patch.object(fetcher.session, "get") as mock_get:
            assert fetcher.get_title("https://mixed.example/") is None
        mock_get.assert_not_called()

    def test_a_refusal_is_journaled_without_the_path(self, fetcher, capsys):
        with patch.object(URLFetcher, "_resolve", REAL_RESOLVE):
            fetcher.get_title("http://127.0.0.1/secret?token=abc")
        out = capsys.readouterr().out
        assert "127.0.0.1" in out and "token" not in out

    def test_a_public_address_is_fetched_as_before(self, fetcher):
        html = "<html><head><title>Hello</title></head></html>"
        with patch.object(fetcher.session, "get", return_value=make_response(html)) as mock_get:
            assert fetcher.get_title("https://example.com/") == "Hello"
        assert mock_get.call_args.kwargs["allow_redirects"] is False

    def test_a_name_that_does_not_resolve_fails_with_the_ordinary_error(self, fetcher, monkeypatch):
        def no_such_host(self, host):
            raise OSError("Name or service not known")
        monkeypatch.setattr(URLFetcher, "_resolve", no_such_host)
        with patch.object(fetcher.session, "get", side_effect=requests.exceptions.ConnectionError):
            assert fetcher.get_title("https://no-such-host.example/") == "Error: Could not connect to the webpage."


class TestRedirects:
    def test_a_redirect_to_a_public_page_is_followed(self, fetcher):
        html = "<html><head><title>Final</title></head></html>"
        responses = [redirect("https://example.org/next"), make_response(html)]
        with patch.object(fetcher.session, "get", side_effect=responses) as mock_get:
            assert fetcher.get_title("https://example.com/") == "Final"
        assert [c.args[0] for c in mock_get.call_args_list] == ["https://example.com/", "https://example.org/next"]

    def test_a_relative_redirect_is_resolved_against_the_current_address(self, fetcher):
        html = "<html><head><title>Final</title></head></html>"
        responses = [redirect("/moved", 301), make_response(html)]
        with patch.object(fetcher.session, "get", side_effect=responses) as mock_get:
            fetcher.get_title("https://example.com/old")
        assert mock_get.call_args_list[1].args[0] == "https://example.com/moved"

    def test_a_redirect_to_an_internal_address_is_refused_at_that_hop(self, fetcher, monkeypatch):
        monkeypatch.setattr(URLFetcher, "_resolve", resolving_to({"metadata.internal": ["169.254.169.254"]}))
        with patch.object(fetcher.session, "get", side_effect=[redirect("http://metadata.internal/latest")]) as mock_get:
            assert fetcher.get_title("https://example.com/short") is None
        assert mock_get.call_count == 1  # the first, public hop only

    @pytest.mark.parametrize("location", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/"])
    def test_a_redirect_to_a_non_http_scheme_is_refused(self, fetcher, location):
        with patch.object(fetcher.session, "get", side_effect=[redirect(location)]) as mock_get:
            assert fetcher.get_title("https://example.com/") is None
        assert mock_get.call_count == 1

    def test_a_redirect_loop_ends_with_an_ordinary_error(self, fetcher):
        with patch.object(fetcher.session, "get", side_effect=lambda *a, **k: redirect("https://example.com/again")) as mock_get:
            result = fetcher.get_title("https://example.com/")
        assert result == "Error: Failed to contact the webpage."
        assert mock_get.call_count == URLFetcher.MAX_REDIRECTS + 1

    def test_the_headers_stay_with_every_hop(self, fetcher):
        html = "<html><head><title>Final</title></head></html>"
        with patch.object(fetcher.session, "get", side_effect=[redirect("https://en.wikipedia.org/x"), make_response(html)]) as mock_get:
            fetcher.get_title("https://fi.wikipedia.org/x")
        assert all(c.kwargs["headers"]["User-Agent"] == URLFetcher.WIKIMEDIA_USER_AGENT for c in mock_get.call_args_list)


class FakeStream:
    """The raw body of a response: `chunks` first, then (optionally) endless filler; counts what was read."""
    def __init__(self, chunks, endless=False):
        self.chunks, self.endless, self.read_bytes, self.closed = list(chunks), endless, 0, False

    def read(self, amt=None, *args, **kwargs):
        if self.chunks:
            data = self.chunks.pop(0)
        elif self.endless:
            data = b"x" * (amt or 16384)
        else:
            data = b""
        self.read_bytes += len(data)
        return data

    def close(self):
        self.closed = True


def streamed_response(raw):
    """A real, unread requests.Response over `raw`, as session.get(stream=True) returns it."""
    resp = requests.Response()
    resp.status_code = 200
    resp.raw = raw
    resp.headers["content-type"] = "text/html; charset=utf-8"
    return resp


class TestBoundedRead:
    def test_a_response_that_never_ends_is_read_only_up_to_the_cap(self, fetcher):
        raw = FakeStream([b"<html><head><title>Early title</title></head><body>"], endless=True)
        with patch.object(fetcher.session, "get", return_value=streamed_response(raw)):
            assert fetcher.get_title("https://example.com/big") == "Early title"
        assert raw.read_bytes <= URLFetcher.MAX_PAGE_BYTES + 16384  # it stopped at the cap, not at the end
        assert raw.closed

    def test_the_body_handed_on_is_never_longer_than_the_cap(self, fetcher):
        raw = FakeStream([b"<html><head><title>T</title></head><body>"], endless=True)
        with patch.object(fetcher.session, "get", return_value=streamed_response(raw)):
            response = fetcher._fetch_page("https://example.com/", None)
        assert len(response.content) == URLFetcher.MAX_PAGE_BYTES

    def test_a_slow_drip_is_cut_off_at_the_time_limit(self, fetcher):
        clock = iter([0, 1, 2, 11, 12, 13, 14])  # monotonic(): start, then past MAX_FETCH_SECONDS on the 3rd chunk
        raw = FakeStream([b"<html><head><title>Slow</title>", b"a", b"b", b"c", b"d", b"e"])
        with patch.object(fetcher.session, "get", return_value=streamed_response(raw)), \
                patch("url_fetcher.time.monotonic", side_effect=lambda: next(clock)):
            response = fetcher._fetch_page("https://example.com/", None)
        assert response.content == b"<html><head><title>Slow</title>ab"  # what arrived before the limit

    def test_an_error_status_closes_the_stream_and_reports_it(self, fetcher):
        raw = FakeStream([b"<html>nope</html>"])
        resp = streamed_response(raw)
        resp.status_code = 403
        resp.reason = "Forbidden"
        with patch.object(fetcher.session, "get", return_value=resp):
            assert fetcher.get_title("https://example.com/") == "Error: the webpage returned HTTP 403 Forbidden."
        assert raw.closed


class TestPageEncoding:
    """Issue #24: a statistical charset guess must not override a declared one."""

    YLE = "Glen Kamaran lähtö Huuhkajista voi jopa kostautua QPR:lle – Palloliitto aikoo viedä asian Fifalle"
    HTML = '<html><head><meta property="og:title" content="{}"><title>x</title></head></html>'

    def _title(self, fetcher, resp):
        with patch.object(fetcher.session, "get", return_value=resp):
            return fetcher.get_generic_title("https://example.com/a")

    def test_declared_utf8_is_not_overridden_by_a_wrong_guess(self, fetcher):
        """The real case: apparent_encoding said mac_greek for this page."""
        resp = real_response(self.HTML.format(self.YLE).encode("utf-8"), "text/html; charset=utf-8")
        with patch.object(requests.Response, "apparent_encoding", "mac_greek"):
            assert self._title(fetcher, resp) == self.YLE

    def test_charset_with_quotes_and_other_parameters(self, fetcher):
        resp = real_response(self.HTML.format(self.YLE).encode("utf-8"), 'text/html; charset="UTF-8"; x=y')
        assert self._title(fetcher, resp) == self.YLE

    def test_a_declared_legacy_charset_is_honoured(self, fetcher):
        resp = real_response(self.HTML.format("Häkkinen").encode("iso-8859-1"), "text/html; charset=iso-8859-1")
        assert self._title(fetcher, resp) == "Häkkinen"

    @pytest.mark.parametrize("charset", ["iso-8859-1", "ISO-8859-1", "windows-1252", "us-ascii"])
    def test_a_default_single_byte_charset_on_utf8_text_is_read_as_utf8(self, fetcher, charset):
        """Issue #28: a server labelling a UTF-8 page ISO-8859-1 would double every ä/ö."""
        resp = real_response(self.HTML.format(self.YLE).encode("utf-8"), f"text/html; charset={charset}")
        assert self._title(fetcher, resp) == self.YLE

    def test_a_single_byte_charset_on_text_that_is_not_utf8_is_still_honoured(self, fetcher):
        resp = real_response(self.HTML.format("H\u00e4kkinen \u20ac").encode("windows-1252"), "text/html; charset=windows-1252")
        assert self._title(fetcher, resp) == "H\u00e4kkinen \u20ac"

    def test_ascii_text_under_a_single_byte_charset_is_unchanged(self, fetcher):
        resp = real_response(self.HTML.format("Plain title").encode("ascii"), "text/html; charset=iso-8859-1")
        assert self._title(fetcher, resp) == "Plain title"

    def test_a_declared_utf8_charset_is_not_second_guessed(self, fetcher):
        resp = real_response(self.HTML.format(self.YLE).encode("utf-8"), "text/html; charset=utf-8")
        assert self._title(fetcher, resp) == self.YLE

    def test_an_unknown_declared_charset_is_logged(self, fetcher, capsys):
        resp = real_response(self.HTML.format("Plain").encode("utf-8"), "text/html; charset=no-such-charset")
        self._title(fetcher, resp)
        assert "unknown charset 'no-such-charset'" in capsys.readouterr().out

    def test_an_unknown_declared_charset_does_not_break_the_title(self, fetcher):
        resp = real_response(self.HTML.format("Plain").encode("utf-8"), "text/html; charset=no-such-charset")
        assert self._title(fetcher, resp) == "Plain"

    def test_no_declaration_and_valid_utf8_is_read_as_utf8(self, fetcher):
        """requests would default a header without charset to ISO-8859-1."""
        resp = real_response(self.HTML.format(self.YLE).encode("utf-8"), "text/html")
        with patch.object(requests.Response, "apparent_encoding", "mac_greek"):
            assert self._title(fetcher, resp) == self.YLE

    def test_no_content_type_at_all_is_read_as_utf8(self, fetcher):
        resp = real_response(self.HTML.format(self.YLE).encode("utf-8"))
        assert self._title(fetcher, resp) == self.YLE

    def test_no_declaration_and_not_utf8_uses_the_guess(self, fetcher):
        resp = real_response(self.HTML.format("Häkkinen").encode("iso-8859-1"), "text/html")
        with patch.object(requests.Response, "apparent_encoding", "iso-8859-1"):
            assert self._title(fetcher, resp) == "Häkkinen"


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
