import os
import socket
import threading
import time
import sys
import traceback

from command_handler import CommandHandler
from url_fetcher import URLFetcher

class IrcBot:
    def __init__(self, server="irc.quakenet.org", port=6667, nickname="KukistiBot", channels=None):
        self.server = server
        self.port = port
        self.nickname = nickname
        self.channels = channels if channels else ["#bottest123"]  # Default channel
        self.running = False
        self.sock = None
        self._send_lock = threading.Lock()  # serializes socket writes across threads
        self.command_handler = CommandHandler()  # Initialize command handler
        self.url_fetcher = URLFetcher(self)  # Initialize URL fetcher

    def connect(self):
        """Connect to the IRC server and join the channel."""
        # A distinct, greppable marker with the PID - lets
        # `journalctl | grep "BOT STARTED"` show every start/restart at a
        # glance instead of having to correlate `systemctl status` output
        # with log timestamps by hand.
        print(f"==== BOT STARTED pid={os.getpid()} ====")
        print(f"Connecting to {self.server}:{self.port} as {self.nickname}...")
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.server, self.port))
            print(f"CONNECTED: {self.server}:{self.port} as {self.nickname}")
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
            print(f"CONNECT FAILED: {e}")
            traceback.print_exc()

    def listen(self):
        """Listen for messages from the server."""
        buffer = ""
        while self.running:
            try:
                data = self.sock.recv(2048).decode("utf-8", errors="ignore")
                if not data:
                    # "DISCONNECTED: ..." wording matches the exception
                    # branch below - a single, consistent, greppable
                    # prefix for every way the connection can end.
                    print("DISCONNECTED: server closed the connection (EOF)")
                    self.running = False
                    break

                # A single IRC line isn't guaranteed to arrive in one
                # recv() call - it can be split across the 2048-byte
                # boundary. Buffer across calls and only process complete
                # "\n"-terminated lines, keeping a trailing partial line
                # (if any) for the next recv() to complete.
                buffer += data
                *complete_lines, buffer = buffer.split("\n")

                for line in complete_lines:
                    line = line.strip()
                    if not line:
                        continue
                    print(f"< {line}")  # Debugging

                    if line.startswith("PING"):
                        self.pong(line)
                        continue

                    # Every real server line is ":prefix COMMAND ...", so
                    # the actual command is parts[1] - matching a command
                    # name as a substring of the whole line (the previous
                    # approach) means chat text that happens to contain
                    # "001" or "PRIVMSG" gets misdispatched as if it were
                    # that server event.
                    parts = line.split(" ", 2)
                    command = parts[1] if line.startswith(":") and len(parts) > 1 else None

                    if command == "001":  # Server welcome message
                        print("Server welcome message received. Joining channels...")
                        self.join_channels()  # Join multiple channels
                    elif command == "PRIVMSG":
                        self.process_message(line)
            except Exception as e:
                # The catch-all here is the last line of defense against
                # anything not anticipated by more specific handling - a
                # traceback (not just str(e)) is what actually pinpoints
                # where this broke, same reasoning as the equivalent
                # catch-alls in liiga_command.py/pesis_command.py.
                print(f"DISCONNECTED: error in listen loop: {e}")
                traceback.print_exc()
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
            # sendall() (rather than send()) guarantees the whole line goes
            # out in one go rather than potentially partial-writing; the
            # lock then serializes that across threads (multiple command
            # background pollers, e.g. !liiga and !superpesis, plus the
            # listener thread, can all call this concurrently) so two
            # messages can never interleave mid-line on the wire.
            with self._send_lock:
                self.sock.sendall((message + "\r\n").encode("utf-8"))
        except Exception as e:
            print(f"Failed to send message: {e}")

    def join_channels(self):
        """Join multiple channels."""
        for channel in self.channels:
            print(f"Attempting to join {channel}...")
            self.send_raw(f"JOIN {channel}")
            time.sleep(1)  # Prevent flooding

    def send_message(self, channel, message):
        """Send a message to the specified IRC channel."""
        safe_message = message.replace("\n", " ").replace("\r", " ")  # Remove line breaks
        self.send_raw(f"PRIVMSG {channel} :{safe_message}")  # Prefix colon to prevent misinterpretation
        
    def process_message(self, message):
        """Extracts sender, channel, and message, then processes commands."""
        parts = message.split(" ", 3)
        if len(parts) < 4:
            return
        prefix, command, channel, msg = parts
        nick = prefix.split("!")[0][1:]  # Extract nickname
        msg = msg[1:]  # Remove leading ':'
        
        if channel in self.channels:
            if msg.startswith("!"):  # Command handling
                self.command_handler.handle_command(self, nick, channel, msg)
            else:  # Check for URLs in messages
                self.url_fetcher.detect_and_fetch(nick, channel, msg)
    
    def stop(self):
        """Stop the bot and close the connection."""
        print("DISCONNECTED: stop() called (requested shutdown)")
        self.running = False
        self.send_raw("QUIT :Bot shutting down")
        self.sock.close()

if __name__ == "__main__":
    debug_mode = "--debug" in sys.argv  # Check if --debug argument is present

    if debug_mode:
        print("Running in debug mode: Joining only the default channel.")
        bot = IrcBot()  # No channels argument, so it defaults to ["#bottest123"]
    else:
        bot = IrcBot(channels=["#smliiga", "#valioliiga", "#nakkimuusi", "#pesis.fi"])

    bot.connect()
