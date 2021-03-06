from collections import namedtuple
import os

import sys
import tempfile
import threading
from typing import Dict, Generator, Optional
import hashlib
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

from robotframework_ls.constants import NULL
from robocorp_ls_core.robotframework_log import get_logger

from robotframework_ls.impl.protocols import ILibspecManager
from robotframework_ls.impl.robot_specbuilder import LibraryDoc

from robocorp_ls_core.protocols import Sentinel

log = get_logger(__name__)

LibspecErrorEntry = namedtuple("LibspecError", "libname arguments alias uri")


def _normfile(filename):
    return os.path.abspath(os.path.normpath(os.path.normcase(filename)))


def _get_libspec_mutex_name(libspec_filename):
    from robocorp_ls_core.system_mutex import generate_mutex_name

    libspec_filename = _norm_filename(libspec_filename)
    basename = os.path.basename(libspec_filename)
    name = os.path.splitext(basename)[0]
    return generate_mutex_name(libspec_filename, prefix="%s_" % (name,))


def _get_additional_info_filename(spec_filename):
    additional_info_filename = os.path.join(spec_filename + ".m")
    return additional_info_filename


def _load_library_doc_and_mtime(spec_filename, obtain_mutex=True):
    """
    :param obtain_mutex:
        Should be False if this is part of a bigger operation that already
        has the spec_filename mutex.
    """
    from robotframework_ls.impl import robot_specbuilder
    from robocorp_ls_core.system_mutex import timed_acquire_mutex

    if obtain_mutex:
        ctx = timed_acquire_mutex(_get_libspec_mutex_name(spec_filename))
    else:
        ctx = NULL
    with ctx:
        # We must load it with a mutex to avoid conflicts between generating/reading.
        builder = robot_specbuilder.SpecDocBuilder()
        try:
            mtime = os.path.getmtime(spec_filename)
            libdoc = builder.build(spec_filename)
            return libdoc, mtime
        except Exception:
            log.exception(
                "Error when loading spec info from: %s", spec_filename)
            return None


def _load_lib_info(canonical_spec_filename, can_regenerate):
    libdoc_and_mtime = _load_library_doc_and_mtime(canonical_spec_filename)
    if libdoc_and_mtime is None:
        return None
    libdoc, mtime = libdoc_and_mtime
    return _LibInfo(libdoc, mtime, canonical_spec_filename, can_regenerate)


_IS_BUILTIN = "is_builtin"
_ARGUMENTS = "arguments"
_ALIAS = "alias"
_SOURCE_TO_MTIME = "source_to_mtime"
_UNABLE_TO_LOAD = "unable_to_load"


def _create_updated_source_to_mtime(library_doc):
    sources = set()

    source = library_doc.source
    if source is not None:
        sources.add(source)

    for keyword in library_doc.keywords:
        source = keyword.source
        if source is not None:
            sources.add(source)

    source_to_mtime = {}
    for source in sources:
        try:
            # i.e.: get it before normalizing (but leave the cache key normalized).
            # This is because even on windows the file-system may end up being
            # case-dependent on some cases.
            mtime = os.path.getmtime(source)
            source = _normfile(source)
            source_to_mtime[source] = mtime
        except Exception:
            log.exception("Unable to load source for file: %s", source)
    return source_to_mtime


def _create_additional_info(spec_filename, is_builtin, obtain_mutex=True, arguments=None, alias=None):
    try:
        additional_info = {_IS_BUILTIN: is_builtin,
                           _ARGUMENTS: arguments, _ALIAS: alias}
        if is_builtin:
            # For builtins we don't have to check the mtime
            # (on a new version we update the folder).
            return additional_info

        library_doc_and_mtime = _load_library_doc_and_mtime(
            spec_filename, obtain_mutex=obtain_mutex
        )
        if library_doc_and_mtime is None:
            additional_info[_UNABLE_TO_LOAD] = True
            return additional_info

        library_doc = library_doc_and_mtime[0]

        additional_info[_SOURCE_TO_MTIME] = _create_updated_source_to_mtime(
            library_doc)
        return additional_info

    except BaseException as e:
        log.exception(
            "Error creating additional info for spec filename: %s\n%s", spec_filename, e)
        return {}


def _load_spec_filename_additional_info(spec_filename):
    """
    Loads additional information given a spec filename.
    """
    import json

    try:
        additional_info_filename = _get_additional_info_filename(spec_filename)

        with open(additional_info_filename, "r") as stream:
            return json.load(stream)
    except BaseException as e:
        log.exception("Unable to load source mtimes from: %s\n%s",
                      spec_filename, e)
        return {}


def _dump_spec_filename_additional_info(spec_filename, is_builtin, obtain_mutex=True, arguments=None, alias=None):
    """
    Creates a filename with additional information not directly available in the
    spec.
    """
    import json

    source_to_mtime = _create_additional_info(
        spec_filename, is_builtin, obtain_mutex=obtain_mutex, arguments=arguments, alias=alias
    )
    additional_info_filename = _get_additional_info_filename(spec_filename)
    with open(additional_info_filename, "w") as stream:
        json.dump(source_to_mtime, stream, indent=2, sort_keys=True)


class _LibInfo(object):
    __slots__ = [
        "library_doc",
        "mtime",
        "_canonical_spec_filename",
        "_additional_info",
        "_invalid",
        "_can_regenerate",
    ]

    def __init__(self, library_doc, mtime, spec_filename, can_regenerate):
        """
        :param library_doc:
        :param mtime:
        :param spec_filename:
        :param bool can_regenerate:
            False means that the information from this file can't really be
            regenerated (i.e.: this is a spec file from a library or created
            by the user).
        """
        assert library_doc
        assert mtime
        assert spec_filename

        self.library_doc = library_doc
        self.mtime = mtime

        self._can_regenerate = can_regenerate
        self._canonical_spec_filename = spec_filename
        self._additional_info = None
        self._invalid = False

    @property
    def additional_info(self):
        if self._additional_info is None:
            self._additional_info = _load_spec_filename_additional_info(
                self._canonical_spec_filename)
        return self._additional_info

    @property
    def alias(self):
        return self.additional_info.get(_ALIAS, None)

    @property
    def arguments(self):
        return tuple(self.additional_info.get(_ARGUMENTS, None) or [])

    def verify_sources_sync(self, arguments, alias):
        """
        :return bool:
            True if everything is ok and this library info can be used. Otherwise,
            the spec file and the _LibInfo must be recreated.
        """
        if not self._can_regenerate:
            # This means that this info was generated by a library or the user
            # himself, thus, we can't regenerate it.
            return True

        if self._invalid:  # Once invalid, always invalid.
            return False

        additional_info = self.additional_info
        if additional_info is not None:
            if additional_info.get(_IS_BUILTIN, False):
                return True

            source_to_mtime = additional_info.get(_SOURCE_TO_MTIME)
            if source_to_mtime is None:
                # Nothing to validate...
                return True

            updated_source_to_mtime = _create_updated_source_to_mtime(
                self.library_doc)
            if source_to_mtime != updated_source_to_mtime:
                log.info(
                    "Library %s is invalid. Current source to mtime:\n%s\nChanged from:\n%s"
                    % (self.library_doc.name, source_to_mtime, updated_source_to_mtime)
                )
                self._invalid = True
                return False

            try:
                if self.arguments != arguments:
                    log.info(
                        "Library %s is invalid. Arguments changes" % self.library_doc.name)
                    self._invalid = True
                    return False
            except BaseException:
                pass

        return True


def _norm_filename(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


class _FolderInfo(object):
    def __init__(self, folder_path, recursive):
        self.folder_path = folder_path
        self.recursive = recursive
        self.libspec_canonical_filename_to_info = {}
        self._watch = NULL
        self._lock = threading.Lock()

    def start_watch(self, observer, notifier):
        with self._lock:
            if self._watch is NULL:
                if not os.path.isdir(self.folder_path):
                    if not os.path.exists(self.folder_path):
                        log.info(
                            "Trying to track changes in path which does not exist: %s",
                            self.folder_path,
                        )
                    else:
                        log.info(
                            "Trying to track changes in path which is not a folder: %s",
                            self.folder_path,
                        )
                    return

                log.info("Tracking folder for changes: %s", self.folder_path)
                from robocorp_ls_core.watchdog_wrapper import PathInfo

                folder_path = self.folder_path
                self._watch = observer.notify_on_any_change(
                    [PathInfo(folder_path, recursive=self.recursive)],
                    notifier.on_change,
                    (self._on_change_spec,),
                )

    def _on_change_spec(self, spec_file):
        with self._lock:
            spec_file_key = _norm_filename(spec_file)
            # Just add/remove that specific spec file from the tracked list.
            libspec_canonical_filename_to_info = (
                self.libspec_canonical_filename_to_info.copy()
            )
            if os.path.exists(spec_file):
                libspec_canonical_filename_to_info[spec_file_key] = None
            else:
                libspec_canonical_filename_to_info.pop(spec_file_key, None)

            self.libspec_canonical_filename_to_info = libspec_canonical_filename_to_info

    def synchronize(self):
        with self._lock:
            try:
                self.libspec_canonical_filename_to_info = self._collect_libspec_info(
                    [self.folder_path],
                    self.libspec_canonical_filename_to_info,
                    recursive=self.recursive,
                )
            except Exception:
                log.exception("Error when synchronizing: %s", self.folder_path)

    def dispose(self):
        with self._lock:
            watch = self._watch
            self._watch = NULL
            watch.stop_tracking()
            self.libspec_canonical_filename_to_info = {}

    def _collect_libspec_info(self, folders, old_libspec_filename_to_info, recursive):
        seen_libspec_files = set()
        if recursive:
            for folder in folders:
                if os.path.isdir(folder):
                    for root, _dirs, files in os.walk(folder):
                        for filename in files:
                            if filename.lower().endswith(".libspec"):
                                seen_libspec_files.add(
                                    os.path.join(root, filename))
        else:
            for folder in folders:
                if os.path.isdir(folder):
                    for filename in os.listdir(folder):
                        if filename.lower().endswith(".libspec"):
                            seen_libspec_files.add(
                                os.path.join(folder, filename))

        new_libspec_filename_to_info = {}

        for filename in seen_libspec_files:
            filename = _norm_filename(filename)
            info = old_libspec_filename_to_info.get(filename)
            if info is not None:
                try:
                    curr_mtime = os.path.getmtime(filename)
                except BaseException:
                    # it was deleted in the meanwhile...
                    continue
                else:
                    if info.mtime != curr_mtime:
                        # The spec filename mtime changed, so, set to None
                        # to reload it.
                        info = None

            new_libspec_filename_to_info[filename] = info
        return new_libspec_filename_to_info


def dummy_process():
    pass


class LibspecManager(ILibspecManager):
    """
    Used to manage the libspec files.

    .libspec files are searched in the following directories:

    - PYTHONPATH folders                                  (not recursive)
    - Workspace folders                                   (recursive -- notifications from the LSP)
    - ${user}.robotframework-ls/specs/${python_hash}      (not recursive)

    It searches for .libspec files in the folders tracked and provides the
    keywords that are available from those (properly caching data as needed).
    """

    @classmethod
    def get_robot_version(cls):
        try:
            import robot

            v = str(robot.get_version())
        except BaseException:
            log.exception("Unable to get robot version.")
            v = "unknown"
        return v

    @classmethod
    def get_robot_major_version(cls):
        robot_version = cls.get_robot_version()

        major_version = 3
        try:
            if "." in robot_version:
                major_version = int(robot_version.split(".")[0])
        except BaseException:
            log.exception("Unable to get robot major version.")

        return major_version

    @classmethod
    def get_internal_libspec_dir(cls):
        from robotframework_ls import robot_config

        home = robot_config.get_robotframework_ls_home()

        pyexe = sys.executable
        if not isinstance(pyexe, bytes):
            pyexe = pyexe.encode("utf-8")

        digest = hashlib.sha256(pyexe).hexdigest()[:8]

        v = cls.get_robot_version()

        # Note: _v1: information on the mtime of the libspec sources now available.
        return os.path.join(home, "specs", "%s_%s" % (digest, v))

    @classmethod
    def get_internal_builtins_libspec_dir(cls, internal_libspec_dir=None):
        return os.path.join(
            internal_libspec_dir or cls.get_internal_libspec_dir(), "builtins"
        )

    def __init__(self, builtin_libspec_dir=None, user_libspec_dir=None):
        """
        :param __internal_libspec_dir__:
            Only to be used in tests (to regenerate the builtins)!
        """

        from robocorp_ls_core import watchdog_wrapper

        self._libspec_failures_cache: Dict[tuple, bool] = {}

        self._main_thread = threading.current_thread()

        self._observer = watchdog_wrapper.create_observer()

        self._file_changes_notifier = watchdog_wrapper.create_notifier(
            self._on_file_changed, timeout=0.5
        )

        self._root_uri = None

        self._libspec_dir = self.get_internal_libspec_dir()

        self._user_libspec_dir = user_libspec_dir or os.path.join(
            self._libspec_dir, "user"
        )
        self._builtins_libspec_dir = (
            builtin_libspec_dir or self.get_internal_builtins_libspec_dir(
                self._libspec_dir)
        )
        log.debug("User libspec dir: %s", self._user_libspec_dir)
        log.debug("Builtins libspec dir: %s", self._builtins_libspec_dir)

        try:
            os.makedirs(self._user_libspec_dir)
        except BaseException:
            # Ignore exception if it's already created.
            pass
        try:
            os.makedirs(self._builtins_libspec_dir)
        except BaseException:
            # Ignore exception if it's already created.
            pass

        self.libspec_errors: Dict[LibspecErrorEntry, str] = {}
        self.libspec_warnings: Dict[LibspecErrorEntry, str] = {}

        # Spec info found in the workspace
        self._workspace_folder_uri_to_folder_info = {}
        self._additional_pythonpath_folder_to_folder_info = {}

        # Spec info found in the pythonpath
        pythonpath_folder_to_folder_info = {}
        for path in sys.path:
            if path and os.path.isdir(path):
                pythonpath_folder_to_folder_info[path] = _FolderInfo(
                    path, recursive=False
                )
        self._pythonpath_folder_to_folder_info = pythonpath_folder_to_folder_info

        # Spec info found in internal dirs (autogenerated)
        self._internal_folder_to_folder_info = {
            self._user_libspec_dir: _FolderInfo(
                self._user_libspec_dir, recursive=False
            ),
            self._builtins_libspec_dir: _FolderInfo(
                self._builtins_libspec_dir, recursive=False
            ),
        }

        # Must be set from the outside world when needed.
        self.config = None

        self.process_pool = ProcessPoolExecutor()

        # we have to submit a dummy process to start the process pool,
        # because it does not start correctly if we do it from another
        # thread
        self.process_pool.submit(dummy_process).result()

        self.thread_pool = ThreadPoolExecutor(thread_name_prefix="libspec_manager_")

        log.debug("Generating builtin libraries.")
        self._gen_builtin_libraries()
        log.debug("Synchronizing internal caches.")
        self._synchronize()
        log.debug("Finished initializing LibspecManager.")

    def __del__(self):
        self.thread_pool.shutdown(False, cancel_futures=True)
        self.process_pool.shutdown(False, cancel_futures=True)

    def _check_in_main_thread(self):
        curr_thread = threading.current_thread()
        if self._main_thread is not curr_thread:
            raise AssertionError(
                f"This may only be called at the thread: {self._main_thread}. Current thread: {curr_thread}"
            )

    @property
    def root_uri(self):
        return self._root_uri

    @root_uri.setter
    def root_uri(self, value):
        self._root_uri = value

    @property
    def config(self):
        return self._config

    @config.setter
    def config(self, config):
        self._check_in_main_thread()
        from robotframework_ls.impl.robot_lsp_constants import OPTION_ROBOT_PYTHONPATH

        self._config = config
        existing_entries = set(
            self._additional_pythonpath_folder_to_folder_info.keys())
        if config is not None:
            pythonpath_entries = set(
                config.get_setting(OPTION_ROBOT_PYTHONPATH, list, [])
            )
            for new_pythonpath_entry in pythonpath_entries:
                if new_pythonpath_entry not in existing_entries:
                    self.add_additional_pythonpath_folder(new_pythonpath_entry)
            for old_entry in existing_entries:
                if old_entry not in pythonpath_entries:
                    self.remove_additional_pythonpath_folder(old_entry)

        self.synchronize_additional_pythonpath_folders()

    @property
    def user_libspec_dir(self):
        return self._user_libspec_dir

    def _on_file_changed(self, spec_file, folder_info_on_change_spec):
        log.debug("File change detected: %s", spec_file)

        # Check if the cache related to libspec generation failure must be
        # cleared.
        fix = False
        for cache_key in self._libspec_failures_cache:
            libname = cache_key[0]
            if libname in spec_file:
                fix = True
                break
        if fix:
            new = {}
            for cache_key, value in self._libspec_failures_cache.items():
                libname = cache_key[0]
                if libname not in spec_file:
                    new[cache_key] = value
            # Always set as a whole (to avoid racing conditions).
            self._libspec_failures_cache = new

        # Notify _FolderInfo._on_change_spec
        if spec_file.lower().endswith(".libspec"):
            folder_info_on_change_spec(spec_file)

    def add_workspace_folder(self, folder_uri: str):
        self._check_in_main_thread()
        from robocorp_ls_core import uris

        if folder_uri not in self._workspace_folder_uri_to_folder_info:
            log.debug("Added workspace folder: %s", folder_uri)
            cp = self._workspace_folder_uri_to_folder_info.copy()
            folder_info = cp[folder_uri] = _FolderInfo(
                uris.to_fs_path(folder_uri), recursive=True
            )
            self._workspace_folder_uri_to_folder_info = cp
            folder_info.start_watch(
                self._observer, self._file_changes_notifier)
            folder_info.synchronize()
        else:
            log.debug("Workspace folder already added: %s", folder_uri)

    def remove_workspace_folder(self, folder_uri: str):
        self._check_in_main_thread()
        if folder_uri in self._workspace_folder_uri_to_folder_info:
            log.debug("Removed workspace folder: %s", folder_uri)
            cp = self._workspace_folder_uri_to_folder_info.copy()
            folder_info = cp.pop(folder_uri, NULL)
            folder_info.dispose()
            self._workspace_folder_uri_to_folder_info = cp
        else:
            log.debug("Workspace folder already removed: %s", folder_uri)

    def add_additional_pythonpath_folder(self, folder_path):
        self._check_in_main_thread()

        from robocorp_ls_core import uris

        if folder_path not in self._additional_pythonpath_folder_to_folder_info:
            log.debug("Added additional pythonpath folder: %s", folder_path)
            cp = self._additional_pythonpath_folder_to_folder_info.copy()

            real_path = os.path.abspath(os.path.join(os.path.normpath(uris.to_fs_path(
                self.root_uri)), folder_path)) if self.root_uri is not None and not os.path.isabs(folder_path) else folder_path

            folder_info = cp[folder_path] = _FolderInfo(
                real_path, recursive=True)
            self._additional_pythonpath_folder_to_folder_info = cp
            folder_info.start_watch(
                self._observer, self._file_changes_notifier)
            folder_info.synchronize()
        else:
            log.debug(
                "Additional pythonpath folder already added: %s", folder_path)

    def remove_additional_pythonpath_folder(self, folder_path):
        self._check_in_main_thread()
        if folder_path in self._additional_pythonpath_folder_to_folder_info:
            log.debug("Removed additional pythonpath folder: %s", folder_path)
            cp = self._additional_pythonpath_folder_to_folder_info.copy()
            folder_info = cp.pop(folder_path, NULL)
            folder_info.dispose()
            self._additional_pythonpath_folder_to_folder_info = cp
        else:
            log.debug(
                "Additional pythonpath folder already removed: %s", folder_path)

    def _gen_builtin_libraries(self):
        """
        Generates .libspec files for the libraries builtin (if needed).
        """

        try:
            import time
            from robotframework_ls.impl import robot_constants
            from robocorp_ls_core.system_mutex import timed_acquire_mutex
            from robocorp_ls_core.system_mutex import generate_mutex_name

            initial_time = time.time()
            wait_for = []

            log.debug("Waiting for mutex to generate builtins.")
            with timed_acquire_mutex(
                generate_mutex_name(
                    _norm_filename(self._builtins_libspec_dir),
                    prefix="gen_builtins_",
                ),
                timeout=100,
            ):
                log.debug("Obtained mutex to generate builtins.")

                def get_builtins():
                    try:
                        import robot.libraries
                        return robot.libraries.STDLIBS
                    except:
                        pass

                    return robot_constants.STDLIBS

                for libname in get_builtins():
                    builtins_libspec_dir = self._builtins_libspec_dir
                    if not os.path.exists(
                        os.path.join(builtins_libspec_dir,
                                     f"{libname}.libspec")
                    ):
                        wait_for.append(
                            self.thread_pool.submit(
                                self._create_libspec, libname, is_builtin=True
                            )
                        )

                for future in wait_for:
                    future.result()

            if wait_for:
                log.debug(
                    "Total time to generate builtins: %.2fs"
                    % (time.time() - initial_time)
                )
                self.synchronize_internal_libspec_folders()

        except BaseException:
            log.exception("Error creating builtin libraries.")
        finally:
            log.info("Finished creating builtin libraries.")

    def synchronize_workspace_folders(self):
        for folder_info in self._workspace_folder_uri_to_folder_info.values():
            folder_info.start_watch(
                self._observer, self._file_changes_notifier)
            folder_info.synchronize()

    def synchronize_pythonpath_folders(self):
        for folder_info in self._pythonpath_folder_to_folder_info.values():
            folder_info.start_watch(
                self._observer, self._file_changes_notifier)
            folder_info.synchronize()

    def synchronize_additional_pythonpath_folders(self):
        for folder_info in self._additional_pythonpath_folder_to_folder_info.values():
            folder_info.start_watch(
                self._observer, self._file_changes_notifier)
            folder_info.synchronize()

    def synchronize_internal_libspec_folders(self):
        for folder_info in self._internal_folder_to_folder_info.values():
            folder_info.start_watch(
                self._observer, self._file_changes_notifier)
            folder_info.synchronize()

    def _synchronize(self):
        """
        Updates the internal caches related to the tracked .libspec files found.

        This can be a slow call as it may traverse the whole workspace folders
        hierarchy, so, it should be used only during startup to fill the initial
        info.
        """
        self.synchronize_workspace_folders()
        self.synchronize_pythonpath_folders()
        self.synchronize_additional_pythonpath_folders()
        self.synchronize_internal_libspec_folders()

    def _iter_lib_info(self) -> Generator[_LibInfo, None, None]:
        # Note: the iteration order is important (first ones are visited earlier
        # and have higher priority).
        iter_in = []
        for (_uri, info) in self._workspace_folder_uri_to_folder_info.items():
            if info.libspec_canonical_filename_to_info:
                iter_in.append(
                    (info.libspec_canonical_filename_to_info, False))

        for (_uri, info) in self._pythonpath_folder_to_folder_info.items():
            if info.libspec_canonical_filename_to_info:
                iter_in.append(
                    (info.libspec_canonical_filename_to_info, False))

        for (_uri, info) in self._additional_pythonpath_folder_to_folder_info.items():
            if info.libspec_canonical_filename_to_info:
                iter_in.append(
                    (info.libspec_canonical_filename_to_info, False))

        for (_uri, info) in self._internal_folder_to_folder_info.items():
            if info.libspec_canonical_filename_to_info:
                iter_in.append((info.libspec_canonical_filename_to_info, True))

        for canonical_filename_to_info, can_regenerate in iter_in:
            for canonical_spec_filename, info in list(
                canonical_filename_to_info.items()
            ):

                if info is None:
                    info = canonical_filename_to_info[
                        canonical_spec_filename
                    ] = _load_lib_info(canonical_spec_filename, can_regenerate)

                # Note: we could end up yielding a library with the same name
                # multiple times due to its scope. It's up to the caller to
                # validate that.

                # Note: we also check if there are keywords available... in
                # some cases we may create libraries for namespace packages
                # (i.e.: empty folders) which don't really have anything -- in
                # this case, this isn't a valid library.
                if (info is not None and info.library_doc is not None):
                    yield info

    def get_library_names(self):
        return sorted(set(lib_info.library_doc.name for lib_info in self._iter_lib_info()))

    def _create_libspec(
        self,
        libname,
        env=None,
        log_time=True,
        cwd=None,
        additional_path=None,
        is_builtin=False,
        arguments=None,
        alias=None,
        current_doc_uri=None
    ):
        cache_key = (libname, is_builtin, arguments)

        not_created = self._libspec_failures_cache.get(
            cache_key, Sentinel.SENTINEL)
        if not_created is Sentinel.SENTINEL:
            created = self._cached_create_libspec(
                libname, env, log_time, cwd, additional_path, is_builtin, arguments, alias, current_doc_uri)
            if not created:
                # Always set as a whole (to avoid racing conditions).
                cp = self._libspec_failures_cache.copy()
                cp[cache_key] = False
                self._libspec_failures_cache = cp

        return not_created

    def _subprocess_check_output(self, *args, **kwargs):
        # Only done for mocking.
        from robocorp_ls_core.subprocess_wrapper import subprocess

        return subprocess.check_output(*args, **kwargs)

    def _cached_create_libspec(
        self,
        libname,
        env,
        log_time,
        cwd,
        additional_path,
        is_builtin,
        arguments, alias, current_doc_uri
    ):
        """
        :param str libname:
        :raise Exception: if unable to create the library.
        """
        import time
        from robotframework_ls.impl import robot_constants
        from robocorp_ls_core.system_mutex import timed_acquire_mutex
        from multiprocessing import Process
        from robotframework_ls.impl.generate_libdoc import run_doc

        curtime = time.time()

        try:
            try:
                additional_pythonpath_entries = list(
                    [x.folder_path for x in self._additional_pythonpath_folder_to_folder_info.values()]
                )

                libargs = "::".join(arguments) if arguments is not None and len(
                    arguments) > 0 else None

                libspec_dir = self._user_libspec_dir
                if libname in robot_constants.STDLIBS:
                    libspec_dir = self._builtins_libspec_dir

                encoded_libname = libname
                if libargs is not None:
                    digest = hashlib.sha256(libargs.encode()).hexdigest()[:8]
                    encoded_libname += f"__{digest}"

                libspec_filename = os.path.join(
                    libspec_dir, encoded_libname + ".libspec")

                libspec_error_entry = LibspecErrorEntry(
                    libname, arguments, alias, current_doc_uri)

                if libspec_error_entry in self.libspec_errors:
                    del self.libspec_errors[libspec_error_entry]

                if libspec_error_entry in self.libspec_warnings:
                    del self.libspec_warnings[libspec_error_entry]

                log.debug(
                    f"Obtaining mutex to generate libpsec: {libspec_filename}.")

                # TODO define builtin vars
                variables = {
                    "${CURDIR}": additional_path,
                    "${TEMPDIR}": os.path.abspath(tempfile.gettempdir()),
                    "${EXECDIR}": os.path.abspath("."),

                    "${/}": os.sep,
                    "${:}": os.pathsep,
                    "${\\n}": os.linesep,

                    '${SPACE}': ' ',
                    '${True}': True,
                    '${False}': False,
                    '${None}': None,
                    '${null}': None,

                    "${TEST NAME}": None,
                    "@{TEST TAGS}": [],
                    "${TEST DOCUMENTATION}": None,
                    "${TEST STATUS}": None,
                    "${TEST MESSAGE}": None,
                    "${PREV TEST NAME}": None,
                    "${PREV TEST STATUS}": None,
                    "${PREV TEST MESSAGE}": None,
                    "${SUITE NAME}": None,
                    "${SUITE SOURCE}": None,
                    "${SUITE DOCUMENTATION}": None,
                    "&{SUITE METADATA}": {},
                    "${SUITE STATUS}": None,
                    "${SUITE MESSAGE}": None,
                    "${KEYWORD STATUS}": None,
                    "${KEYWORD MESSAGE}": None,
                    "${LOG LEVEL}": None,
                    "${OUTPUT FILE}": None,
                    "${LOG FILE}": None,
                    "${REPORT FILE}": None,
                    "${DEBUG FILE}": None,
                    "${OUTPUT DIR}": None,
                }

                with timed_acquire_mutex(_get_libspec_mutex_name(libspec_filename)):
                    try:
                        # remove old
                        if os.path.exists(libspec_filename):
                            log.info("remove old spec file %s",
                                     libspec_filename)
                            os.remove(libspec_filename)
                            additional_libspec_filename = _get_additional_info_filename(
                                libspec_filename)
                            if os.path.exists(additional_libspec_filename):
                                os.remove(additional_libspec_filename)

                        future = self.process_pool.submit(
                            run_doc, f"{libname}{f'::{libargs}' if libargs else ''}", libspec_filename, additional_path, additional_pythonpath_entries, variables)

                        _, error, warning = future.result(100)

                        if warning is not None:
                            self.libspec_warnings[libspec_error_entry] = warning
                        if error is not None:
                            self.libspec_errors[libspec_error_entry] = error
                        else:
                            _dump_spec_filename_additional_info(
                                libspec_filename, is_builtin=is_builtin, obtain_mutex=False, arguments=arguments, alias=alias)
                    except BaseException as e:
                        self.libspec_errors[libspec_error_entry] = str(e)
                        raise

                    return True
            except Exception as e:
                log.exception("Error creating libspec: %s\n%s", libname, e)

                return False

        finally:
            if log_time:
                delta = time.time() - curtime
                log.debug("Took: %.2fs to generate info for: %s" %
                          (delta, libname))

    def dispose(self):
        self._observer.dispose()
        self._file_changes_notifier.dispose()

    def _do_create_libspec_on_get(self, libname, current_doc_uri, arguments, alias):
        from robocorp_ls_core import uris

        additional_path = None
        abspath = None
        cwd = None
        if current_doc_uri is not None:
            cwd = os.path.dirname(uris.to_fs_path(current_doc_uri))
            if not cwd or not os.path.isdir(cwd):
                cwd = None

        if os.path.isabs(libname):
            abspath = libname

        elif current_doc_uri is not None:
            # relative path: let's make it absolute
            fs_path = os.path.dirname(uris.to_fs_path(current_doc_uri))
            abspath = os.path.abspath(os.path.join(fs_path, libname))

        if abspath:
            additional_path = os.path.dirname(abspath)
            libname = os.path.basename(libname)
            if libname.lower().endswith((".py", ".class", ".java")):
                libname = os.path.splitext(libname)[0]

        if self._create_libspec(libname, additional_path=additional_path, cwd=cwd, arguments=arguments, alias=alias, current_doc_uri=current_doc_uri):
            self.synchronize_internal_libspec_folders()
            return True
        return False

    def get_library_info(self, libname, create=True, current_doc_uri=None, arguments=(), alias=None) -> Optional[LibraryDoc]:
        """
        :param libname:
            It may be a library name, a relative path to a .py file or an
            absolute path to a .py file.

        :rtype: LibraryDoc
        """

        if libname is None:
            return None

        libname_lower = libname.lower()
        if libname_lower.endswith((".py", ".class", ".java")):
            libname_lower = os.path.splitext(libname_lower)[0]

        if "/" in libname_lower or "\\" in libname_lower:
            libname_lower = os.path.basename(libname_lower)

        for lib_info in self._iter_lib_info():
            library_doc = lib_info.library_doc
            if library_doc.name and library_doc.name.lower() == libname_lower and lib_info.arguments == arguments or ():
                if not lib_info.verify_sources_sync(arguments, alias):
                    if create:
                        # Found but it's not in sync. Try to regenerate (don't proceed
                        # because we don't want to match a lower priority item, so,
                        # regenerate and get from the cache without creating).
                        self._do_create_libspec_on_get(
                            libname, current_doc_uri, arguments, alias)

                        # Note: get even if it if was not created (we may match
                        # a lower priority library).
                        return self.get_library_info(
                            libname, create=False, current_doc_uri=current_doc_uri,
                            arguments=arguments, alias=alias
                        )
                    else:
                        # Not in sync and it should not be created, just skip it.
                        continue
                else:
                    return library_doc

        if create:
            if self._do_create_libspec_on_get(libname, current_doc_uri, arguments, alias):
                return self.get_library_info(
                    libname, create=False, current_doc_uri=current_doc_uri,
                    arguments=arguments, alias=alias
                )

        log.debug("Unable to find library named: %s", libname)
        return None

    def get_library_error(self, libname, current_doc_uri=None, arguments=(), alias=None):
        return self.libspec_errors.get(LibspecErrorEntry(libname, arguments, alias, current_doc_uri), None)

    def get_library_warning(self, libname, current_doc_uri=None, arguments=(), alias=None):
        return self.libspec_warnings.get(LibspecErrorEntry(libname, arguments, alias, current_doc_uri), None)
