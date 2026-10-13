"""Entry point of the packaged command line, autoshelf-cli.exe (runs in a console)."""

import sys

from autoshelf.cli import main

if __name__ == "__main__":
    sys.exit(main())
