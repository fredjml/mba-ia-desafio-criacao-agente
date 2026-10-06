import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

py = sys.argv[1]
script = sys.argv[2]
out_path = sys.argv[3]
zone = timezone(timedelta(hours=-3))

def stamp() -> str:
    return datetime.now(zone).strftime("%Y-%m-%dT%H:%M:%S%z")

with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
    handle.write(f"=== MATRIX cheap=C1-C5 N=300; expensive=C1/C2/C3/C5 N=60 C4 N=40; race_pause_seconds=0 ===\n")
    handle.write(f"=== SESSION_START {stamp()} ===\n")
    handle.flush()
    codes = []
    for round_id in (1, 2, 3):
        handle.write(f"=== RODADA {round_id} START {stamp()} ===\n")
        handle.flush()
        started = time.perf_counter()
        completed = subprocess.run(
            [py, "-u", script, "--rodada", str(round_id)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        elapsed = time.perf_counter() - started
        codes.append(completed.returncode)
        handle.write(
            f"=== RODADA {round_id} EXIT {completed.returncode} elapsed_seconds={elapsed:.3f} {stamp()} ===\n"
        )
        handle.flush()
    handle.write(f"=== SESSION_END {stamp()} exit_codes={codes} ===\n")
    handle.flush()
sys.exit(0 if codes == [0, 0, 0] else 1)
