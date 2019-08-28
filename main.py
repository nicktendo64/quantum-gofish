import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, \
    InlineQueryHandler, ParseMode
from telegram.error import TelegramError

import logging
import datetime
from enum import Enum

with open("ignore/token.txt", "r") as f:
    API_TOKEN = f.read().rstrip()

with open("ignore/username.txt", "r") as f:
    USERNAME = f.read().rstrip()
    DM_URL = "http://t.me/{}".format(USERNAME)

def get_static_handler(command):
    """
    Given a string command, returns a CommandHandler for that string that
    responds to messages with the content of static_responses/[command].txt

    Throws IOError if file does not exist or something
    """

    f = open("static_responses/{}.txt".format(command), "r")
    response = f.read()

    return CommandHandler(command, \
        ( lambda bot, update : \
        bot.send_message(chat_id=update.message.chat.id, text=response) ) )

# Credit: https://github.com/CaKEandLies/Telegram_Cthulhu/blob/master/cthulhu_game_bot.py#L63
def feedback_handler(bot, update, args):
    """
    Store feedback from users in a text file.
    """
    if args and len(args) > 0:
        feedback = open("data/feedback.txt", "a")
        feedback.write("\n")
        feedback.write(update.message.from_user.first_name)
        feedback.write("\n")
        # Records User ID so that if feature is implemented, can message them
        # about it.
        feedback.write(str(update.message.from_user.id))
        feedback.write("\n")
        feedback.write(" ".join(args))
        feedback.write("\n")
        feedback.close()
        bot.send_message(chat_id=update.message.chat_id,
                         text="Thanks for the feedback!")
    else:
        bot.send_message(chat_id=update.message.chat_id,
                         text="Format: /feedback [feedback]")

def handle_error(bot, update, error):
    try:
        raise error
    except TelegramError:
        logging.getLogger(__name__).warning('TelegramError! %s caused by this update:\n%s', error, update)

NUM_PER_SUIT = 4
class GameState:
    """
    Class for tracking the state of a game. That is, given n players
    (referred to by indices 0 to n-1) and n suits (also 0 to n-1), learn data
    about what players may or may not have.
    """

    def __init__(self, num_players):
        # TODO will need to change if we want to support arbitrary joins in first round
        self.num_players = num_players
        self.player_minimums = [ [ 0 for _ in range(num_players) ] for _ in range(num_players) ]
        self.player_maximums = [ [ num_players for _ in range(num_players) ] for _ in range(num_players) ]
        self.hand_sizes = [ NUM_PER_SUIT for _ in range(num_players) ]

    # TODO track the message that lets us know each thing so we can send "proof" of why a move is invalid
    def has_at_least(self, player, suit, n):
        self.player_minimums[player][suit] = max(self.player_minimums[player][suit], n)

    def has_at_most(self, player, suit, n):
        self.player_maximums[player][suit] = min(self.player_maximums[player][suit], n)

    def has_exactly(self, player, suit, n):
        self.player_maximums[player][suit] = 0
        self.player_minimums[player][suit] = 0

    def has_hand_size(self, player, n):
        self.hand_sizes[player] = n

    def can_have(self, player, suit, n):
        """
        Check if 'player' can have 'n' cards of suit 'suit'
        """
        return n >= self.player_minimums[player][suit] and \
            n <= self.player_maximums[player][suit]

    def deduce_extrema(self):
        """
        From all current extrema, deduce stricter extrema from the principles
        of the game: there are NUM_PER_SUIT cards of each suit and each players
        hand consists of cards from the suits.
        """

        # TODO should we apply this function until we hit a fixed point
        # instead of just applying once each turn?

        # there are exactly NUM_PER_SUIT cards in every suit
        for suit in range(self.num_players):
            for player in range(self.num_players):
                in_other_hands = 0
                maybe_in_other_hands = 0
                for other_player in range(self.num_players):
                    if player == other_player:
                        continue
                    in_other_hands += self.player_minimums[other_player][suit]
                    maybe_in_other_hands += self.player_maximums[other_player][suit]
                self.has_at_most(player, suit, NUM_PER_SUIT - in_other_hands)
                self.has_at_least(player, suit, NUM_PER_SUIT - maybe_in_other_hands)

        # each player has the number of cards that they have
        for player in range(self.num_players):
            for suit in range(self.num_players):
                num_known_cards = 0
                num_possible_cards = 0
                for other_suit in range(self.num_players):
                    if suit == other_suit:
                        continue
                    num_known_cards += self.player_minimums[player][other_suit]
                    num_possible_cards += self.player_maximums[player][other_suit]
                self.has_at_most(player, suit, self.hand_sizes[player] - num_known_cards)
                self.has_at_least(player, suit, self.hand_sizes[player] - num_possible_cards)

    def is_converged(self):
        return self.player_maximums == self.player_minimums

    def asked_for(self, player, suit):
        """
        Note that some player 'player' has asked another for the suit 'suit'.
        If this is impossible (it is known that 'player' has no 'suit's), returns
        False and does nothing.

        If it is possible, returns True and internally notes that the player
        has at least 1 card of suit 'suit'.
        """

        if not self.can_have(player, suit, 1):
            return False

        self.has_at_least(player, suit, 1)

        return True

    def gave_away(self, player, suit, n):
        """
        Note that some player 'player' has given away exacly 'n' cards with suit
        'suit'.

        If this is impossible (it is known that 'player' has more or less than n
        'suit's), returns False and does nothing.

        If it is possible, returns True and internally notes that the player
        has given away 'n' 'suit's
        """

        if not self.can_have(player, suit, n):
            return False

        self.has_hand_size(player, self.hand_sizes[player] - n)
        self.has_exactly(player, suit, 0)

        return True

    def received(self, player, suit, n):
        """
        Notes that 'player' has received 'n' cards of suit 'suit'. This action
        cannot fail. Returns True.
        """

        self.has_hand_size(player, self.hand_sizes[player] + n)
        self.player_minimums[player][suit] += n
        self.player_maximums[player][suit] += n
        self.has_at_most(player, suit, NUM_PER_SUIT)

        return True

    def test_action(self, source, target, suit, n):
        print( self.asked_for(source, suit) and \
            self.gave_away(target, suit, n) and \
            self.received(source, suit, n) )
        print()

    def __str__(self):
        return "Hand sizes[players]:  " + str(self.hand_sizes) + "\n" + \
               "Mins[players][suits]: " + str(state.player_minimums) + "\n" + \
               "Maxs[players][suits]: " + str(state.player_maximums)

class Player:
    """
    Helper class for representing a Telegram user,
    allowing them to set a nickname, and tag them in a Markdown-encoded message.
    """
    def __init__(self, id, nickname):
        self.id = id
        self.name = name

    def set_nickname(self, name):
        self.name = name

    def get_markdown_tag(self):
        return "[{}](tg://user?id={})".format(self.name, self.id)

class GameStatus(Enum):
    GAME_NOT_STARTED = 0
    AWAITING_ASK = 1
    AWAITING_RESPONSE = 2
    GAME_OVER = 3

class Game:
    """
    Represents a game of Quantum Go Fish. Maintains a GameState representing the
    current state of the game, and a list of players and suit names with indices
    corresponding to the GameState.
    """
    def __init__(self):
        self.players = []
        self.suit_names = []
        self.started = False
        self.status = GameStatus.GAME_NOT_STARTED

    def player_join(self, player):
        if self.started:
            return "cannot join a game that has already started"
        else:
            self.players.append(player)

    def player_leave(self, player):
        if self.started:
            return "cannot join a game that has already started"
        elif player not in self.players:
            return "it's not like you were playing..."
        else:
            self.players.remove(player)

    def game_start(self):
        self.num_players = len(self.players)
        self.state = GameState(self.num_players)
        self.started = True
        # TODO shuffle players?

        self.status = GameStatus.AWAITING_ASK
        self.asking_player_idx = 0

    def get_player(self, nickname_or_idx):
        """
        Get a Player object of a player in this game given 'nickname_or_idx', a
        string that another user might use to refer to them
        (their index in the turn order or their nickname).

        Returns None if no player with that index/nickname exists.
        """
        if nickname_or_idx.isdigit() and int(nickname_or_idx) < len(self.players):
            return self.players[int(nickname_or_idx)]
        for player in self.players:
            if player.name == self.nickname:
                return player

    def get_player_md_tag(self, nickname_or_idx):
        """
        Wrapper for get_player that returns either the Markdown tag of the
        player referred to by 'nickname_or_idx' or None if the player could not
        be identified.
        """

        res = self.get_player(nickname_or_idx)
        if res:
            return res.get_markdown_tag()
        else:
            return None

    def ask_for(self, player, target_str, suit):
        if self.status != GameStatus.AWAITING_ASK:
            return "/ask unexpected"

        target = self.get_player(target_str)
        if target:
            target_idx = self.players.index(target)
        else:
            return "cannot parse target user: " + target_str

        if suit in suits:
            suit_idx = suits.index(suit)
        else:
            if len(suits) == self.num_players:
                return "could not parse suit name: " + suit
            suit_idx = len(suits)
            suits.append(suit)

        if not self.state.asked_for(target_idx, suit_idx):
            return "error: game state indicates that {} has at least one \"{}\" with probability zero".format(player.name, suit)

        self.status = GameStatus.AWAITING_RESPONSE
        self.requested_suit_idx = suit_idx
        self.target_player = target

    def respond_to_request(self, player, n_str):
        if self.status != GameStatus.AWAITING_RESPONSE:
            return "/ihave unexpected"

        if n_str.isdigit():
            n = int(n_str)
        else:
            return "cannot parse number of cards: " + n_str

        if player == self.target_player:
            player_idx = self.players.index(player)
        else:
            return "expected \"/ihave [n]\" from {}, not {}".format(self.target_player.name, player.name)

        if self.state.gave_away(player_idx, self.requested_suit_idx, n):
            self.state.received(self.asking_player_idx, self.requested_suit_idx, n)
        else:
            return "error: game state indicates that {} has {} \"{}\" with probability zero".format(player.name, n, suit)

        self.status = GameStatus.AWAITING_ASK
        self.asking_player_idx = (self.asking_player_idx + 1) % self.num_players


def newgame_handler(bot, update, chat_data):
    chat_data["game_obj"] = Game()
    update.message.reply_text("Started new Quantum Go Fish game! /joingame to join")

# Telegram handlers for inquiries about players/nicknames

def i_am_handler(bot, update, user_data, args):
    if args:
        nickname = " ".join(args)
        if "player_obj" in user_data:
            user_data["player_obj"].set_nickname(nickname)
        else:
            user_data["player_obj"] = Player(update.message.from_user.id, nickname)

        update.message.reply_text("Successfully changed nickname to: " + nickname)
    else:
        update.message.reply_text("Nickname required")

def list_player_handler(bot, update, chat_data):
    if "game_obj" in chat_data:
        update.message.reply_text(chat_data["game_obj"].player_list())
    else:
        update.message.reply_text("No game exists in this chat")

def whois_handler(bot, update, args, chat_data):
    if "game_obj" not in chat_data:
        update.message.reply_text("No game exists in this chat")
    elif not args:
        update.message.reply_text("usage: /whois [nickname or index]")
    else:
        nickname = " ".join(args)
        res = chat_data["game_obj"].get_player_md_tag(nickname)
        if res:
            update.message.reply_text(res, parse_mode=ParseMode.MARKDOWN)
        else:
            update.message.reply_text("No player with nickname '{}'".format(nickname))

# Telegram handlers for game management actions (joining/leaving the lobby, starting)
# TODO shouldn't need... (see comment below)
def join_handler(bot, update, user_data, chat_data):
    if "game_obj" in chat_data:
        if "player_obj" not in user_data:
            user_data["player_obj"] = Player(update.message.from_user.id, update.message.from_user.first_name)

        player = user_data["player_obj"]
        game = chat_data["game_obj"]
        msg = game.player_join(player)
        if msg:
            update.message.reply_text(msg)
        else:
            update.message.reply_text("Welcome, {}! Current player count is {}.".format(player.name, len(game.players)))
    else:
        update.message.reply_text("No game exists in this chat")

def leave_handler(bot, update, user_data, chat_data):
    if "game_obj" in chat_data:
        if "player_obj" not in user_data:
            user_data["player_obj"] = Player(update.message.from_user.id, update.message.from_user.first_name)

        player = user_data["player_obj"]
        game = chat_data["game_obj"]
        msg = game.player_leave(player)
        if msg:
            update.message.reply_text(msg)
        else:
            update.message.reply_text("{} has left. Current player count is {}.".format(player.name, len(game.players)))
    else:
        update.message.reply_text("No game exists in this chat")

def start_game_handler(bot, update, chat_data):
    if "game_obj" in chat_data:
        chat_data["game_obj"].game_start()
        update.message.reply_text("Game has started!")
        # TODO whose turn is it
    else:
        update.message.reply_text("No game exists in this chat")

# Telegram handlers for in-game actions: asking another user for something,
# responding with how many you have, or /go fish (equivalent to "/ihave 0")

def ask_handler(bot, update, user_data, chat_data, args):
    if len(args) < 2:
        update.message.reply_text("syntax: /ask [user] [suit name]")
    elif "game_obj" not in chat_data:
        update.message.reply_text("No game exists in this chat")
    elif "player_obj" not in user_data or user_data["player_obj"] not in chat_data["game_obj"].players:
        update.message.reply_text("It doesn't look like you're in this game")
    else:
        response = chat_data["game_obj"].ask_for(user_data["player_obj"], args[0], " ".join(args[1:]))
        # TODO require one-word nicknames

        if response:
            update.message.reply_text(response)
        # TODO also reply indicating success


def have_handler(bot, update, user_data, chat_data, args):
    if len(args) != 1:
        update.message.reply_text("syntax: /ihave [number]")
    elif "game_obj" not in chat_data:
        update.message.reply_text("No game exists in this chat")
    elif "player_obj" not in user_data or user_data["player_obj"] not in chat_data["game_obj"].players:
        update.message.reply_text("It doesn't look like you're in this game")
    else:
        response = chat_data["game_obj"].respond_to_request(user_data["player_obj"], args[0])
        if response:
            update.message.reply_text(response)
        # TODO also reply indicating success

def go_fish_handler(bot, update, user_data, chat_data):
    have_handler(bot, update, user_data=user_data, chat_data=chat_data,
        args=["0"])

if __name__ == "__main__":
    updater = Updater(token=API_TOKEN)
    dispatcher = updater.dispatcher

    dispatcher.add_handler(get_static_handler("help"))
    dispatcher.add_handler(CommandHandler('feedback', feedback_handler, pass_args=True))

    dispatcher.add_handler(CommandHandler('newgame', newgame_handler, pass_chat_data=True))
    dispatcher.add_handler(CommandHandler('listplayers', list_player_handler, pass_chat_data=True))
    dispatcher.add_handler(CommandHandler('whois', whois_handler, pass_args=True, pass_chat_data=True))
    dispatcher.add_handler(CommandHandler('iam', i_am_handler, pass_args=True, pass_user_data=True))

    # TODO these 2 commands shouldn't be necessary: you should be able to just start a game, then whoever asks first
    # is in it, as well as whoever they ask. but GameState currently does not support this
    dispatcher.add_handler(CommandHandler('joingame', join_handler, pass_user_data=True, pass_chat_data=True))
    dispatcher.add_handler(CommandHandler('leavegame', leave_handler, pass_user_data=True, pass_chat_data=True))
    dispatcher.add_handler(CommandHandler('startgame', start_game_handler, pass_chat_data=True))

    dispatcher.add_handler(CommandHandler('ask', ask_handler, pass_args=True, pass_user_data=True, pass_chat_data=True))
    dispatcher.add_handler(CommandHandler('ihave', have_handler, pass_args=True, pass_user_data=True, pass_chat_data=True))
    dispatcher.add_handler(CommandHandler('gofish', go_fish_handler, pass_user_data=True, pass_chat_data=True))

    dispatcher.add_error_handler(handle_error)

    logging.basicConfig(
        filename="ignore/bot.log",
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO)

    updater.start_polling()
    updater.idle()
