# Security policy: cas-write

## Posture

cas-write is provided as-is, with no warranty (see LICENSE). It is a correctness
tool for concurrent writers, not a security boundary. Use it as one control among
several, never as a sole guarantee.

The honest ceiling: it reduces, and does not eliminate, the chance that one
writer's changes are silently overwritten by another. Writers that go through the
module are serialized by an advisory lock. Writers that do not go through it are
detected by a content hash most of the time, and can still win the narrow race
between the hash check and the rename. If every writer of a file must be
excluded, you need a mechanism the operating system enforces on all of them, not
an advisory lock the cooperating half agrees to take.

Two properties worth knowing before you rely on this:

The advisory lock is `fcntl.flock`. It is honored between processes that take it,
and ignored by everything else, including any process that simply opens the file
and writes.

A transform passed to `cas_update` runs more than once when there is contention.
If it is not safe to re-run, the result is wrong with no error raised. That is a
correctness risk the module cannot detect for you.

## Validation status

The offline suite in `tests/test_cas_write.py` has been run: 18 checks, all
passing, on Python 3 under macOS. It covers every guard in the module plus the two
defects found while writing it (permission mode carried from the temp file, and
lost updates through the check-then-rename window).

The concurrency check was also run with the lock deliberately disabled, to confirm
it fails without it. It does, keeping 8 of 30 writes.

There is no fuzzing corpus and no adversarial corpus, because the threat model is
concurrent cooperating processes rather than a hostile writer. A hostile local
process can defeat this trivially by not taking the lock, and that is a
limitation of the design, not a bug to fix.

## Reporting a problem

Report privately through this repository's Security tab, using GitHub's
"Report a vulnerability" flow, or by opening a minimal issue that describes the
impact without a working exploit. Please give a reasonable window for a fix
before publishing details.

For a correctness bug that is not security sensitive, a normal issue with a
reproduction is the fastest path.
