import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "live"), os.path.join(ROOT, "research"), ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

ROOT_DIR = ROOT
