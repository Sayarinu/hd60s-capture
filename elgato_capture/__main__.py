import os
import sys

from .app import main

code = main(sys.argv)
sys.stdout.flush()
sys.stderr.flush()
# A wedged capture card can leave a driver call blocked in a worker thread;
# exit hard so the process never lingers after the window closes.
os._exit(code)
