"""Run the command line with `python -m tbot`; launcher executables can be blocked on Windows."""

import sys

from tbot.cli import run

sys.exit(run())
