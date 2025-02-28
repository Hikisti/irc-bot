import requests

class WeatherCommand:
    """Fetches weather data for a given city."""
    
    def execute(self, city):
        if not city:
            return "Usage: !weather <city>"
        
        try:
            # Replace with an actual weather API endpoint
            url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid=YOUR_API_KEY&units=metric"
            response = requests.get(url)
            response.raise_for_status()
            
            data = response.json()
            temp = data["main"]["temp"]
            desc = data["weather"][0]["description"]
            
            return f"Weather in {city}: {temp}°C, {desc}"
        except requests.exceptions.RequestException as e:
            return f"Error fetching weather data: {e}"
