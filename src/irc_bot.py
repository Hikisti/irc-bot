import socket

class IrcBot (object):
    def __init__(self):
        self.ircsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server = "dreamhack.se.quakenet.org"
        self.channel = ""
        self.botnick = "SuurinJaKaunein"
        self.adminname = "Hikisti"
        self.exitcode = "bye " + self.botnick
        self.name = ""
        self.message = ""

    def join_channel(self):
        ircmsg = ""
        while ircmsg.find(f"Welcome to the QuakeNet IRC Network, {self.botnick}") == -1:
            ircmsg = self.receive_information()
            self.check_ping(ircmsg)
        self.ircsock.send(bytes("JOIN " + "#bottest123" + "\n", "UTF-8"))
        #self.ircsock.send(bytes("JOIN " + "#valioliiga" + "\n", "UTF-8"))
        #self.ircsock.send(bytes("JOIN " + "#nakkimuusi" + "\n", "UTF-8"))
        
    def connect_to_server(self):
        self.ircsock.connect((self.server, 6667))
        self.ircsock.send(bytes("USER " + self.botnick + " " + self.botnick + " " + self.botnick + " " + self.botnick + "\n", "UTF-8"))
        self.ircsock.send(bytes("NICK " + self.botnick + "\n", "UTF-8"))

    def check_ping(self, msg):
        if msg.find('PING') != -1:
            self.ircsock.send(bytes('PONG ' + msg.split(":")[1] + "\n", "UTF-8"))
   
    def send_a_message(self, msg):
        self.ircsock.send(bytes("PRIVMSG " + self.channel + " :" + str(msg) + "\n", "UTF-8"))

    def receive_information(self):
        ircmsg = self.ircsock.recv(2048).decode("ISO-8859-1")
        ircmsg = ircmsg.strip('\n\r')
        print(ircmsg)
        return ircmsg

    def split_nick_and_message_and_channel(self, msg):
        self.name = msg.split('!', 1)[0][1:]
        self.message = msg.split('PRIVMSG', 1)[1].split(':', 1)[1]
        self.channel = msg.split('PRIVMSG', 1)[1].split(':', 1)[0].strip()

    def stop_the_bot(self, msg):
        if self.name.lower() == self.adminname.lower() and msg.rstrip() == self.exitcode:
            self.send_a_message("I will quit, bye.")
            self.ircsock.send(bytes("QUIT \n", "UTF-8"))
            return True
        else:
            return False

    def main(self):
        self.connect_to_server()
        self.join_channel()
        while True:
            ircmsg = ""
            self.name = ""
            self.message = ""
            ircmsg = self.receive_information()

            if ircmsg.find("PRIVMSG") != -1:
                self.split_nick_and_message_and_channel(ircmsg)
 
            if self.stop_the_bot(self.message):
                break
            self.check_ping(ircmsg)

if __name__ == '__main__':
    ircBot = IrcBot()
    ircBot.main()