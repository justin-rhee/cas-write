# Architecture Decision Records (ADRs)

Why this is shaped the way it is, including the two decisions that only exist
because the tests disagreed with the code.

## Compare-and-swap rather than a lock file people have to remember

The failure that started this was a lost update on a shared index file. Two agent
sessions and a scheduled job all appended to it. Every write succeeded, the last
one won, and one session's entries were simply absent. There was no error to
notice, no log line, and no way to tell after the fact which writer had lost.

The reflex fix is a lock file that every writer acquires first. I did not want
that as the primary mechanism, for a reason that matters more with agents in the
mix than without them: a lock only works if every writer knows to take it. The
writers here were heterogeneous, some of them written months apart, and at least
one of them was an agent that would happily write the file through a tool that
knows nothing about my conventions. A lock protects the writers who cooperate and
is invisible to the ones who do not, and the ones who do not are exactly the ones
I could not fix.

Compare-and-swap inverts that. Instead of asking writers to announce themselves,
it checks whether the file still looks like the file you read. A writer who never
heard of this module still changes the hash, so the next writer notices and
retries against the new content. That property survives contact with code you do
not control, which is the whole point.

## The transform is a function, not precomputed content

The obvious API is "give me the new content and the hash you expect". That is
`cas_write`, and it is here, but it is not the headline entry point.

The problem with precomputed content is that on a conflict there is nothing
sensible to do with it. Your content was derived from a version of the file that
no longer exists, so writing it anyway is the lost update you were trying to
prevent, and discarding it means your caller has to loop, re-read, and rebuild.
Every caller would write that loop, and some of them would write it wrong.

So `cas_update` takes the derivation itself, and re-runs it against the fresh
content on every retry. It moves one requirement onto the caller, which is that
the function has to be safe to run more than once, and that requirement is now
the sharpest way to misuse this tool. A transform that is not idempotent produces
a wrong result silently, which is why it is stated in the module docstring, in
the README, and in the exception message you get when retries run out.

## The temp file lives beside the target

`os.replace` is atomic only within a single filesystem. A temp file in the system
temp directory can easily be on a different volume, at which point the rename
degrades into a copy that can be interrupted half-written, which is the exact
failure the atomic rename was chosen to avoid. Creating the temp file in the
target's own directory keeps the rename inside one filesystem.

That is a small decision with an ugly consequence worth naming: this module
writes a temp file into your directory, so a directory watcher will see
`.cas-*.tmp` files appear and vanish. A test pins the behavior so it cannot drift
by accident.

## The lock exists because the test said the hash was not enough

This is the decision I did not plan to make.

The design was pure optimistic concurrency, and the module docstring originally
claimed the remaining race was theoretical, a sub-millisecond window between the
final hash check and the rename. Then I wrote the test that exercises the actual
promise: six threads, five appends each, assert all thirty lines survive. The
first run kept six.

The window was never sub-millisecond. Between the check and the rename sits a
temp file write and an `fsync`, and fsync is slow enough that under real
contention another writer usually lands inside it. The hash check was correct and
almost never got a chance to matter.

The fix is an exclusive `fcntl.flock` held across the compare and the replace, so
those two steps are one critical section. The lock is on a sidecar `.lock` file
rather than on the target, because every successful write replaces the target's
inode, and a lock held on the old inode excludes nobody once that happens. Both
of those properties have their own test.

What I want to be clear about is which claim changed. The tool is not
lock-and-nothing-else now: cooperating writers are serialized by the lock, and
non-cooperating writers are still caught by the hash, which is the same
defense-in-depth the first ADR argues for. What changed is that I stopped
describing a real, frequently-hit race as theoretical. I had written the honest
sounding caveat before I had measured anything, and the caveat was wrong in the
direction that flattered the design.

After the fix I disabled the lock again and re-ran the suite, because a test that
passes with the protection removed never tested the protection. It fails without
the lock, keeping eight of thirty.

## It preserves the file's permission mode

Also found by a test, and smaller. `tempfile.mkstemp` creates files as 0600, and
`os.replace` carries the temp file's mode onto the target. So the first update of
a 0644 file that other processes read silently made it readable only by its
owner, and the thing that broke would not be this module, it would be some other
process getting a permission error days later on a file nobody had touched.

The module now copies the target's mode onto the temp file before the swap. A
file being created for the first time keeps the tight 0600 default, since there
is no existing mode to preserve and the safer default is the right one to leave
in place.

## It fails loudly instead of forcing the write

After `max_retries` losing attempts, `cas_update` raises. It does not fall back
to writing anyway.

This is the one place where the tool is deliberately less convenient than it
could be. An exception in a scheduled job means somebody has to look at it. But
the failure mode this exists to prevent is precisely a write that succeeded when
it should not have, so a fallback that forces the write would reintroduce the
original bug under a different name, and it would do it in the rare, hard to
reproduce case where the evidence is thinnest.

## What is deliberately not here

No backoff between retries. Under the contention this is built for, retries are
cheap and the next read is the only thing needed. Adding jitter and sleeps would
be more code serving a workload I have not measured.

No directory fsync, so this is not crash durability. The rename is atomic with
respect to other readers, which is what protects concurrent writers, but a power
loss at the wrong moment can still leave the old content. Saying so in the README
is the honest version of a fix I have not written.

No Windows support. The lock is `fcntl.flock`, which does not exist there, and I
have no way to test a Windows equivalent properly. An untested branch for a
platform I do not run would be a worse answer than a stated limit.
