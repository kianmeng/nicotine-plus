# COPYRIGHT (C) 2020-2022 Nicotine+ Contributors
#
# GNU GENERAL PUBLIC LICENSE
#    Version 3, 29 June 2007
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from pynicotine import slskmessages
from pynicotine.config import config
from pynicotine.logfacility import log
from pynicotine.utils import get_completion_list


class ChatRooms:

    def __init__(self, core, queue, ui_callback=None):

        self.core = core
        self.queue = queue
        self.ui_callback = getattr(ui_callback, "chatrooms", None)
        self.server_rooms = set()
        self.joined_rooms = set()
        self.private_rooms = config.sections["private_rooms"]["rooms"]
        self.completion_list = []

    def server_login(self):

        join_list = self.joined_rooms

        if not join_list:
            join_list = config.sections["server"]["autojoin"]

        for room in join_list:
            if room == "Public ":
                self.queue.append(slskmessages.JoinPublicRoom())

            elif isinstance(room, str):
                self.queue.append(slskmessages.JoinRoom(room))

        if self.ui_callback:
            self.ui_callback.server_login()

    def server_disconnect(self):

        self.server_rooms.clear()

        if self.ui_callback:
            self.ui_callback.server_disconnect()

    def show_room(self, room, private=False):

        if room == "Public ":
            # Fake a JoinRoom protocol message
            self.join_room(slskmessages.JoinRoom(room))
            self.queue.append(slskmessages.JoinPublicRoom())

        elif room not in self.joined_rooms:
            self.queue.append(slskmessages.JoinRoom(room, private))
            return

        if self.ui_callback:
            self.ui_callback.show_room(room)

    def remove_room(self, room):

        if room == "Public ":
            self.queue.append(slskmessages.LeavePublicRoom())
        else:
            self.queue.append(slskmessages.LeaveRoom(room))

        self.joined_rooms.discard(room)

        if room in config.sections["columns"]["chat_room"]:
            del config.sections["columns"]["chat_room"][room]

        if self.ui_callback:
            self.ui_callback.remove_room(room)

    def clear_messages(self, room):
        if self.ui_callback:
            self.ui_callback.clear_messages(room)

    def echo_message(self, room, message, message_type="local"):
        if self.ui_callback:
            self.ui_callback.echo_message(room, message, message_type)

    def send_message(self, room, message):

        event = self.core.pluginhandler.outgoing_public_chat_event(room, message)
        if event is None:
            return

        room, message = event
        message = self.core.privatechat.auto_replace(message)

        self.queue.append(slskmessages.SayChatroom(room, message))
        self.core.pluginhandler.outgoing_public_chat_notification(room, message)

    def create_private_room(self, room, owner=None, operators=None):

        private_room = self.private_rooms.get(room)

        if private_room is None:
            private_room = self.private_rooms[room] = {
                "users": [],
                "joined": 0,
                "operators": operators or [],
                "owned": False,
                "owner": owner
            }
            return private_room

        private_room["owner"] = owner

        if operators is None:
            return private_room

        for operator in operators:
            if operator not in private_room["operators"]:
                private_room["operators"].append(operator)

        return private_room

    def is_private_room_owned(self, room):
        private_room = self.private_rooms.get(room)
        return private_room is not None and private_room["owner"] == self.core.login_username

    def is_private_room_member(self, room):
        return room in self.private_rooms

    def is_private_room_operator(self, room):
        private_room = self.private_rooms.get(room)
        return private_room is not None and self.core.login_username in private_room["operators"]

    def request_room_list(self):
        self.queue.append(slskmessages.RoomList())

    def request_private_room_disown(self, room):

        if not self.is_private_room_owned(room):
            return

        self.queue.append(slskmessages.PrivateRoomDisown(room))
        del self.private_rooms[room]

    def request_private_room_dismember(self, room):

        if not self.is_private_room_member(room):
            return

        self.queue.append(slskmessages.PrivateRoomDismember(room))
        del self.private_rooms[room]

    def request_private_room_toggle(self, enabled):
        self.queue.append(slskmessages.PrivateRoomToggle(enabled))

    def get_user_stats(self, msg):
        """ Server code: 36 """

        if self.ui_callback:
            self.ui_callback.get_user_stats(msg)

    def join_room(self, msg):
        """ Server code: 14 """

        self.joined_rooms.add(msg.room)

        if msg.private:
            self.create_private_room(msg.room, msg.owner, msg.operators)

        if self.ui_callback:
            self.ui_callback.join_room(msg)

        self.core.pluginhandler.join_chatroom_notification(msg.room)

    def leave_room(self, msg):
        """ Server code: 15 """

        if self.ui_callback:
            self.ui_callback.leave_room(msg)

        self.core.pluginhandler.leave_chatroom_notification(msg.room)

    def get_user_status(self, msg):
        """ Server code: 7 """

        if self.ui_callback:
            self.ui_callback.get_user_status(msg)

    def private_room_users(self, msg):
        """ Server code: 133 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is None:
            private_room = self.create_private_room(msg.room)

        private_room["users"] = msg.users
        private_room["joined"] = msg.numusers

        if self.ui_callback:
            self.ui_callback.private_room_users(msg)

    def private_room_add_user(self, msg):
        """ Server code: 134 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is not None and msg.user not in private_room["users"]:
            private_room["users"].append(msg.user)

        if self.ui_callback:
            self.ui_callback.private_room_add_user(msg)

    def private_room_remove_user(self, msg):
        """ Server code: 135 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is not None and msg.user in private_room["users"]:
            private_room["users"].remove(msg.user)

        if self.ui_callback:
            self.ui_callback.private_room_remove_user(msg)

    def private_room_disown(self, msg):
        """ Server code: 137 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is not None and private_room["owner"] == self.core.login_username:
            private_room["owner"] = None

        if self.ui_callback:
            self.ui_callback.private_room_disown(msg)

    def private_room_added(self, msg):
        """ Server code: 139 """

        if msg.room not in self.private_rooms:
            self.create_private_room(msg.room)
            log.add(_("You have been added to a private room: %(room)s"), {"room": msg.room})

        if self.ui_callback:
            self.ui_callback.private_room_added(msg)

    def private_room_removed(self, msg):
        """ Server code: 140 """

        if msg.room in self.private_rooms:
            del self.private_rooms[msg.room]

        if self.ui_callback:
            self.ui_callback.private_room_removed(msg)

    def private_room_toggle(self, msg):
        """ Server code: 141 """

        config.sections["server"]["private_chatrooms"] = msg.enabled

    def private_room_add_operator(self, msg):
        """ Server code: 143 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is not None and msg.user not in private_room["operators"]:
            private_room["operators"].append(msg.user)

        if self.ui_callback:
            self.ui_callback.private_room_add_operator(msg)

    def private_room_remove_operator(self, msg):
        """ Server code: 144 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is not None and msg.user in private_room["operators"]:
            private_room["operators"].remove(msg.user)

        if self.ui_callback:
            self.ui_callback.private_room_remove_operator(msg)

    def private_room_operator_added(self, msg):
        """ Server code: 145 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is not None and self.core.login_username not in private_room["operators"]:
            private_room["operators"].append(self.core.login_username)

        if self.ui_callback:
            self.ui_callback.private_room_operator_added(msg)

    def private_room_operator_removed(self, msg):
        """ Server code: 146 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is not None and self.core.login_username in private_room["operators"]:
            private_room["operators"].remove(self.core.login_username)

        if self.ui_callback:
            self.ui_callback.private_room_operator_removed(msg)

    def private_room_owned(self, msg):
        """ Server code: 148 """

        private_room = self.private_rooms.get(msg.room)

        if private_room is None:
            private_room = self.create_private_room(msg.room)

        private_room["operators"] = msg.operators

        if self.ui_callback:
            self.ui_callback.private_room_owned(msg)

    def public_room_message(self, msg):
        """ Server code: 152 """

        if self.ui_callback:
            self.ui_callback.public_room_message(msg)

        self.core.pluginhandler.public_room_message_notification(msg.room, msg.user, msg.msg)

    def room_list(self, msg):
        """ Server code: 64 """

        login_username = self.core.login_username

        for room in msg.rooms:
            self.server_rooms.add(room[0])

        for room in msg.ownedprivaterooms:
            room_data = self.private_rooms.get(room[0])

            if room_data is None:
                self.private_rooms[room[0]] = {"users": [], "joined": room[1], "operators": [], "owner": login_username}
                continue

            room_data['joined'] = room[1]
            room_data['owner'] = login_username

        for room in msg.otherprivaterooms:
            room_data = self.private_rooms.get(room[0])

            if room_data is None:
                self.private_rooms[room[0]] = {"users": [], "joined": room[1], "operators": [], "owner": None}
                continue

            room_data['joined'] = room[1]

            if room_data['owner'] == login_username:
                room_data['owner'] = None

        if self.ui_callback:
            self.ui_callback.room_list(msg)

    def say_chat_room(self, msg):
        """ Server code: 13 """

        log.add_chat(_("Chat message from user '%(user)s' in room '%(room)s': %(message)s"), {
            "user": msg.user,
            "room": msg.room,
            "message": msg.msg
        })

        event = self.core.pluginhandler.incoming_public_chat_event(msg.room, msg.user, msg.msg)
        if event is None:
            return

        _room, _user, msg.msg = event

        if self.ui_callback:
            self.ui_callback.say_chat_room(msg)

        self.core.pluginhandler.incoming_public_chat_notification(msg.room, msg.user, msg.msg)

    def set_user_country(self, user, country):
        if self.ui_callback:
            self.ui_callback.set_user_country(user, country)

    def ticker_add(self, msg):
        """ Server code: 114 """

        if self.ui_callback:
            self.ui_callback.ticker_add(msg)

    def ticker_remove(self, msg):
        """ Server code: 115 """

        if self.ui_callback:
            self.ui_callback.ticker_remove(msg)

    def ticker_set(self, msg):
        """ Server code: 113 """

        if self.ui_callback:
            self.ui_callback.ticker_set(msg)

    def user_joined_room(self, msg):
        """ Server code: 16 """

        if self.ui_callback:
            self.ui_callback.user_joined_room(msg)

        self.core.pluginhandler.user_join_chatroom_notification(msg.room, msg.userdata.username)

    def user_left_room(self, msg):
        """ Server code: 17 """

        if self.ui_callback:
            self.ui_callback.user_left_room(msg)

        self.core.pluginhandler.user_leave_chatroom_notification(msg.room, msg.username)

    def update_completions(self):

        self.completion_list = get_completion_list(list(self.core.pluginhandler.chatroom_commands), self.server_rooms)

        if self.ui_callback:
            self.ui_callback.set_completion_list(self.completion_list)


class Tickers:

    def __init__(self):

        self.messages = []

    def add_ticker(self, user, message):

        message = message.replace("\n", " ")
        self.messages.insert(0, (user, message))

    def remove_ticker(self, user):

        for i, message in enumerate(self.messages):
            if message[0] == user:
                del self.messages[i]
                return

    def get_tickers(self):
        return self.messages

    def clear_tickers(self):
        self.messages.clear()
