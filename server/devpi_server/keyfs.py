"""
filesystem key/value storage with support for storing and retrieving
basic python types based on parametrizable keys.  Multiple
read Transactions can execute concurrently while at most one
write Transaction is ongoing.  Each Transaction will see a consistent
view of key/values referring to the point in time it was started,
independent from any future changes.
"""
from __future__ import unicode_literals
import re
import contextlib
import py
from . import mythread
from .fileutil import loads
from .interfaces import IStorageConnection2
from .log import threadlog, thread_push_log, thread_pop_log
from .readonly import get_mutable_deepcopy, ensure_deeply_readonly, \
                      is_deeply_readonly
from .filestore import FileEntry
from .fileutil import read_int_from_file, write_int_to_file
import attr
import time

from devpi_common.types import cached_property


notset = object()


class MissingFileException(Exception):
    def __init__(self, relpath, serial):
        msg = "missing file '%s' at serial %s" % (relpath, serial)
        super(MissingFileException, self).__init__(msg)
        self.relpath = relpath
        self.serial = serial


class TxNotificationThread:
    def __init__(self, keyfs):
        self.keyfs = keyfs
        self.cv_new_event_serial = mythread.threading.Condition()
        self.event_serial_path = str(self.keyfs.basedir.join(".event_serial"))
        self.event_serial_in_sync_at = None
        self._on_key_change = {}

    def on_key_change(self, key, subscriber):
        if mythread.has_active_thread(self):
            raise RuntimeError(
                "cannot register handlers after thread has started")
        keyname = getattr(key, "name", key)
        assert py.builtin._istext(keyname) or py.builtin._isbytes(keyname)
        self._on_key_change.setdefault(keyname, []).append(subscriber)

    def wait_event_serial(self, serial):
        with threadlog.around("info", "waiting for event-serial %s", serial):
            with self.cv_new_event_serial:
                while serial > self.read_event_serial():
                    self.cv_new_event_serial.wait()

    def read_event_serial(self):
        # the disk serial is kept one higher because pre-2.1.2
        # "event_serial" pointed to the "next event serial to be
        # processed" instead of the now "last processed event serial"
        return read_int_from_file(self.event_serial_path, 0) - 1

    def get_event_serial_timestamp(self):
        f = py.path.local(self.event_serial_path)
        retries = 5
        while retries:
            try:
                return f.stat().mtime
            except py.error.ENOENT:
                return
            except py.error.EBUSY:
                retries -= 1
                if not retries:
                    raise
                # let other threads work
                time.sleep(0.001)

    def write_event_serial(self, event_serial):
        write_int_to_file(event_serial + 1, self.event_serial_path)

    def thread_shutdown(self):
        pass

    def tick(self):
        event_serial = self.read_event_serial()
        while event_serial < self.keyfs.get_current_serial():
            self.thread.exit_if_shutdown()
            event_serial += 1
            self._execute_hooks(event_serial, self.log)
            with self.cv_new_event_serial:
                self.write_event_serial(event_serial)
                self.cv_new_event_serial.notify_all()
        serial = self.keyfs.get_current_serial()
        if event_serial >= serial:
            if event_serial == serial:
                self.event_serial_in_sync_at = time.time()
            self.keyfs.wait_tx_serial(serial + 1)
            self.thread.exit_if_shutdown()

    def thread_run(self):
        self.log = thread_push_log("[NOTI]")
        while 1:
            try:
                self.tick()
            except mythread.Shutdown:
                raise
            except MissingFileException as e:
                self.log.warn("Waiting for file %s in event serial %s" % (
                    e.relpath, e.serial))
                self.thread.sleep(5)
            except Exception:
                self.log.exception(
                    "Unhandled exception in notification thread.")
                self.thread.sleep(1.0)

    def get_ixconfig(self, entry, event_serial):
        user = entry.key.params['user']
        index = entry.key.params['index']
        if getattr(self, '_get_ixconfig_cache_serial', None) != event_serial:
            self._get_ixconfig_cache = {}
            self._get_ixconfig_cache_serial = event_serial
        cache_key = (user, index)
        if cache_key in self._get_ixconfig_cache:
            return self._get_ixconfig_cache[cache_key]
        with self.keyfs.transaction(write=False):
            key = self.keyfs.get_key('USER')(user=user)
            value = key.get()
        if value is None:
            # the user doesn't exist anymore
            self._get_ixconfig_cache[cache_key] = None
            return None
        ixconfig = value.get('indexes', {}).get(index)
        if ixconfig is None:
            # the index doesn't exist anymore
            self._get_ixconfig_cache[cache_key] = None
            return None
        self._get_ixconfig_cache[cache_key] = ixconfig
        return ixconfig

    def _execute_hooks(self, event_serial, log, raising=False):
        log.debug("calling hooks for tx%s", event_serial)
        with self.keyfs.get_connection() as conn:
            changes = conn.get_changes(event_serial)
            # we first check for missing files before we call subscribers
            for relpath, (keyname, back_serial, val) in changes.items():
                if keyname in ('STAGEFILE', 'PYPIFILE_NOMD5'):
                    key = self.keyfs.get_key_instance(keyname, relpath)
                    entry = FileEntry(key, val)
                    if entry.meta == {} or entry.last_modified is None:
                        # the file was removed
                        continue
                    ixconfig = self.get_ixconfig(entry, event_serial)
                    if ixconfig is None:
                        # the index doesn't exist (anymore)
                        continue
                    elif ixconfig.get('type') == 'mirror' and ixconfig.get('mirror_use_external_urls', False):
                        # the index uses external URLs now
                        continue
                    if conn.io_file_exists(entry._storepath):
                        # all good
                        continue
                    # the file is missing, check whether we can ignore it
                    serial = self.keyfs.get_current_serial()
                    if event_serial < serial:
                        # there are newer serials existing
                        with self.keyfs.transaction(write=False) as tx:
                            current_val = tx.get(key)
                        if current_val is None:
                            # entry was deleted
                            continue
                        current_entry = FileEntry(key, current_val)
                        if current_entry.meta == {} or current_entry.last_modified is None:
                            # the file was removed at some point
                            continue
                        current_ixconfig = self.get_ixconfig(entry, serial)
                        if current_ixconfig is None:
                            # the index doesn't exist (anymore)
                            continue
                        if current_ixconfig.get('type') == 'mirror':
                            if current_ixconfig.get('mirror_use_external_urls', False):
                                # the index uses external URLs now
                                continue
                            if current_entry.project is None:
                                # this is an old mirror entry with no
                                # project info, so this can be ignored
                                continue
                        log.debug("missing current_entry.meta %r" % current_entry.meta)
                    log.debug("missing entry.meta %r" % entry.meta)
                    raise MissingFileException(relpath, event_serial)
            # all files exist or are deleted in a later serial,
            # call subscribers now
            for relpath, (keyname, back_serial, val) in changes.items():
                subscribers = self._on_key_change.get(keyname, [])
                if not subscribers:
                    continue
                key = self.keyfs.get_key_instance(keyname, relpath)
                ev = KeyChangeEvent(key, val, event_serial, back_serial)
                for sub in subscribers:
                    subname = getattr(sub, "__name__", sub)
                    log.debug("%s(key=%r, at_serial=%r, back_serial=%r",
                              subname, ev.typedkey, event_serial, ev.back_serial)
                    try:
                        sub(ev)
                    except Exception:
                        if raising:
                            raise
                        log.exception("calling %s failed, serial=%s", sub, event_serial)

        log.debug("finished calling all hooks for tx%s", event_serial)


class KeyFS(object):
    """ singleton storage object. """
    class ReadOnly(Exception):
        """ attempt to open write transaction while in readonly mode. """

    def __init__(self, basedir, storage, readonly=False, cache_size=10000):
        self.basedir = py.path.local(basedir).ensure(dir=1)
        self._keys = {}
        self._threadlocal = mythread.threading.local()
        self._cv_new_transaction = mythread.threading.Condition()
        self._import_subscriber = {}
        self.notifier = TxNotificationThread(self)
        self._storage = storage(
            self.basedir,
            notify_on_commit=self._notify_on_commit,
            cache_size=cache_size)
        self._readonly = readonly

    def get_connection(self, closing=True, write=False):
        conn = IStorageConnection2(
            self._storage.get_connection(closing=False, write=write))
        if closing:
            return contextlib.closing(conn)
        return conn

    def finalize_init(self):
        self._storage.perform_crash_recovery()

    def import_changes(self, serial, changes):
        subscriber_task_infos = []
        with self.get_connection(write=True) as conn:
            with conn.write_transaction() as fswriter:
                next_serial = conn.last_changelog_serial + 1
                assert next_serial == serial, (next_serial, serial)
                for relpath, tup in changes.items():
                    keyname, back_serial, val = tup
                    typedkey = self.get_key_instance(keyname, relpath)
                    try:
                        fswriter.record_set(
                            typedkey, get_mutable_deepcopy(val),
                            back_serial=back_serial)
                    except TypeError as e:
                        # for backward compatibility with storage backends
                        # which didn't support the back_serial keyword
                        if not len(e.args) or 'back_serial' not in e.args[0]:
                            raise
                        fswriter.record_set(
                            typedkey, get_mutable_deepcopy(val))
                    meth = self._import_subscriber.get(keyname)
                    if meth is not None:
                        subscriber_task_infos.append(
                            (meth, typedkey, val, back_serial))
                if subscriber_task_infos:
                    with self.transaction(write=False, at_serial=serial):
                        for meth, typedkey, val, back_serial in subscriber_task_infos:
                            threadlog.debug("calling import subscriber %r", meth)
                            meth(fswriter.conn, serial, typedkey, val, back_serial)

    def subscribe_on_import(self, key, subscriber):
        assert key.name not in self._import_subscriber
        self._import_subscriber[key.name] = subscriber

    def _notify_on_commit(self, serial):
        self.release_all_wait_tx()

    def release_all_wait_tx(self):
        with self._cv_new_transaction:
            self._cv_new_transaction.notify_all()

    def wait_tx_serial(self, serial, timeout=None, recheck=1.0):
        """ Return True when the transaction with the serial has been commited.
        Return False if it hasn't happened within a specified timeout.
        If timeout was not specified, we'll wait indefinitely.  In any case,
        this method wakes up every "recheck" seconds to query the database
        in case some other process has produced a commit (in-process commits
        are recognized immediately).
        """
        # we presume that even a few concurrent wait_tx_serial() calls
        # won't cause much pressure on the database.  If that assumption
        # is wrong we have to install a thread which does the
        # db-querying and sets the local condition.
        time_spent = 0

        # recheck time should never be higher than the timeout
        if timeout is not None and recheck > timeout:
            recheck = timeout
        with threadlog.around("debug", "waiting for tx-serial %s", serial):
            with self._cv_new_transaction:
                with self.get_connection() as conn:
                    while serial > conn.db_read_last_changelog_serial():
                        if timeout is not None and time_spent >= timeout:
                            return False
                        self._cv_new_transaction.wait(timeout=recheck)
                        time_spent += recheck
                    return True

    def get_next_serial(self):
        return self.get_current_serial() + 1

    def get_current_serial(self):
        with self.get_connection() as conn:
            return conn.last_changelog_serial

    def get_last_commit_timestamp(self):
        return self._storage.last_commit_timestamp

    @property
    def tx(self):
        return getattr(self._threadlocal, "tx")

    def add_key(self, name, path, type):
        assert isinstance(path, py.builtin._basestring)
        if "{" in path:
            key = PTypedKey(self, path, type, name)
        else:
            key = TypedKey(self, path, type, name)
        if name in self._keys:
            raise ValueError("Duplicate registration for key named '%s'" % name)
        self._keys[name] = key
        setattr(self, name, key)
        if hasattr(self._storage, 'add_key'):
            self._storage.add_key(key)
        return key

    def get_key(self, name):
        return self._keys.get(name)

    def get_key_instance(self, keyname, relpath):
        key = self.get_key(keyname)
        if isinstance(key, PTypedKey):
            key = key(**key.extract_params(relpath))
        return key

    def begin_transaction_in_thread(self, write=False, at_serial=None):
        if write and self._readonly:
            raise self.ReadOnly()
        assert not hasattr(self._threadlocal, "tx")
        tx = Transaction(self, write=write, at_serial=at_serial)
        self._threadlocal.tx = tx
        thread_push_log("[%stx%s]" %("W" if write else "R", tx.at_serial))
        return tx

    def clear_transaction(self):
        thread_pop_log()
        del self._threadlocal.tx

    def restart_as_write_transaction(self):
        if self._readonly:
            raise self.ReadOnly()
        tx = self.tx
        if tx.write:
            raise RuntimeError("Can't restart a write transaction.")
        thread_pop_log()
        tx.restart(write=True)
        thread_push_log("[Wtx%s]" %(tx.at_serial))

    def restart_read_transaction(self):
        tx = self.tx
        if tx.write:
            raise RuntimeError("Can only restart a read transaction.")
        thread_pop_log()
        tx.restart(write=False)
        thread_push_log("[Rtx%s]" %(tx.at_serial))

    def rollback_transaction_in_thread(self):
        try:
            self._threadlocal.tx.rollback()
        finally:
            self.clear_transaction()

    def commit_transaction_in_thread(self):
        try:
            self._threadlocal.tx.commit()
        finally:
            self.clear_transaction()

    @contextlib.contextmanager
    def transaction(self, write=False, at_serial=None):
        tx = self.begin_transaction_in_thread(write=write, at_serial=at_serial)
        try:
            yield tx
        except:
            self.rollback_transaction_in_thread()
            raise
        self.commit_transaction_in_thread()


class PTypedKey:
    rex_braces = re.compile(r'\{(.+?)\}')

    def __init__(self, keyfs, key, type, name):
        self.keyfs = keyfs
        self.pattern = py.builtin._totext(key)
        self.type = type
        self.name = name

        def repl(match):
            name = match.group(1)
            return r'(?P<%s>[^\/]+)' % name
        rex_pattern = self.pattern.replace("+", r"\+")
        rex_pattern = self.rex_braces.sub(repl, rex_pattern)
        self.rex_reverse = re.compile("^" + rex_pattern + "$")

    def __call__(self, **kw):
        for val in kw.values():
            if "/" in val:
                raise ValueError(val)
        relpath = self.pattern.format(**kw)
        return TypedKey(self.keyfs, relpath, self.type, self.name,
                        params=kw)

    def extract_params(self, relpath):
        m = self.rex_reverse.match(relpath)
        return m.groupdict() if m is not None else {}

    def on_key_change(self, callback):
        self.keyfs.notifier.on_key_change(self.name, callback)

    def __repr__(self):
        return "<PTypedKey %r type %r>" %(self.pattern, self.type.__name__)


class KeyChangeEvent:
    def __init__(self, typedkey, value, at_serial, back_serial):
        self.typedkey = typedkey
        self.value = value
        self.at_serial = at_serial
        self.back_serial = back_serial


class TypedKey:
    def __init__(self, keyfs, relpath, type, name, params=None):
        self.keyfs = keyfs
        self.relpath = relpath
        self.type = type
        self.name = name
        self.params = params or {}

    @cached_property
    def params(self):
        key = self.keyfs.get_key(self.name)
        if isinstance(key, PTypedKey):
            return key.extract_params(self.relpath)
        return {}

    def __hash__(self):
        return hash(self.relpath)

    def __eq__(self, other):
        return self.relpath == other.relpath

    def __repr__(self):
        return "<TypedKey %r type %r>" %(self.relpath, self.type.__name__)

    def get(self, readonly=True):
        return self.keyfs.tx.get(self, readonly=readonly)

    @property
    def last_serial(self):
        try:
            return self.keyfs.tx.last_serial(self)
        except KeyError:
            return None

    def is_dirty(self):
        return self.keyfs.tx.is_dirty(self)

    @contextlib.contextmanager
    def update(self):
        val = self.keyfs.tx.get(self, readonly=False)
        yield val
        # no exception, so we can set and thus mark dirty the object
        self.set(val)

    def set(self, val):
        if not isinstance(val, self.type):
            raise TypeError("%r requires value of type %r, got %r" %(
                            self.relpath, self.type.__name__,
                            type(val).__name__))
        self.keyfs.tx.set(self, val)

    def exists(self):
        return self.keyfs.tx.exists(self)

    def delete(self):
        return self.keyfs.tx.delete(self)


@attr.s(slots=True)
class RelpathInfo(object):
    relpath = attr.ib(type=str)
    keyname = attr.ib(type=str)
    serial = attr.ib(type=int)
    back_serial = attr.ib(type=int)
    value = attr.ib()


def get_relpath_at(self, relpath, serial):
    """ Fallback method for legacy storage connections. """
    (keyname, last_serial) = self.db_read_typedkey(relpath)
    serials_and_values = iter_serial_and_value_backwards(
        self, relpath, last_serial)
    try:
        (last_serial, back_serial, val) = next(serials_and_values)
        while last_serial >= 0:
            if last_serial > serial:
                (last_serial, back_serial, val) = next(serials_and_values)
                continue
            return (last_serial, back_serial, val)
    except StopIteration:
        pass
    raise KeyError(relpath)


def iter_serial_and_value_backwards(conn, relpath, last_serial):
    while last_serial >= 0:
        tup = conn.get_changes(last_serial).get(relpath)
        if tup is None:
            raise RuntimeError("no transaction entry at %s" % (last_serial))
        keyname, back_serial, val = tup
        yield (last_serial, back_serial, val)
        last_serial = back_serial

    # we could not find any change below at_serial which means
    # the key didn't exist at that point in time
    return


def iter_relpaths_at(self, typedkeys, at_serial):
    keynames = frozenset(k.name for k in typedkeys)
    seen = set()
    for serial in range(at_serial, -1, -1):
        raw_entry = self.get_raw_changelog_entry(serial)
        changes = loads(raw_entry)[0]
        for relpath, (keyname, back_serial, val) in changes.items():
            if keyname not in keynames:
                continue
            if relpath not in seen:
                seen.add(relpath)
                yield RelpathInfo(
                    relpath=relpath, keyname=keyname,
                    serial=serial, back_serial=back_serial,
                    value=val)


class Transaction(object):
    def __init__(self, keyfs, at_serial=None, write=False):
        self.keyfs = keyfs
        self.commit_serial = None
        self.write = write
        if self.write:
            # open connection immediately
            self.conn
        if at_serial is None:
            at_serial = self.conn.last_changelog_serial
        self.at_serial = at_serial
        self._original = {}
        self.cache = {}
        self.dirty = set()
        self.closed = False
        self.doomed = False
        self._listeners = []

    @cached_property
    def conn(self):
        return self.keyfs.get_connection(
            write=self.write, closing=False)

    def iter_relpaths_at(self, typedkeys, at_serial):
        return self.conn.iter_relpaths_at(typedkeys, at_serial)

    def iter_serial_and_value_backwards(self, relpath, last_serial):
        while last_serial >= 0:
            (last_serial, back_serial, val) = self.conn.get_relpath_at(
                relpath, last_serial)
            yield (last_serial, val)
            last_serial = back_serial

    def get_last_serial_and_value_at(self, typedkey, at_serial, raise_on_error=True):
        relpath = typedkey.relpath
        try:
            (last_serial, back_serial, val) = self.conn.get_relpath_at(relpath, at_serial)
        except KeyError:
            if not raise_on_error:
                return None
            raise
        if val is None and raise_on_error:
            raise KeyError(relpath)  # was deleted
        return (last_serial, val)

    def get_value_at(self, typedkey, at_serial):
        (last_serial, val) = self.get_last_serial_and_value_at(typedkey, at_serial)
        return val

    def last_serial(self, typedkey):
        (last_serial, val) = self.get_last_serial_and_value_at(typedkey, self.at_serial)
        return last_serial

    def derive_key(self, relpath):
        """ return key instance for a given key path."""
        try:
            return self.get_key_in_transaction(relpath)
        except KeyError:
            # XXX we could avoid asking the database
            # if the relpath included the keyname
            # but that's yet another refactoring (tm).
            keyname, serial = self.conn.db_read_typedkey(relpath)
        return self.keyfs.get_key_instance(keyname, relpath)

    def get_key_in_transaction(self, relpath):
        for key in self.cache:
            if key.relpath == relpath:
                return key
        raise KeyError(relpath)

    def is_dirty(self, typedkey):
        return typedkey in self.dirty

    def get_original(self, typedkey):
        """ Return original value from start of transaction,
            without changes from current transaction."""
        try:
            return self._original[typedkey]
        except KeyError:
            # will raise KeyError if it doesn't exist
            val = self.get_value_at(typedkey, self.at_serial)
            assert is_deeply_readonly(val)
            self._original[typedkey] = val
        return val

    def get(self, typedkey, readonly=True):
        """ Return current value referenced by typedkey,
            either as a readonly-view or as a mutable deep copy. """
        try:
            val = self.cache[typedkey]
        except KeyError:
            absent = typedkey in self.dirty
            if not absent:
                try:
                    val = self.get_original(typedkey)
                except KeyError:
                    absent = True
            if absent:
                # for convenience we return an empty instance
                # but below we still respect the readonly property
                val = typedkey.type()
            else:
                assert is_deeply_readonly(val)
                self.cache[typedkey] = val
        if readonly:
            return ensure_deeply_readonly(val)
        else:
            return get_mutable_deepcopy(val)

    def exists(self, typedkey):
        if typedkey in self.cache:
            return True
        if typedkey in self.dirty:
            return False
        try:
            val = self.get_value_at(typedkey, self.at_serial)
        except KeyError:
            return False
        else:
            assert is_deeply_readonly(val)
            self.cache[typedkey] = val
            return True

    def delete(self, typedkey):
        if not self.write:
            raise self.keyfs.ReadOnly()
        self.cache.pop(typedkey, None)
        self.dirty.add(typedkey)

    def set(self, typedkey, val):
        if not self.write:
            raise self.keyfs.ReadOnly()
        # sanity check for dictionaries: we always want to have unicode
        # keys, not bytes
        if typedkey.type == dict:
            check_unicode_keys(val)
        try:
            old_val = self.get_original(typedkey)
        except KeyError:
            old_val = notset
        self.cache[typedkey] = val
        if val != old_val:
            self.dirty.add(typedkey)
        else:
            self.dirty.discard(typedkey)

    def commit(self):
        if self.doomed:
            threadlog.debug("closing doomed transaction")
            return self._close()
        if not self.write:
            return self._close()
        if not self.dirty and not self.conn.dirty_files:
            threadlog.debug("nothing to commit, just closing tx")
            return self._close()
        try:
            with self.conn.write_transaction() as fswriter:
                for typedkey in self.dirty:
                    val = self.cache.get(typedkey)
                    # None signals deletion
                    fswriter.record_set(typedkey, val)
                commit_serial = self.conn.last_changelog_serial + 1
        finally:
            self._close()
        self.commit_serial = commit_serial
        for listener in self._listeners:
            listener()
        return commit_serial

    def on_commit_success(self, callback):
        self._listeners.append(callback)

    def _close(self):
        if self.closed:
            # We can reach this when the transaction is restarted and there
            # is an exception after the commit and before the assignment of
            # the __dict__. The ``transaction`` context manager will call
            # ``rollback``, which then arrives here.
            return
        threadlog.debug("closing transaction at %s", self.at_serial)
        del self.cache
        del self.dirty
        self.conn.close()
        self.closed = True
        return self.at_serial

    def rollback(self):
        if hasattr(self.conn, 'rollback'):
            self.conn.rollback()
        threadlog.debug("transaction rollback at %s" % (self.at_serial))
        return self._close()

    def restart(self, write=False):
        self.commit()
        threadlog.debug(
            "restarting %s transaction afresh as %s transaction",
            "write" if self.write else "read",
            "write" if write else "read")
        newtx = self.__class__(self.keyfs, write=write)
        self.__dict__ = newtx.__dict__

    def doom(self):
        """ mark as doomed to automatically rollback any changes """
        self.doomed = True


def check_unicode_keys(d):
    for key, val in d.items():
        assert not isinstance(key, py.builtin.bytes), repr(key)
        # not allowing bytes seems ok for now, we might need to relax that
        # it certainly helps to get unicode clean
        assert not isinstance(val, py.builtin.bytes), repr(key) + "=" + repr(val)
        if isinstance(val, dict):
            check_unicode_keys(val)
