"""Open the G2 Bridge window. This is also the packaged app's entry point."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goggles_bridge.gui import main

if __name__ == '__main__':
    main()
