# COPYRIGHT (C) 2020-2022 Nicotine+ Contributors
# COPYRIGHT (C) 2016-2017 Michael Labouebe <gfarmerfr@free.fr>
# COPYRIGHT (C) 2016 Mutnick <muhing@yahoo.com>
# COPYRIGHT (C) 2009-2011 quinox <quinox@users.sf.net>
# COPYRIGHT (C) 2009 daelstorm <daelstorm@gmail.com>
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

import gc
import importlib.util
import os
import pickle
import shelve
import stat
import sys
import time

from threading import Thread

from pynicotine import slskmessages
from pynicotine.config import config
from pynicotine.logfacility import log
from pynicotine.slskmessages import UINT_LIMIT
from pynicotine.slskmessages import UserStatus
from pynicotine.utils import TRANSLATE_PUNCTUATION
from pynicotine.utils import encode_path
from pynicotine.utils import rename_process

""" Check if there's an appropriate (performant) database type for shelves """

if importlib.util.find_spec("_gdbm"):

    def shelve_open_gdbm(filename, flag='c', protocol=None, writeback=False):
        import _gdbm  # pylint: disable=import-error
        return shelve.Shelf(_gdbm.open(filename, flag), protocol, writeback)

    shelve.open = shelve_open_gdbm

elif importlib.util.find_spec("semidbm"):

    import semidbm  # pylint: disable=import-error
    try:
        # semidbm throws an exception when calling sync on a read-only dict, avoid this
        del semidbm.db._SemiDBMReadOnly.sync  # pylint: disable=protected-access

    except AttributeError:
        pass

    def shelve_open_semidbm(filename, flag='c', protocol=None, writeback=False):
        return shelve.Shelf(semidbm.open(filename, flag), protocol, writeback)

    shelve.open = shelve_open_semidbm

else:
    log.add(_("Cannot find %(option1)s or %(option2)s, please install either one.") % {
        "option1": "python3-gdbm",
        "option2": "semidbm"
    })
    sys.exit()


class Scanner:
    """ Separate process responsible for building shares. It handles scanning of
    folders and files, as well as building databases and writing them to disk. """

    def __init__(self, config_obj, queue, shared_folders, share_db_paths, init=False, rescan=True, rebuild=False):

        self.config = config_obj
        self.queue = queue
        self.shared_folders, self.shared_buddy_folders = shared_folders
        self.share_dbs = {}
        self.share_db_paths = share_db_paths
        self.init = init
        self.rescan = rescan
        self.rebuild = rebuild
        self.tinytag = None
        self.version = 2

    def run(self):

        try:
            rename_process(b'nicotine-scan')

            from pynicotine.external.tinytag import TinyTag
            self.tinytag = TinyTag()

            if not Shares.load_shares(self.share_dbs, self.share_db_paths, remove_failed=True):
                # Failed to load shares, rebuild
                self.rescan = self.rebuild = True

            if self.init:
                self.create_compressed_shares()

            if self.rescan:
                start_num_folders = len(list(self.share_dbs.get("buddyfiles", {})))

                self.queue.put((_("Rescanning shares…"), None, None))
                self.queue.put((_("%(num)s folders found before rescan, rebuilding…"),
                               {"num": start_num_folders}, None))

                new_mtimes, new_files, new_streams = self.rescan_dirs("normal", rebuild=self.rebuild)
                _new_mtimes, new_files, _new_streams = self.rescan_dirs("buddy", new_mtimes, new_files,
                                                                        new_streams, self.rebuild)

                self.queue.put((_("Rescan complete: %(num)s folders found"), {"num": len(new_files)}, None))

                self.create_compressed_shares()

        except Exception:
            from traceback import format_exc

            self.queue.put((
                _("Serious error occurred while rescanning shares. If this problem persists, "
                  "delete %(dir)s/*.db and try again. If that doesn't help, please file a bug "
                  "report with this stack trace included: %(trace)s"), {
                    "dir": self.config.data_dir,
                    "trace": "\n" + format_exc()
                }, None
            ))
            self.queue.put(Exception("Scanning failed"))

        finally:
            Shares.close_shares(self.share_dbs)

    def create_compressed_shares_message(self, share_type):
        """ Create a message that will later contain a compressed list of our shares """

        if share_type == "normal":
            streams = self.share_dbs.get("streams")
        else:
            streams = self.share_dbs.get("buddystreams")

        compressed_shares = slskmessages.SharedFileList(shares=streams)
        compressed_shares.make_network_message()
        compressed_shares.list = None
        compressed_shares.type = share_type

        self.queue.put(compressed_shares)

    def create_compressed_shares(self):
        self.create_compressed_shares_message("normal")
        self.create_compressed_shares_message("buddy")

    def create_db_file(self, destination):

        share_db = self.share_dbs.get(destination)

        if share_db is not None:
            share_db.close()

        db_path = os.path.join(self.config.data_dir, destination + ".db")
        self.remove_db_file(db_path)

        return shelve.open(db_path, flag='n', protocol=pickle.HIGHEST_PROTOCOL)

    @staticmethod
    def remove_db_file(db_path):

        db_path_encoded = encode_path(db_path)

        if os.path.isfile(db_path_encoded):
            os.remove(db_path_encoded)

        elif os.path.isdir(db_path_encoded):
            import shutil
            shutil.rmtree(db_path_encoded)

    def real2virtual(self, path):

        path = path.replace('/', '\\')

        for virtual, real, *_unused in Shares.virtual_mapping(self.config):
            # Remove slashes from share name to avoid path conflicts
            virtual = virtual.replace('/', '_').replace('\\', '_')

            real = real.replace('/', '\\')
            if path == real:
                return virtual

            # Use rstrip to remove trailing separator from root directories
            real = real.rstrip('\\') + '\\'

            if path.startswith(real):
                virtualpath = virtual + '\\' + path[len(real):]
                return virtualpath

        return "__INTERNAL_ERROR__" + path

    def set_shares(self, share_type, files=None, streams=None, mtimes=None, wordindex=None):

        self.config.create_data_folder()

        storable_objects = [
            (files, "files"),
            (streams, "streams"),
            (mtimes, "mtimes"),
            (wordindex, "wordindex")
        ]

        for source, destination in storable_objects:
            if source is None:
                continue

            try:
                if share_type == "buddy":
                    destination = "buddy" + destination

                self.share_dbs[destination] = share_db = self.create_db_file(destination)
                share_db.update(source)

            except Exception as error:
                self.queue.put((_("Can't save %(filename)s: %(error)s"),
                                {"filename": destination + ".db", "error": error}, None))
                return

    def rescan_dirs(self, share_type, mtimes=None, files=None, streams=None, rebuild=False):
        """
        Check for modified or new files via OS's last mtime on a directory,
        or, if rebuild is True, all directories
        """

        # Reset progress
        self.queue.put("indeterminate")

        if share_type == "buddy":
            shared_folders = (x[1] for x in self.shared_buddy_folders)
            prefix = "buddy"
        else:
            shared_folders = (x[1] for x in self.shared_folders)
            prefix = ""

        new_files = {}
        new_streams = {}
        new_mtimes = {}

        if files is not None:
            new_files = {**files, **new_files}

        if streams is not None:
            new_streams = {**streams, **new_streams}

        if mtimes is not None:
            new_mtimes = {**mtimes, **new_mtimes}

        old_mtimes = self.share_dbs.get(prefix + "mtimes")
        share_version = None

        if old_mtimes is not None:
            share_version = old_mtimes.get("__NICOTINE_SHARE_VERSION__")

        # Rebuild shares if share version is outdated
        if share_version is None or share_version < self.version:
            rebuild = True

        for folder in shared_folders:
            # Get mtimes for top-level shared folders, then every subfolder
            try:
                files, streams, mtimes = self.get_files_list(
                    folder, old_mtimes, self.share_dbs.get(prefix + "files"),
                    self.share_dbs.get(prefix + "streams"), rebuild
                )
                new_files = {**new_files, **files}
                new_streams = {**new_streams, **streams}
                new_mtimes = {**new_mtimes, **mtimes}

            except OSError as error:
                self.queue.put((_("Error while scanning folder %(path)s: %(error)s"), {
                    'path': folder,
                    'error': error
                }, None))

        # Save data to databases
        new_mtimes["__NICOTINE_SHARE_VERSION__"] = self.version
        self.set_shares(share_type, files=new_files, streams=new_streams, mtimes=new_mtimes)

        # Update Search Index
        # wordindex is a dict in format {word: [num, num, ..], ... } with num matching keys in newfileindex
        # fileindex is a dict in format { num: (path, size, (bitrate, vbr), length), ... }
        wordindex = self.get_files_index(new_files, prefix + "fileindex")

        # Save data to databases
        self.set_shares(share_type, wordindex=wordindex)

        del wordindex
        gc.collect()

        return new_mtimes, new_files, new_streams

    @staticmethod
    def is_hidden(folder, filename=None, entry_stat=None):
        """ Stop sharing any dot/hidden directories/files """

        # If the last folder in the path starts with a dot, we exclude it
        if filename is None:
            last_folder = os.path.basename(os.path.normpath(folder.replace('\\', '/')))

            if last_folder.startswith("."):
                return True

        # If we're asked to check a file we exclude it if it start with a dot
        if filename is not None and filename.startswith("."):
            return True

        # Check if file is marked as hidden on Windows
        if sys.platform == "win32":
            if len(folder) == 3 and folder[1] == ":" and folder[2] in ("\\", "/"):
                # Root directories are marked as hidden, but we allow scanning them
                return False

            if entry_stat is None:
                if filename is not None:
                    entry_stat = os.stat(encode_path(folder + '\\' + filename))
                else:
                    entry_stat = os.stat(encode_path(folder))

            return entry_stat.st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN

        return False

    def get_files_list(self, folder, oldmtimes, oldfiles, oldstreams, rebuild=False, folder_stat=None):
        """ Get a list of files with their filelength, bitrate and track length in seconds """

        if folder_stat is None:
            folder_stat = os.stat(encode_path(folder))

        folder_unchanged = False
        virtual_folder = self.real2virtual(folder)
        mtime = folder_stat.st_mtime

        file_list = []
        files = {}
        streams = {}
        mtimes = {folder: mtime}

        if not rebuild and folder in oldmtimes and mtime == oldmtimes[folder]:
            try:
                files[virtual_folder] = oldfiles[virtual_folder]
                streams[virtual_folder] = oldstreams[virtual_folder]
                folder_unchanged = True

            except KeyError:
                self.queue.put(("Inconsistent cache for '%(vdir)s', rebuilding '%(dir)s'", {
                    'vdir': virtual_folder,
                    'dir': folder
                }, "miscellaneous"))

        try:
            with os.scandir(encode_path(folder, prefix=False)) as entries:
                for entry in entries:
                    entry_stat = entry.stat()

                    if entry.is_file():
                        try:
                            if not folder_unchanged:
                                filename = entry.name.decode("utf-8", "replace")

                                if self.is_hidden(folder, filename, entry_stat):
                                    continue

                                # Get the metadata of the file
                                path = entry.path.decode("utf-8", "replace")
                                data = self.get_file_info(filename, path, self.tinytag, entry_stat)
                                file_list.append(data)

                        except Exception as error:
                            self.queue.put((_("Error while scanning file %(path)s: %(error)s"),
                                           {'path': entry.path, 'error': error}, None))

                        continue

                    path = entry.path.decode("utf-8", "replace").replace('\\', os.sep)

                    if self.is_hidden(path, entry_stat=entry_stat):
                        continue

                    dir_files, dir_streams, dir_mtimes = self.get_files_list(
                        path, oldmtimes, oldfiles, oldstreams, rebuild, entry_stat
                    )

                    files = {**files, **dir_files}
                    streams = {**streams, **dir_streams}
                    mtimes = {**mtimes, **dir_mtimes}

        except OSError as error:
            self.queue.put((_("Error while scanning folder %(path)s: %(error)s"),
                           {'path': folder, 'error': error}, None))

        if not folder_unchanged:
            files[virtual_folder] = file_list
            streams[virtual_folder] = self.get_dir_stream(file_list)

        return files, streams, mtimes

    def get_file_info(self, name, pathname, tinytag, file_stat=None):
        """ Get file metadata """

        audio = None
        audio_info = None
        duration = None

        if file_stat is None:
            file_stat = os.stat(encode_path(pathname))

        size = file_stat.st_size

        # We skip metadata scanning of files without meaningful content
        if size > 128:
            try:
                audio = tinytag.get(encode_path(pathname), size, tags=False)

            except Exception as error:
                self.queue.put((_("Error while scanning metadata for file %(path)s: %(error)s"),
                               {'path': pathname, 'error': error}, None))

        if audio is not None:
            bitrate = audio.bitrate
            samplerate = audio.samplerate
            bitdepth = audio.bitdepth
            duration = audio.duration

            if bitrate is not None:
                bitrate = int(bitrate + 0.5)  # Round the value with minimal performance loss

                if not UINT_LIMIT > bitrate >= 0:
                    bitrate = None

            if duration is not None:
                duration = int(duration)

                if not UINT_LIMIT > duration >= 0:
                    duration = None

            if samplerate is not None:
                samplerate = int(samplerate)

                if not UINT_LIMIT > samplerate >= 0:
                    samplerate = None

            if bitdepth is not None:
                bitdepth = int(bitdepth)

                if not UINT_LIMIT > bitdepth >= 0:
                    bitdepth = None

            audio_info = (bitrate, int(audio.is_vbr), samplerate, bitdepth)

        return [name, size, audio_info, duration]

    @staticmethod
    def get_dir_stream(folder):
        """ Pack all files and metadata in directory """

        message = slskmessages.FileSearchResult()
        stream = bytearray()
        stream.extend(message.pack_uint32(len(folder)))

        for fileinfo in folder:
            stream.extend(message.pack_file_info(fileinfo))

        return stream

    def get_files_index(self, shared_files, fileindex_dest):
        """ Update Search index with new files """

        """ We dump data directly into the file index database to save memory.
        For the word index db, we can't use the same approach, as we need to access
        dict elements frequently. This would take too long to access from disk. """

        self.share_dbs[fileindex_dest] = fileindex_db = self.create_db_file(fileindex_dest)

        wordindex = {}
        file_index = -1
        num_shared_files = len(shared_files)
        last_percent = 0.0

        for file_num, folder in enumerate(shared_files, start=1):
            # Truncate the percentage to two decimal places to avoid sending data to the GUI thread too often
            percent = float("%.2f" % (file_num / num_shared_files))

            if last_percent < percent <= 1.0:
                self.queue.put(percent)
                last_percent = percent

            for file_index, fileinfo in enumerate(shared_files[folder], start=file_index + 1):
                fileinfo = fileinfo[:]
                filename = fileinfo[0]

                # Add to file index
                fileinfo[0] = folder + '\\' + filename
                fileindex_db[repr(file_index)] = fileinfo

                # Collect words from filenames for Search index
                # Use set to prevent duplicates
                for k in set((folder + " " + filename).lower().translate(TRANSLATE_PUNCTUATION).split()):
                    try:
                        wordindex[k].append(file_index)
                    except KeyError:
                        wordindex[k] = [file_index]

        return wordindex


class Shares:

    def __init__(self, core, queue, network_callback=None, ui_callback=None, init_shares=True):

        self.core = core
        self.network_callback = network_callback
        self.ui_callback = ui_callback
        self.queue = queue
        self.share_dbs = {}
        self.requested_share_times = {}
        self.pending_network_msgs = []
        self.rescanning = False
        self.should_compress_shares = False
        self.compressed_shares_normal = slskmessages.SharedFileList()
        self.compressed_shares_buddy = slskmessages.SharedFileList()

        self.convert_shares()
        self.share_db_paths = [
            ("files", os.path.join(config.data_dir, "files.db")),
            ("streams", os.path.join(config.data_dir, "streams.db")),
            ("wordindex", os.path.join(config.data_dir, "wordindex.db")),
            ("fileindex", os.path.join(config.data_dir, "fileindex.db")),
            ("mtimes", os.path.join(config.data_dir, "mtimes.db")),
            ("buddyfiles", os.path.join(config.data_dir, "buddyfiles.db")),
            ("buddystreams", os.path.join(config.data_dir, "buddystreams.db")),
            ("buddywordindex", os.path.join(config.data_dir, "buddywordindex.db")),
            ("buddyfileindex", os.path.join(config.data_dir, "buddyfileindex.db")),
            ("buddymtimes", os.path.join(config.data_dir, "buddymtimes.db"))
        ]

        if not init_shares:
            return

        self.init_shares()

    def server_disconnect(self):
        self.requested_share_times.clear()
        self.pending_network_msgs.clear()

    """ Shares-related actions """

    def init_shares(self):

        rescan_startup = (config.sections["transfers"]["rescanonstartup"]
                          and not config.need_config())

        self.rescan_shares(init=True, rescan=rescan_startup)

    def virtual2real(self, path):

        path = path.replace('/', os.sep).replace('\\', os.sep)

        for virtual, real, *_unused in self.virtual_mapping(config):
            # Remove slashes from share name to avoid path conflicts
            virtual = virtual.replace('/', '_').replace('\\', '_')

            if path == virtual:
                return real

            if path.startswith(virtual + os.sep):
                realpath = real.rstrip('/\\') + path[len(virtual):]
                return realpath

        return "__INTERNAL_ERROR__" + path

    @staticmethod
    def virtual_mapping(config_obj):

        mapping = config_obj.sections["transfers"]["shared"][:]
        mapping += config_obj.sections["transfers"]["buddyshared"]

        return mapping

    @staticmethod
    def get_normalized_virtual_name(virtual_name, shared_folders):

        # Provide a default name for root folders
        if not virtual_name:
            virtual_name = "Shared"

        # Remove slashes from share name to avoid path conflicts
        virtual_name = virtual_name.replace('/', '_').replace('\\', '_')
        new_virtual_name = str(virtual_name)

        # Check if virtual share name is already in use
        counter = 1
        while new_virtual_name in (x[0] for x in shared_folders):
            new_virtual_name = virtual_name + str(counter)
            counter += 1

        return new_virtual_name

    def convert_shares(self):
        """ Convert fs-based shared to virtual shared (pre 1.4.0) """

        def _convert_to_virtual(shared_folder):
            if isinstance(shared_folder, tuple):
                return shared_folder

            virtual = shared_folder.replace('/', '_').replace('\\', '_').strip('_')
            log.add("Renaming shared folder '%s' to '%s'. A rescan of your share is required.",
                    (shared_folder, virtual))
            return virtual, shared_folder

        config.sections["transfers"]["shared"] = [_convert_to_virtual(x)
                                                  for x in config.sections["transfers"]["shared"]]
        config.sections["transfers"]["buddyshared"] = [_convert_to_virtual(x)
                                                       for x in config.sections["transfers"]["buddyshared"]]

    @classmethod
    def load_shares(cls, shares, dbs, remove_failed=False):

        errors = []
        exception = None

        for destination, db_path in dbs:
            db_path_encoded = encode_path(db_path)

            try:
                if os.path.exists(db_path_encoded):
                    shares[destination] = shelve.open(db_path, flag='r', protocol=pickle.HIGHEST_PROTOCOL)

            except Exception:
                from traceback import format_exc

                errors.append(db_path)
                exception = format_exc()

                if remove_failed:
                    Scanner.remove_db_file(db_path)

        if not errors:
            return True

        log.add(_("Failed to process the following databases: %(names)s"), {
            'names': '\n'.join(errors)
        })
        log.add(exception)
        return False

    def file_is_shared(self, user, virtualfilename, realfilename):

        log.add_transfer("Checking if file is shared: %(virtual_name)s with real path %(path)s", {
            "virtual_name": virtualfilename,
            "path": realfilename
        })

        folder, _sep, file = virtualfilename.rpartition('\\')
        shared_files = self.share_dbs.get("files")
        bshared_files = self.share_dbs.get("buddyfiles")
        file_is_shared = False

        if not realfilename.startswith("__INTERNAL_ERROR__"):
            if bshared_files is not None:
                user_row = self.core.userlist.buddies.get(user)

                if user_row:
                    # Check if buddy is trusted
                    if config.sections["transfers"]["buddysharestrustedonly"] and not user_row[4]:
                        pass

                    else:
                        for fileinfo in bshared_files.get(str(folder), []):
                            if file == fileinfo[0]:
                                file_is_shared = True
                                break

            if not file_is_shared and shared_files is not None:
                for fileinfo in shared_files.get(str(folder), []):
                    if file == fileinfo[0]:
                        file_is_shared = True
                        break

        if not file_is_shared:
            log.add_transfer(("File is not present in the database of shared files, not sharing: "
                              "%(virtual_name)s with real path %(path)s"), {
                "virtual_name": virtualfilename,
                "path": realfilename
            })
            return False

        return True

    def get_compressed_shares_message(self, share_type):
        """ Returns the compressed shares message. Creates a new one if necessary, e.g.
        if an individual file was added to our shares. """

        if self.should_compress_shares:
            self.rescan_shares(init=True, rescan=False)

        if share_type == "normal":
            return self.compressed_shares_normal

        if share_type == "buddy":
            return self.compressed_shares_buddy

        return None

    @staticmethod
    def close_shares(share_dbs):

        dbs = [
            "files", "streams", "wordindex",
            "fileindex", "mtimes",
            "buddyfiles", "buddystreams", "buddywordindex",
            "buddyfileindex", "buddymtimes"
        ]

        for database in dbs:
            db_file = share_dbs.get(database)

            if db_file is not None:
                share_dbs[database].close()
                del share_dbs[database]

    def send_num_shared_folders_files(self):
        """ Send number publicly shared files to the server. """

        if not (self.core and self.core.user_status != UserStatus.OFFLINE):
            return

        if self.rescanning:
            return

        try:
            shared = self.share_dbs.get("files")
            index = self.share_dbs.get("fileindex")

            if shared is None or index is None:
                shared = {}
                index = []

            try:
                sharedfolders = len(shared)
                sharedfiles = len(index)

            except TypeError:
                sharedfolders = len(list(shared))
                sharedfiles = len(list(index))

            self.queue.append(slskmessages.SharedFoldersFiles(sharedfolders, sharedfiles))

        except Exception as error:
            log.add(_("Failed to send number of shared files to the server: %s"), error)

    """ Scanning """

    def build_scanner_process(self, shared_folders=None, init=False, rescan=True, rebuild=False):

        import multiprocessing

        # Frozen binaries only support fork (if not on Windows)
        if sys.platform != "win32" and getattr(sys, 'frozen', False):
            start_method = "fork"
        else:
            start_method = "spawn"

        multiprocessing.set_start_method(start_method, force=True)

        scanner_queue = multiprocessing.Queue()
        scanner = Scanner(
            config,
            scanner_queue,
            shared_folders,
            self.share_db_paths,
            init,
            rescan,
            rebuild
        )
        scanner = multiprocessing.Process(target=scanner.run)
        scanner.daemon = True
        return scanner, scanner_queue

    def rebuild_shares(self, use_thread=True):
        return self.rescan_shares(rebuild=True, use_thread=use_thread)

    def get_shared_folders(self):

        shared_folders = config.sections["transfers"]["shared"][:]
        shared_folders_buddy = config.sections["transfers"]["buddyshared"][:]

        return shared_folders, shared_folders_buddy

    def process_scanner_messages(self, scanner, scanner_queue):

        while scanner.is_alive():
            # Cooldown
            time.sleep(0.05)

            while not scanner_queue.empty():
                item = scanner_queue.get()

                if isinstance(item, Exception):
                    return True

                if isinstance(item, tuple):
                    template, args, log_level = item
                    log.add(template, args, log_level)

                elif isinstance(item, float):
                    if self.ui_callback:
                        self.ui_callback.set_scan_progress(item)
                    else:
                        log.add(_("Rescan progress: %s"), str(int(item * 100)) + " %")

                elif item == "indeterminate" and self.ui_callback:
                    self.ui_callback.show_scan_progress()
                    self.ui_callback.set_scan_indeterminate()

                elif isinstance(item, slskmessages.SharedFileList):
                    if item.type == "normal":
                        self.compressed_shares_normal = item

                    elif item.type == "buddy":
                        self.compressed_shares_buddy = item

                    self.should_compress_shares = False

        return False

    def check_shares_available(self):

        share_groups = self.get_shared_folders()
        unavailable_shares = []

        for share in share_groups:
            for virtual_name, folder_path, *_unused in share:
                if not os.access(encode_path(folder_path), os.R_OK):
                    unavailable_shares.append((virtual_name, folder_path))

        return unavailable_shares

    def rescan_shares(self, init=False, rescan=True, rebuild=False, use_thread=True, force=False):

        if self.rescanning:
            return None

        if rescan and not force:
            # Verify all shares are mounted before allowing destructive rescan
            unavailable_shares = self.check_shares_available()

            if unavailable_shares:
                log.add(_("Rescan aborted due to unavailable shares"))
                rescan = False

                if self.ui_callback:
                    self.ui_callback.shares_unavailable(unavailable_shares)

                if not init:
                    return None

        # Hand over database control to the scanner process
        self.rescanning = True
        self.close_shares(self.share_dbs)

        shared_folders = self.get_shared_folders()
        scanner, scanner_queue = self.build_scanner_process(shared_folders, init, rescan, rebuild)
        scanner.start()

        if use_thread:
            Thread(
                target=self._process_scanner, args=(scanner, scanner_queue), name="ProcessShareScanner", daemon=True
            ).start()
            return None

        return self._process_scanner(scanner, scanner_queue)

    def _process_scanner(self, scanner, scanner_queue):

        # Let the scanner process do its thing
        error = self.process_scanner_messages(scanner, scanner_queue)

        if self.ui_callback:
            self.ui_callback.hide_scan_progress()

        # Scanning done, load shares in the main process again
        self.load_shares(self.share_dbs, self.share_db_paths)
        self.rescanning = False

        if not error:
            self.send_num_shared_folders_files()

        # Process any file transfer queue requests that arrived while scanning
        if self.network_callback:
            self.network_callback(self.pending_network_msgs)

        return error

    """ Network Messages """

    def get_shared_file_list(self, msg):
        """ Peer code: 4 """

        user = msg.init.target_user
        request_time = time.time()

        if user in self.requested_share_times and request_time < self.requested_share_times[user] + 0.4:
            # Ignoring request, because it's less than half a second since the
            # last one by this user
            return

        self.requested_share_times[user] = request_time

        log.add(_("User %(user)s is browsing your list of shared files"), {'user': user})

        ip_address, _port = msg.init.addr
        checkuser, reason = self.core.network_filter.check_user(user, ip_address)

        if not checkuser:
            message = self.core.ban_message % reason
            self.core.privatechat.send_automatic_message(user, message)

        shares_list = None

        if checkuser == 1:
            # Send Normal Shares
            shares_list = self.get_compressed_shares_message("normal")

        elif checkuser == 2:
            # Send Buddy Shares
            shares_list = self.get_compressed_shares_message("buddy")

        if not shares_list:
            # Nyah, Nyah
            shares_list = slskmessages.SharedFileList(init=msg.init)

        shares_list.init = msg.init
        self.queue.append(shares_list)

    def folder_contents_request(self, msg):
        """ Peer code: 36 """

        init = msg.init
        ip_address, _port = msg.init.addr
        username = msg.init.target_user
        checkuser, reason = self.core.network_filter.check_user(username, ip_address)

        if not checkuser:
            message = self.core.ban_message % reason
            self.core.privatechat.send_automatic_message(username, message)

        normalshares = self.share_dbs.get("streams")
        buddyshares = self.share_dbs.get("buddystreams")

        if checkuser == 1 and normalshares is not None:
            shares = normalshares

        elif checkuser == 2 and buddyshares is not None:
            shares = buddyshares

        else:
            shares = {}

        if checkuser:
            try:
                if msg.dir in shares:
                    self.queue.append(slskmessages.FolderContentsResponse(
                        init=init, directory=msg.dir, token=msg.token, shares=shares[msg.dir]))
                    return

                if msg.dir.rstrip('\\') in shares:
                    self.queue.append(slskmessages.FolderContentsResponse(
                        init=init, directory=msg.dir, token=msg.token, shares=shares[msg.dir.rstrip('\\')]))
                    return

            except Exception as error:
                log.add(_("Failed to fetch the shared folder %(folder)s: %(error)s"),
                        {"folder": msg.dir, "error": error})

            self.queue.append(slskmessages.FolderContentsResponse(init=init, directory=msg.dir, token=msg.token))

    """ Quit """

    def quit(self):
        self.close_shares(self.share_dbs)
