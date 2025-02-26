import socket
import threading
import time

class IrcBot:
    def __init__(self, server="irc.quakenet.org", port=6667, nickname="SuurinJaKaunein", channel="#bottest123"):
        self.server = server
        self.port = port
        self.nickname = nickname
        self.channel = channel
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
            self.join_channel()

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
                    elif " 001 " in line:  # Welcome message received
                        print("Server welcome message received. Joining channel...")
                        self.join_channel()
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

    def join_channel(self):
        """Join a specified channel."""
        print(f"Joining channel {self.channel}...")
        self.send_raw(f"JOIN {self.channel}")

    def send_message(self, message):
        """Send a message to the channel."""
        print(f"Sending message to {self.channel}: {message}")
        self.send_raw(f"PRIVMSG {self.channel} :{message}")

    def process_message(self, message):
        """Extract the nickname, channel, and actual message."""
        parts = message.split(" ", 3)
        if len(parts) < 4:
            return
        prefix, command, channel, msg = parts
        nick = prefix.split("!")[0][1:]  # Extract nick
        msg = msg[1:]  # Remove leading ':' from message
        print(f"[{channel}] {nick}: {msg}")

    def stop(self):
        """Stop the bot and close the connection."""
        print("Stopping bot...")
        self.running = False
        self.send_raw("QUIT :Bot shutting down")
        self.sock.close()

# Example usage:
if __name__ == "__main__":
    bot = IrcBot()
    bot.connect()
