# cas-write

Compare-and-swap writes for a file that more than one process updates, so a
second writer cannot silently erase the first.

## Why I built it

Two processes that read the same file, each build new content from what they
read, and each save will both succeed, and whichever finishes last is the only
one whose changes survive. Mine did this to a shared index file that two agent
sessions and a scheduled job all appended to. A whole session's entries were
gone. Nothing errored, nothing logged, and I only found out days later when I
went looking for something I was sure I had written.

## How it works

It is about 220 lines of Python, standard library only, and roughly a third of
that is the docstring explaining where it stops working.

The mechanism is small. Hash the file, compute your new content from what you
read, then swap the new content in only if the hash still matches, and if it
does not, re-read and re-run against whatever is there now. The compare and the
swap happen under an exclusive lock, so they are one step rather than two with a
gap in between.

```
$ python3 tests/test_cas_write.py
cas-write: 18 offline checks
  ok    file_sha returns None for a missing file
  ok    read_with_sha raises on a missing file
  ok    cas_write writes when the hash matches
  ok    cas_write conflict leaves the file untouched
  ok    cas_write creates a file when told to expect none
  ok    cas_write refuses to create over an existing file
  ok    cas_update applies the transform
  ok    cas_update re-runs the transform on fresh content
  ok    cas_update fails loudly instead of forcing the write
  ok    cas_update raises on a missing file
  ok    an existing file keeps its permission mode
  ok    a new file keeps the tight default mode
  ok    no temp files left behind on success
  ok    no temp files left behind on a failed write
  ok    a sidecar lock file is created and left in place
  ok    the lock is on the sidecar, not the target
  ok    the temp file is created beside the target
  ok    six concurrent writers lose nothing

18 passed, 0 failed
```

## Install and use

Copy `src/cas_write.py` next to your code. There is nothing to install.

```python
from cas_write import cas_update, cas_write, file_sha, CASConflict

# read, transform, swap, retrying against fresh content on a conflict
text, attempts = cas_update("notes.md", lambda current: current + "one more line\n")

# or, if you already built the content and want to abort on conflict
sha = file_sha("notes.md")
new = build_it()
try:
    cas_write("notes.md", new, sha)
except CASConflict:
    print("someone else wrote first, nothing was changed")
```

The function you pass to `cas_update` gets re-run on each retry, against the
content that is on disk at that moment. Write it so running it twice is fine.

## Use it if

- more than one process appends to or rewrites the same file
- one of those writers is an agent, or a scheduled job, or both
- the file matters enough that losing an entry is worse than failing loudly
- you want the failure to be an exception rather than a gap you find later

## Why not just a lock

A lock file is the reflex fix, and it works right up until a writer shows up that
does not know about it. That was the whole problem on my index file. Those writers
were written months apart, one of them in another language, and one of them was an
agent using its own file-write tool, which knows nothing about my conventions. A
lock protects the writers that cooperate and is invisible to the ones that do not,
and the ones that do not were exactly the ones I could not go fix.

Compare-and-swap asks a different question. Rather than asking writers to announce
themselves, it checks whether the file still looks like the file you read. A writer
that has never heard of this module still changes the hash, so the next writer
through notices and retries against the new content.

There is a lock in here anyway, and it is the second layer, not the mechanism. It
is held across the compare and the replace so those two steps are one, because
without it the concurrency test lost twenty-four of thirty appends. The lock
serializes the writers that take it, the hash catches everyone else, and the hash
is the part that survives contact with code you do not control.

## What it won't do

It only fully protects writers that go through it. An editor, an agent's own
file-write tool, or a script in another language will not take the lock. Those
get caught by the hash check most of the time, and can still win the narrow race
between the check and the rename.

It will not save you from a transform that is unsafe to re-run. If your function
appends a line the first time and something different the second time, a retry
produces a wrong answer with no error at all. That is the easiest way to be
fooled by this, and the reason the docstring says so twice.

It is not crash durability. The rename is atomic, but the directory entry is not
fsynced, so a machine that loses power at the wrong moment can still leave you
with the old content.

It is POSIX only. The lock uses `fcntl.flock`, so the module does not import on
Windows.

It leaves a `<target>.lock` file next to your file, on purpose. Deleting it would
race with the next writer.

`cas_update` will not create a file. It raises `FileNotFoundError`. Use
`cas_write` with `expected_sha=None` when you mean to create one.

## How I tested it

18 offline checks, in `tests/test_cas_write.py`. No network, no credentials, and
each check runs in its own throwaway temp directory. Run it with
`python3 tests/test_cas_write.py`.

Two of those checks exist because writing them broke the code.

The permission check came first. The original version created its temp file with
`mkstemp`, which is mode 0600, and the rename carried that onto the target. A
0644 file that other processes read quietly became readable only by me. The fix
copies the target's mode onto the temp file before the swap.

The concurrency check is the one that matters. Six threads, thirty appends, and
the first run kept six of them. The hash check was real but the window it left
open was not small: between "the hash still matches" and "the rename landed"
sits a write and an fsync, which is long enough that other writers usually land
inside it. Adding the lock around the compare and the swap fixed it. I then
disabled the lock again and re-ran, to be sure the check actually fails without
it, and it does.

## License

MIT, see [LICENSE](LICENSE). No warranty. Security posture and how to report a
problem: [SECURITY.md](SECURITY.md).

Design decisions and what changed while building it: [docs/ADR.md](docs/ADR.md).

One of a set of small tools I've pulled out of a bigger system I run, where
agents write the code and plain scripts decide when it's actually done. They all
share one rule: the machine suggests, a person decides, and nothing quietly goes
wrong behind your back. More of them on my
[GitHub profile](https://github.com/justin-rhee).
