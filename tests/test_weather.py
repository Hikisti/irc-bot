from unittest.mock import MagicMock, patch

import pytest
import requests

from src.weather import WeatherCommand


def make_response(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.reason = "Not Found"
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(response=resp)
    return resp


@pytest.fixture
def weather_command(monkeypatch):
    monkeypatch.setenv("WEATHER_API_KEY", "test-key")
    return WeatherCommand()


class TestWeatherCommand:
    def test_missing_api_key_returns_error_without_crashing(self, monkeypatch):
        monkeypatch.delenv("WEATHER_API_KEY", raising=False)
        with patch("src.weather.load_dotenv"):
            wc = WeatherCommand()
        assert "WEATHER_API_KEY is not set" in wc.execute("austin")

    def test_no_args_returns_usage_error(self, weather_command):
        assert "Usage:" in weather_command.execute("")

    def test_happy_path_includes_humidity(self, weather_command):
        data = {
            "location": {"name": "Austin", "country": "United States of America"},
            "current": {
                "condition": {"text": "Sunny"},
                "temp_c": 32,
                "temp_f": 90,
                "feelslike_c": 34,
                "feelslike_f": 93,
                "wind_kph": 11.2,
                "wind_dir": "NE",
                "humidity": 45,
            },
        }
        with patch("src.weather.requests.get", return_value=make_response(data)):
            result = weather_command.execute("austin")

        assert result == (
            "Current weather in Austin, United States of America: Sunny, "
            "32°C (90°F) (feels like 34°C/93°F). Wind: NE 3.1 m/s. Humidity: 45%."
        )

    def test_city_and_country_are_parsed(self, weather_command):
        data = {
            "location": {"name": "Paris", "country": "France"},
            "current": {
                "condition": {"text": "Cloudy"},
                "temp_c": 18,
                "temp_f": 64,
                "feelslike_c": 17,
                "feelslike_f": 63,
                "wind_kph": 7.2,
                "wind_dir": "W",
                "humidity": 60,
            },
        }
        with patch("src.weather.requests.get", return_value=make_response(data)) as mock_get:
            weather_command.execute("paris, france")

        params = mock_get.call_args.kwargs["params"]
        assert params["q"] == "paris,france"

    def test_missing_humidity_falls_back_to_placeholder(self, weather_command):
        data = {
            "location": {"name": "Austin", "country": "United States of America"},
            "current": {
                "condition": {"text": "Sunny"},
                "temp_c": 32,
                "temp_f": 90,
                "feelslike_c": 34,
                "feelslike_f": 93,
                "wind_kph": 11.2,
                "wind_dir": "NE",
            },
        }
        with patch("src.weather.requests.get", return_value=make_response(data)):
            result = weather_command.execute("austin")

        assert "Humidity: ?%." in result

    def test_unexpected_payload_returns_error(self, weather_command):
        with patch("src.weather.requests.get", return_value=make_response({"foo": "bar"})):
            result = weather_command.execute("austin")
        assert result.startswith("Error:")

    def test_timeout_returns_friendly_error(self, weather_command):
        with patch("src.weather.requests.get", side_effect=requests.exceptions.Timeout):
            result = weather_command.execute("austin")
        assert "timed out" in result

    def test_connection_error_returns_friendly_error(self, weather_command):
        with patch("src.weather.requests.get", side_effect=requests.exceptions.ConnectionError):
            result = weather_command.execute("austin")
        assert "Unable to connect" in result

    def test_http_error_returns_status_in_message(self, weather_command):
        error_response = make_response({}, status_code=404)
        with patch("src.weather.requests.get", return_value=error_response):
            result = weather_command.execute("nonexistent-city")
        assert "404" in result
