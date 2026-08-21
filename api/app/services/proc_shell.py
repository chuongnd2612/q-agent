"""One place that decides whether a subprocess needs `shell=True`.

`subprocess.run(["prog", "--flag"], shell=True)` behaves completely differently by
platform, and the POSIX behaviour is a silent trap:

* **Windows** — needed. Node tool "binaries" in `node_modules/.bin` are `.cmd`
  shims, and `CreateProcess` cannot execute those directly, so the shell has to
  resolve them.
* **POSIX** — actively harmful. Python passes only ``argv[0]`` to ``/bin/sh -c``;
  every remaining item becomes ``$0``, ``$1``… of that shell and **never reaches
  the program**. The command runs with *no arguments*.

That is not a hypothetical. Five call sites shipped `shell=True` with an argument
list, so in the Linux API container `playwright test --list` ran as bare
`playwright` (printing its usage banner and exiting 1) and `tsc --noEmit` ran as
bare `tsc` (dropping `--noEmit`, so it *emitted*). The banner's exit code looked
exactly like a real failure, so the project gate rejected every generated spec for
months while appearing to work — see #613.

The rule lives here, once, rather than as five copies of the same comment that can
drift apart — the lesson #557 paid for with a server/device divergence.

**Never pass a list with `shell=True` on POSIX.** Use::

    subprocess.run([bin, "test", "--list"], shell=NEEDS_SHELL, ...)
"""

from __future__ import annotations

import os

#: True only on Windows, where `.cmd` shims in `node_modules/.bin` need a shell to
#: resolve. False on POSIX, where `shell=True` would discard every argument.
NEEDS_SHELL = os.name == "nt"
