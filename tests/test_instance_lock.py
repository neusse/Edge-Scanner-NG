import os
import subprocess
import sys

import pytest

from scanner.instance_lock import ScannerInstanceLock


def test_sibling_rejected_and_clean_release(tmp_path):
    path = tmp_path / "scanner.lock"
    with ScannerInstanceLock(path, "live"):
        with pytest.raises(RuntimeError, match="sibling already running.*mode=live"):
            ScannerInstanceLock(path, "replay").acquire()
    with ScannerInstanceLock(path, "replay"):
        pass


def test_crashed_owner_does_not_leave_stale_lock(tmp_path):
    path = tmp_path / "scanner.lock"
    child = subprocess.run(
        [sys.executable, "-c",
         "import os,sys; from pathlib import Path; from scanner.instance_lock import ScannerInstanceLock; "
         "lock=ScannerInstanceLock(Path(sys.argv[1]), 'live'); lock.acquire(); os._exit(0)",
         str(path)], check=True,
    )
    assert child.returncode == 0
    with ScannerInstanceLock(path, "replay"):
        pass
