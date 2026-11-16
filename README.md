# cas-write

[![tests](https://github.com/justin-rhee/cas-write/actions/workflows/test.yml/badge.svg)](https://github.com/justin-rhee/cas-write/actions/workflows/test.yml) [![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![python 3](https://img.shields.io/badge/python-3-blue.svg) ![zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)

Lets several processes update the same file without one quietly erasing another's changes.

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

Two agent sessions and a scheduled job all appended to one index file. Each read it, built its update and saved, so whichever landed last erased the rest. Nothing errored, and a whole session's entries vanished one night.

## Use it if

- more than one process writes the same file, and one of them is an agent or a scheduled job
- losing an entry is worse than failing loudly
- you want the failure to be an exception rather than a gap you find later

## Install

Copy `src/cas_write.py` next to your code. It's about 220 lines of standard-library Python, and a third of that is the docstring on where it stops working. Check it with `python3 tests/test_cas_write.py`, which ends `18 passed, 0 failed`.

## How it works

1. hash the file
2. build your new content from what you read
3. swap it in only if the hash still matches, under an exclusive lock so the compare and the swap are one step
4. on a mismatch, re-read and run your function again against what's there now, so write it to be safe to run twice

## Why not just a lock?

A lock file works until a writer shows up that doesn't know about it. My writers were written months apart, one in another language, one an agent using its own file tool. A lock protects the writers that cooperate and is invisible to the rest. The hash catches everyone, because a writer that's never heard of this module still changes the file. The lock is here anyway as a second layer: without it, the concurrency test lost 24 of 30 appends.

## What it won't do

- fully protect writers that don't go through it, since they skip the lock and can still win the narrow race between the check and the rename
- save you from a transform that's unsafe to re-run, which gives a wrong answer on retry with no error, the easiest way to be fooled by this
- give you crash durability, because the directory entry isn't fsynced, so a power cut at the wrong moment can leave the old content
- run on Windows, since the lock uses `fcntl.flock`
- clean up the `<target>.lock` file beside yours, which stays on purpose because deleting it would race the next writer
- create a file from `cas_update`; use `cas_write` with `expected_sha=None` for that

## How I tested it

- 18 offline checks, each in its own temp folder: `python3 tests/test_cas_write.py`
- six threads making thirty appends: the first version kept six of them, and turning the lock off again makes that test fail
- writing a permission check caught a real bug, where the swap turned a 0644 file owner-only

<details>
<summary>More on testing</summary>

The original created its temp file with `mkstemp`, which is mode 0600, and the rename carried that onto the target, so a file other processes read quietly became readable only by its owner. The fix copies the target's mode onto the temp file before the swap. The concurrency gap was bigger than expected: between "the hash still matches" and "the rename landed" sits a write and an fsync, long enough that other writers usually land inside it. CI runs the suite on macOS and Ubuntu. Design decisions: [docs/ADR.md](docs/ADR.md).

</details>

## License

MIT. See [LICENSE](LICENSE). No warranty. Security notes and how to report a problem: [SECURITY.md](SECURITY.md).

<!-- related:start -->
## Related

- [take-once](https://github.com/justin-rhee/take-once): lets several coding agents share one task list without two of them grabbing the same task
- [never-worse-backup](https://github.com/justin-rhee/never-worse-backup): keeps an auto-backup from saving another session's half-finished work

Part of a small set of tools I pulled out of my own agent harness. I use them myself and read every issue. More on my [GitHub profile](https://github.com/justin-rhee).
<!-- related:end -->
