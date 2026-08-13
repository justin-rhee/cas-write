#!/usr/bin/env python3
"""Offline test suite for cas-write. No network, no credentials, no fixtures
outside a temp directory that is created and thrown away per check.

    python3 tests/test_cas_write.py

Exit 0 only if every check passes.
"""

import os
import shutil
import stat
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import cas_write as cw

PASS = 0
FAIL = 0


def check(name, fn):
    """Run one check. A check is a function that raises on failure."""
    global PASS, FAIL
    d = tempfile.mkdtemp(prefix="cas-write-test-")
    try:
        fn(d)
        print("  ok    %s" % name)
        PASS += 1
    except Exception as exc:
        print("  FAIL  %s: %s: %s" % (name, type(exc).__name__, exc))
        FAIL += 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


def write(path, text, mode=None):
    with open(path, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def read(path):
    with open(path) as f:
        return f.read()


def temps_in(d):
    return [n for n in os.listdir(d) if n.startswith(".cas-")]


# --- the hash helpers ------------------------------------------------------

def t_file_sha_missing(d):
    assert cw.file_sha(os.path.join(d, "nope.md")) is None, "missing file should hash to None"


def t_read_with_sha_missing(d):
    try:
        cw.read_with_sha(os.path.join(d, "nope.md"))
    except FileNotFoundError:
        return
    raise AssertionError("read_with_sha should raise FileNotFoundError")


# --- cas_write -------------------------------------------------------------

def t_write_happy(d):
    p = write(os.path.join(d, "f.md"), "one\n")
    sha = cw.file_sha(p)
    returned = cw.cas_write(p, "one\ntwo\n", sha)
    assert read(p) == "one\ntwo\n", "content not written"
    assert returned == cw.file_sha(p), "returned sha does not match what is on disk"


def t_write_conflict_changes_nothing(d):
    p = write(os.path.join(d, "f.md"), "one\n")
    stale = cw.file_sha(p)
    write(p, "someone else got here first\n")   # another writer lands
    try:
        cw.cas_write(p, "my update\n", stale)
    except cw.CASConflict:
        assert read(p) == "someone else got here first\n", "conflict must change nothing"
        return
    raise AssertionError("expected CASConflict")


def t_write_creates_when_absent(d):
    p = os.path.join(d, "new.md")
    cw.cas_write(p, "fresh\n", None)
    assert read(p) == "fresh\n", "file should have been created"


def t_write_refuses_create_over_existing(d):
    p = write(os.path.join(d, "f.md"), "already here\n")
    try:
        cw.cas_write(p, "clobber\n", None)
    except cw.CASConflict:
        assert read(p) == "already here\n", "must not clobber"
        return
    raise AssertionError("expected CASConflict when expecting absence")


# --- cas_update ------------------------------------------------------------

def t_update_happy(d):
    p = write(os.path.join(d, "f.md"), "one\n")
    text, attempts = cw.cas_update(p, lambda t: t + "two\n")
    assert text == "one\ntwo\n", "transform result wrong"
    assert attempts == 1, "should succeed on the first attempt, got %d" % attempts
    assert read(p) == "one\ntwo\n", "disk content wrong"


def t_update_retries_against_fresh_content(d):
    """The headline property: a losing attempt re-runs the transform on the
    content the winner left behind, so the winner's line survives."""
    p = write(os.path.join(d, "f.md"), "base\n")
    state = {"n": 0}

    def transform(current):
        state["n"] += 1
        if state["n"] == 1:
            # a competing writer lands between our read and our swap
            write(p, current + "theirs\n")
        return current + "mine\n"

    text, attempts = cw.cas_update(p, transform)
    assert attempts == 2, "expected 2 attempts, got %d" % attempts
    assert text == "base\ntheirs\nmine\n", "lost update, got %r" % text
    assert read(p) == "base\ntheirs\nmine\n", "disk content wrong: %r" % read(p)


def t_update_exhausts_and_raises(d):
    p = write(os.path.join(d, "f.md"), "base\n")

    def always_loses(current):
        write(p, current + "someone else\n")   # a writer lands on every attempt
        return current + "mine\n"

    try:
        cw.cas_update(p, always_loses, max_retries=3)
    except cw.CASRetriesExhausted as exc:
        assert "3 attempt" in str(exc), "error should name the attempt count: %s" % exc
        assert "mine" not in read(p), "must not have forced the write"
        return
    raise AssertionError("expected CASRetriesExhausted")


def t_update_missing_file_raises(d):
    try:
        cw.cas_update(os.path.join(d, "nope.md"), lambda t: t)
    except FileNotFoundError:
        return
    raise AssertionError("cas_update on a missing file should raise FileNotFoundError")


# --- permissions (the defect found while building this) --------------------

def t_mode_preserved(d):
    """The original version of this code let os.replace carry the temp file's
    0600 onto the target, silently locking other readers out of a shared file."""
    p = write(os.path.join(d, "f.md"), "one\n", mode=0o644)
    cw.cas_update(p, lambda t: t + "two\n")
    got = stat.S_IMODE(os.stat(p).st_mode)
    assert got == 0o644, "mode changed to %s" % oct(got)


def t_new_file_keeps_tight_default(d):
    p = os.path.join(d, "new.md")
    cw.cas_write(p, "fresh\n", None)
    got = stat.S_IMODE(os.stat(p).st_mode)
    assert got == 0o600, "a newly created file should stay 0600, got %s" % oct(got)


# --- temp file discipline --------------------------------------------------

def t_no_temp_left_on_success(d):
    p = write(os.path.join(d, "f.md"), "one\n")
    cw.cas_update(p, lambda t: t + "two\n")
    assert temps_in(d) == [], "temp files left behind: %s" % temps_in(d)


def t_no_temp_left_on_write_failure(d):
    """A failed write must clean up after itself and leave the target alone."""
    p = write(os.path.join(d, "f.md"), "original\n")
    try:
        cw.cas_write(p, "café\n", cw.file_sha(p), encoding="ascii")
    except UnicodeEncodeError:
        pass
    except Exception as exc:
        raise AssertionError("unexpected error type: %r" % exc)
    else:
        raise AssertionError("expected the encode to fail")
    assert read(p) == "original\n", "target was modified by a failed write"
    assert temps_in(d) == [], "temp files left behind: %s" % temps_in(d)


def t_lock_file_created_and_left(d):
    """The sidecar lock is left in place on purpose. Pinned as a test so the
    extra file on disk is documented behavior rather than a surprise."""
    p = write(os.path.join(d, "f.md"), "one\n")
    cw.cas_update(p, lambda t: t + "two\n")
    assert os.path.exists(cw.lock_path_for(p)), "sidecar lock file should exist"


def t_lock_is_on_sidecar_not_target(d):
    """A lock held on the target would stop excluding anyone after the first
    write, because the replace swaps the inode out from under it."""
    p = write(os.path.join(d, "f.md"), "one\n")
    before = os.stat(cw.lock_path_for(p)) if os.path.exists(cw.lock_path_for(p)) else None
    cw.cas_update(p, lambda t: t + "two\n")
    cw.cas_update(p, lambda t: t + "three\n")
    after = os.stat(cw.lock_path_for(p))
    assert before is None or before.st_ino == after.st_ino, "lock file inode must be stable"
    assert os.stat(p).st_ino != after.st_ino, "the lock must not be the target itself"


def t_temp_created_beside_target(d):
    """os.replace is only atomic within one filesystem, so the temp file has to
    be in the target's own directory rather than the system temp dir."""
    seen = {}
    real = cw.tempfile.mkstemp

    def spy(*args, **kwargs):
        seen["dir"] = kwargs.get("dir")
        return real(*args, **kwargs)

    cw.tempfile.mkstemp = spy
    try:
        p = write(os.path.join(d, "f.md"), "one\n")
        cw.cas_update(p, lambda t: t + "two\n")
    finally:
        cw.tempfile.mkstemp = real
    assert seen.get("dir") == os.path.dirname(os.path.abspath(p)), \
        "temp file was created in %r, not beside the target" % seen.get("dir")


# --- the thing it is actually for -----------------------------------------

def t_concurrent_appends_all_survive(d):
    """Six threads appending at once. Every line must be present at the end.
    Without compare-and-swap this test loses lines."""
    p = write(os.path.join(d, "shared.md"), "")
    threads, errors = [], []
    per_thread = 5

    def worker(tid):
        try:
            for i in range(per_thread):
                cw.cas_update(p, lambda t, tid=tid, i=i: t + "t%d-%d\n" % (tid, i),
                              max_retries=200)
        except Exception as exc:
            errors.append(exc)

    for tid in range(6):
        threads.append(threading.Thread(target=worker, args=(tid,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, "worker errors: %r" % errors[:3]
    lines = [ln for ln in read(p).splitlines() if ln]
    assert len(lines) == 6 * per_thread, \
        "expected %d lines, found %d (lost updates)" % (6 * per_thread, len(lines))
    assert len(set(lines)) == len(lines), "duplicate lines: a retry double-applied"


CHECKS = [
    ("file_sha returns None for a missing file", t_file_sha_missing),
    ("read_with_sha raises on a missing file", t_read_with_sha_missing),
    ("cas_write writes when the hash matches", t_write_happy),
    ("cas_write conflict leaves the file untouched", t_write_conflict_changes_nothing),
    ("cas_write creates a file when told to expect none", t_write_creates_when_absent),
    ("cas_write refuses to create over an existing file", t_write_refuses_create_over_existing),
    ("cas_update applies the transform", t_update_happy),
    ("cas_update re-runs the transform on fresh content", t_update_retries_against_fresh_content),
    ("cas_update fails loudly instead of forcing the write", t_update_exhausts_and_raises),
    ("cas_update raises on a missing file", t_update_missing_file_raises),
    ("an existing file keeps its permission mode", t_mode_preserved),
    ("a new file keeps the tight default mode", t_new_file_keeps_tight_default),
    ("no temp files left behind on success", t_no_temp_left_on_success),
    ("no temp files left behind on a failed write", t_no_temp_left_on_write_failure),
    ("a sidecar lock file is created and left in place", t_lock_file_created_and_left),
    ("the lock is on the sidecar, not the target", t_lock_is_on_sidecar_not_target),
    ("the temp file is created beside the target", t_temp_created_beside_target),
    ("six concurrent writers lose nothing", t_concurrent_appends_all_survive),
]


def main():
    print("cas-write: %d offline checks" % len(CHECKS))
    for name, fn in CHECKS:
        check(name, fn)
    print("")
    print("%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
