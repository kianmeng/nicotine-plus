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

import json
import os

from operator import itemgetter
from threading import Thread

from pynicotine import slskmessages
from pynicotine import utils
from pynicotine.config import config
from pynicotine.logfacility import log
from pynicotine.slskmessages import UserStatus
from pynicotine.utils import clean_file
from pynicotine.utils import encode_path
from pynicotine.utils import get_result_bitrate_length
from pynicotine.utils import RestrictedUnpickler


class UserBrowse:

    def __init__(self, core, ui_callback=None):

        self.core = core
        self.users = set()
        self.ui_callback = getattr(ui_callback, "userbrowse", None)
        utils.OPEN_SOULSEEK_URL = self.open_soulseek_url

    def server_login(self):
        for user in self.users:
            self.core.watch_user(user)  # Get notified of user status

    def server_disconnect(self):
        if self.ui_callback:
            self.ui_callback.server_disconnect()

    def send_upload_attempt_notification(self, username):
        """ Send notification to user when attempting to initiate upload from our end """

        self.core.send_message_to_peer(username, slskmessages.UploadQueueNotification())

    def add_user(self, user):
        if user not in self.users:
            self.users.add(user)

    def remove_user(self, user):

        self.users.remove(user)

        if self.ui_callback:
            self.ui_callback.remove_user(user)

    def show_user(self, user, path=None, local_shares_type=None, switch_page=True):

        self.add_user(user)

        if self.ui_callback:
            self.ui_callback.show_user(user, path, local_shares_type, switch_page)

    def parse_local_shares(self, username, msg):
        """ Parse a local shares list and show it in the UI """

        built = msg.make_network_message()
        msg.parse_network_message(built)

        self.shared_file_list(msg, username)

    def browse_local_public_shares(self, path=None, new_request=None):
        """ Browse your own public shares """

        username = config.sections["server"]["login"] or "Default"

        if username not in self.users or new_request:
            msg = self.core.shares.get_compressed_shares_message("normal")
            Thread(
                target=self.parse_local_shares, args=(username, msg), name="LocalShareParser", daemon=True
            ).start()

        self.show_user(username, path=path, local_shares_type="normal")

    def browse_local_buddy_shares(self, path=None, new_request=False):
        """ Browse your own buddy shares """

        username = config.sections["server"]["login"] or "Default"

        if username not in self.users or new_request:
            msg = self.core.shares.get_compressed_shares_message("buddy")
            Thread(
                target=self.parse_local_shares, args=(username, msg), name="LocalBuddyShareParser", daemon=True
            ).start()

        self.show_user(username, path=path, local_shares_type="buddy")

    def browse_user(self, username, path=None, local_shares_type=None, new_request=False, switch_page=True):
        """ Browse a user's shares """

        if not username:
            return

        if username == (config.sections["server"]["login"] or "Default"):
            if local_shares_type == "normal":
                self.browse_local_public_shares(path, new_request)
                return

            self.browse_local_buddy_shares(path, new_request)
            return

        user_exists = (username in self.users)
        self.show_user(username, path=path, switch_page=switch_page)

        if self.core.user_status == UserStatus.OFFLINE:
            self.show_connection_error(username)
            return

        self.core.watch_user(username, force_update=True)

        if not user_exists or new_request:
            self.core.send_message_to_peer(username, slskmessages.GetSharedFileList())

    def create_user_shares_folder(self):

        shares_folder = os.path.join(config.data_dir, "usershares")
        shares_folder_encoded = encode_path(shares_folder)

        try:
            if not os.path.isdir(shares_folder_encoded):
                os.makedirs(shares_folder_encoded)

        except Exception as error:
            log.add(_("Can't create directory '%(folder)s', reported error: %(error)s"),
                    {'folder': shares_folder, 'error': error})
            return None

        return shares_folder

    def load_shares_list_from_disk(self, filename):

        filename_encoded = encode_path(filename)

        try:
            try:
                # Try legacy format first
                import bz2

                with bz2.BZ2File(filename_encoded) as file_handle:
                    shares_list = RestrictedUnpickler(file_handle, encoding='utf-8').load()

            except Exception:
                # Try new format

                with open(filename_encoded, encoding="utf-8") as file_handle:
                    shares_list = json.load(file_handle)

            # Basic sanity check
            for _folder, files in shares_list:
                for _code, _filename, _size, _ext, _attrs, *_unused in files:
                    break

        except Exception as msg:
            log.add(_("Loading Shares from disk failed: %(error)s"), {'error': msg})
            return

        username = filename.replace('\\', os.sep).split(os.sep)[-1]
        self.show_user(username)

        msg = slskmessages.SharedFileList()
        msg.list = shares_list

        self.shared_file_list(msg, username)

    def save_shares_list_to_disk(self, user, shares_list):

        shares_folder = self.create_user_shares_folder()

        if not shares_folder:
            return

        try:
            path = os.path.join(shares_folder, clean_file(user))

            with open(encode_path(path), "w", encoding="utf-8") as file_handle:
                # Add line breaks for readability, but avoid indentation to decrease file size
                json.dump(shares_list, file_handle, ensure_ascii=False, indent=0)

            log.add(_("Saved list of shared files for user '%(user)s' to %(dir)s"),
                    {'user': user, 'dir': shares_folder})

        except Exception as error:
            log.add(_("Can't save shares, '%(user)s', reported error: %(error)s"), {'user': user, 'error': error})

    def download_file(self, user, folder, file_data, prefix=""):

        virtualpath = "\\".join([folder, file_data[1]])
        size = file_data[2]
        h_bitrate, _bitrate, h_length, _length = get_result_bitrate_length(size, file_data[4])

        self.core.transfers.get_file(user, virtualpath, prefix,
                                     size=size, bitrate=h_bitrate, length=h_length)

    def download_folder(self, user, requested_folder, shares_list, prefix="", recurse=False):

        if requested_folder is None:
            return

        remove_prefix = requested_folder.rsplit('\\', 1)[0]

        for folder, files in shares_list.items():
            if not recurse and requested_folder != folder:
                continue

            if requested_folder not in folder:
                # Not a subfolder of the requested folder, skip
                continue

            # Remember custom download location
            if prefix:
                self.core.transfers.requested_folders[user][folder] = prefix

            # Get final download destination
            destination = self.core.transfers.get_folder_destination(user, folder, remove_prefix)

            if files:
                if config.sections["transfers"]["reverseorder"]:
                    files.sort(key=itemgetter(1), reverse=True)

                for file_data in files:
                    virtualpath = "\\".join([folder, file_data[1]])
                    size = file_data[2]
                    h_bitrate, _bitrate, h_length, _length = get_result_bitrate_length(size, file_data[4])

                    self.core.transfers.get_file(user, virtualpath, destination,
                                                 size=size, bitrate=h_bitrate, length=h_length)

            if not recurse:
                # Downloading a single folder, no need to continue
                return

    def upload_file(self, user, folder, file_data, locally_queued=False):

        virtualpath = "\\".join([folder, file_data[1]])
        size = file_data[2]

        self.core.transfers.push_file(user, virtualpath, size, locally_queued=locally_queued)

    def upload_folder(self, user, requested_folder, shares_list, recurse=False):

        if not requested_folder or not user:
            return

        for folder, files in shares_list.items():
            if not recurse and requested_folder != folder:
                continue

            if requested_folder not in folder:
                # Not a subfolder of the requested folder, skip
                continue

            if files:
                locally_queued = False

                for file_data in files:
                    filename = "\\".join([folder, file_data[1]])
                    size = file_data[2]

                    self.core.transfers.push_file(user, filename, size, locally_queued=locally_queued)
                    locally_queued = True

            if not recurse:
                # Uploading a single folder, no need to continue
                return

    @staticmethod
    def get_soulseek_url(user, path):
        import urllib.parse
        return "slsk://" + urllib.parse.quote("%s/%s" % (user, path.replace("\\", "/")))

    def open_soulseek_url(self, url):

        import urllib.parse

        try:
            user, file_path = urllib.parse.unquote(url[7:]).split("/", 1)
            self.browse_user(user, path=file_path.replace("/", "\\"))

        except Exception:
            log.add(_("Invalid Soulseek URL: %s"), url)

    def show_connection_error(self, username):
        if self.ui_callback:
            self.ui_callback.show_connection_error(username)

    def peer_message_progress(self, msg):
        if self.ui_callback:
            self.ui_callback.peer_message_progress(msg)

    def peer_connection_closed(self, msg):
        if self.ui_callback:
            self.ui_callback.peer_connection_closed(msg)

    def get_user_status(self, msg):
        """ Server code: 7 """

        if self.ui_callback:
            self.ui_callback.get_user_status(msg)

    def shared_file_list(self, msg, user=None):
        """ Peer code: 5 """

        if user is None:
            user = msg.init.target_user

        if self.ui_callback:
            self.ui_callback.shared_file_list(user, msg)
