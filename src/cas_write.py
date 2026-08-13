"""cas-write: compare-and-swap file writes, so two writers cannot silently
overwrite each other.

The problem this solves is a lost update. Two processes read the same file,
each computes new content from what it read, each writes. Both writes succeed,
the second one lands last, and the first one's changes are gone. Nothing
errors, so nobody finds out until someone notices missing text much later.

The mechanism is compare-and-swap with bounded retry:

  1. read the current bytes, hash them
  2. compute the new content from a re-runnable transform of the current text
  3. write the new content to a temp file in the SAME directory, fsync it, then
     os.replace it over the target ONLY if the target's hash still matches the
     one read in step 1 (re-checked immediately before the replace, to keep the
     race window as small as possible)
  4. on a mismatch, re-read, re-run the transform against the FRESH content,
     and try again; after max_retries losing attempts, raise loudly

Two entry points:

  cas_update(path, transform_fn)   read, transform, swap, retry on conflict.
                                   transform_fn(current_text) -> new_text must
                                   be safe to re-run, because it is re-invoked
                                   on each retry against whatever is on disk.

  cas_write(path, content, sha)    one attempt, no retry. Raises on conflict and
                                   changes nothing. For callers that already
                                   computed their content and want
                                   abort-on-conflict semantics.

The compare and the replace happen while holding an exclusive advisory lock on a
sidecar file, because the hash check alone is not enough. Between "the hash still
matches" and "the rename landed" there is a write and an fsync, which takes long
enough that another writer usually lands inside it. Measured on the first run of
the concurrency test in tests/: six threads, thirty appends, twenty-four of them
silently lost. The lock closes that window for every writer that takes it, and
the hash check stays as the net that catches writers that do not.

Honest limits, stated here because a file-writing helper that oversells itself is
worse than none:

  * It only fully guards writers that go through this module. An editor, an
    agent's file-write tool, or a process in another language does not take the
    lock. Those get caught by the hash check most of the time, and can still win
    the small remaining race between the check and the rename.
  * If transform_fn is not safe to re-run, a retry produces a wrong result with
    no error at all. That is the sharpest way to misuse this.
  * The advisory lock is POSIX only (fcntl.flock), so this module does not import
    on Windows.
  * A sidecar <target>.lock file is created next to the target and deliberately
    left in place. Removing it would race with the next writer.

Standard library only. No third-party imports, no network.
"""

import contextlib
import fcntl
import hashlib
import os
import stat
import tempfile

__all__ = [
    "CASConflict",
    "CASRetriesExhausted",
    "sha256_bytes",
    "file_sha",
    "read_with_sha",
    "cas_write",
    "cas_update",
]


class CASConflict(Exception):
    """A single cas_write attempt lost the race (on-disk hash != expected)."""


class CASRetriesExhausted(Exception):
    """cas_update used up max_retries consecutive losing attempts."""


def sha256_bytes(data: bytes) -> str:
    """sha256 hex digest of the given bytes."""
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    """Current sha256 of the file's bytes, or None if the file does not exist.

    None is a real value here, not an error: it is the hash you pass as
    expected_sha when you mean "I expect to be creating this file".
    """
    try:
        with open(path, "rb") as f:
            return sha256_bytes(f.read())
    except FileNotFoundError:
        return None


def read_with_sha(path, *, encoding="utf-8"):
    """Return (text, sha) for the file. Raises FileNotFoundError if absent."""
    with open(path, "rb") as f:
        data = f.read()
    return data.decode(encoding), sha256_bytes(data)


def lock_path_for(path):
    """Path of the sidecar lock file used to serialize writers of the target."""
    return str(path) + ".lock"


@contextlib.contextmanager
def _exclusive(path):
    """Hold an exclusive advisory lock across the compare and the replace.

    The lock lives on a sidecar file rather than on the target, because every
    successful write replaces the target's inode. A lock held on the target
    would stop excluding anybody the moment the first write landed, since later
    writers would open a different inode and lock that instead.
    """
    fd = os.open(lock_path_for(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic_replace(path, new_text, *, encoding="utf-8"):
    """Write new_text to a temp file in the target's directory, then replace.

    The temp file is created in the target's own directory on purpose. os.replace
    is only atomic within a single filesystem, so a temp file in the system temp
    dir could land on a different volume and turn the atomic rename into a
    copy that can be interrupted half-written.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cas-", suffix=".tmp")
    try:
        # mkstemp creates the temp file 0600, and os.replace carries the temp
        # file's mode onto the target. Without this, updating a 0644 file that
        # other processes read would silently tighten it to 0600 and break them.
        # A missing target means we are creating the file, so the safe 0600
        # default is the right one to keep.
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        except FileNotFoundError:
            pass
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def cas_write(path, new_content, expected_sha, *, encoding="utf-8"):
    """One compare-and-swap attempt.

    Atomically replace the file with new_content ONLY if its current sha256
    still equals expected_sha. Raises CASConflict, changing nothing, if it does
    not. Pass expected_sha=None to mean "the file should not exist yet".

    The check and the replace are done under an exclusive lock, so they are one
    critical section rather than two steps with a writable gap between them.

    Returns the sha256 of the content that was written.
    """
    with _exclusive(path):
        current = file_sha(path)
        if current != expected_sha:
            raise CASConflict(
                "%s changed since it was read: expected sha %s, found %s"
                % (path,
                   (expected_sha or "MISSING")[:12] + "...",
                   (current or "MISSING")[:12] + "...")
            )
        _atomic_replace(path, new_content, encoding=encoding)
    return sha256_bytes(new_content.encode(encoding))


def cas_update(path, transform_fn, *, max_retries=5, encoding="utf-8"):
    """Read, transform, compare-and-swap, retrying on conflict.

    transform_fn(current_text) -> new_text runs against the current content, and
    the result is written only if the file has not changed since that read. On a
    conflict the file is re-read and transform_fn runs again on the fresh
    content, up to max_retries attempts, after which CASRetriesExhausted is
    raised rather than forcing the write. transform_fn must therefore be safe to
    re-run.

    The file must already exist. Use cas_write with expected_sha=None to create
    one.

    Returns (new_text, attempts), where attempts is the 1-based number of the
    attempt that succeeded.
    """
    last = None
    for attempt in range(1, max_retries + 1):
        text, sha = read_with_sha(path, encoding=encoding)
        new_text = transform_fn(text)
        try:
            cas_write(path, new_text, sha, encoding=encoding)
            return new_text, attempt
        except CASConflict as exc:
            last = exc
            continue
    raise CASRetriesExhausted(
        "%s: compare-and-swap failed after %d attempt(s) (last: %s)"
        % (path, max_retries, last)
    )
