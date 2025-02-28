import requests

class ElectricityCommand:
    """Fetches electricity prices in Finland."""
    
    def execute(self, args):
        try:
            # Replace with actual API endpoint for electricity prices in Finland
            url = "https://api.example.com/electricity/finland"
            response = requests.get(url)
            response.raise_for_status()
            
            data = response.json()
            return f"Current electricity price in Finland: {data['price']} €/MWh"
        except requests.exceptions.RequestException as e:
            return f"Error fetching electricity prices: {e}"
