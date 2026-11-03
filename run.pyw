"""Start the tray app without a console window (pythonw)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mom_app.app import main  # noqa: E402

if __name__ == "__main__":
    main()
