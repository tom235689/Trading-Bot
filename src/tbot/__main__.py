"""Run the command line with `python -m tbot`; launcher executables can be blocked on Windows."""

import sys

from tbot.cli import main

sys.exit(main())
