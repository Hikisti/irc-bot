import socket
import threading
import time

class IrcBot:
    def __init__(self, server="irc.quakenet.org", port=6667, nickname="SuurinJaKaunein", channels=None):
        self.server = server
        self.port = port
        self.nickname = nickname
        self.channels = channels if channels else ["#bottest123"]  # Default channel
        self.running = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)


    def connect(self):
        """Connect to the IRC server and join the channel."""
        print(f"Connecting to {self.server}:{self.port} as {self.nickname}...")
        try:
            self.sock.connect((self.server, self.port))
            self.send_raw(f"NICK {self.nickname}")
            self.send_raw(f"USER {self.nickname} 0 * :{self.nickname}")
            self.running = True
            
            # Start listening in a separate thread
            listener = threading.Thread(target=self.listen, daemon=True)
            listener.start()

            # Wait a few seconds before joining channel
            time.sleep(3)
            self.join_channels()

            # Keep the bot running
            while self.running:
                time.sleep(1)
        except Exception as e:
            print(f"Connection error: {e}")

    def listen(self):
        """Listen for messages from the server."""
        while self.running:
            try:
                response = self.sock.recv(2048).decode("utf-8", errors="ignore").strip()
                if not response:
                    print("Connection lost. Exiting...")
                    self.running = False
                    break

                for line in response.split("\n"):
                    line = line.strip()
                    print(f"< {line}")  # Debugging

                    if line.startswith("PING"):
                        self.pong(line)
                    elif " 001 " in line:  # Server welcome message
                        print("Server welcome message received. Joining channels...")
                        self.join_channels()  # Join multiple channels
                    elif "PRIVMSG" in line:
                        self.process_message(line)
            except Exception as e:
                print(f"Error in listen loop: {e}")
                self.running = False


    def pong(self, message):
        """Respond to PING messages from the server."""
        server = message.split()[1]
        print(f"Responding to PING from {server}")
        self.send_raw(f"PONG {server}")

    def send_raw(self, message):
        """Send a raw command to the IRC server."""
        print(f"> {message}")  # Debugging
        try:
            self.sock.send((message + "\r\n").encode("utf-8"))
        except Exception as e:
            print(f"Failed to send message: {e}")

    def join_channels(self):
        """Join multiple channels."""
        for channel in self.channels:
            print(f"Attempting to join {channel}...")
            self.send_raw(f"JOIN {channel}")
            time.sleep(1)  # Prevent flooding

    def send_message(self, message):
        """Send a message to the channel."""
        print(f"Sending message to {self.channel}: {message}")
        self.send_raw(f"PRIVMSG {self.channel} :{message}")
    
    def process_message(self, message):
        """Extract nickname, channel, and message."""
        parts = message.split(" ", 3)
        if len(parts) < 4:
            return
        prefix, command, channel, msg = parts
        nick = prefix.split("!")[0][1:]  # Extract nickname
        msg = msg[1:]  # Remove leading ':'
        
        if channel in self.channels:
            print(f"[{channel}] {nick}: {msg}")  # Show messages from all joined channels

    def stop(self):
        """Stop the bot and close the connection."""
        print("Stopping bot...")
        self.running = False
        self.send_raw("QUIT :Bot shutting down")
        self.sock.close()

if __name__ == "__main__":
    bot = IrcBot(channels=["#smliiga", "#nhl.fi", "#nakkimuusi"])
    bot.connect()
