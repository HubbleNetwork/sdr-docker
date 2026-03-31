import subprocess
import sys
import time

_EXIT_CODE_CONNECTION_LOST = 3
_RESTART_DELAY_S = 3

if __name__ == "__main__":
    while True:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import multiprocessing; multiprocessing.set_start_method('fork', force=True); "
             "from stream_web.app import main; main()"],
        )
        if proc.returncode == _EXIT_CODE_CONNECTION_LOST:
            print(f"\n[supervisor] Connection lost (exit code {proc.returncode}). "
                  f"Restarting in {_RESTART_DELAY_S}s...\n", flush=True)
            time.sleep(_RESTART_DELAY_S)
            continue
        break
