"""Execute original notebook cells inside the credential-free Cloud Run sandbox."""
import json
import os
import sys
import traceback
from pathlib import Path

path = Path(sys.argv[1]).resolve()
os.chdir(path.parent)
os.environ["MPLBACKEND"] = "Agg"
cells = [("".join(c.get("source", [])), i + 1) for i, c in enumerate(json.loads(path.read_text())["cells"]) if c.get("cell_type") == "code"]
namespace = {"__name__": "__main__", "__file__": str(path)}
completed = 0
for source, index in cells:
    print(f"Starting original notebook cell {index}.", flush=True)
    try:
        exec(compile(source, f"{path.name}:cell-{index}", "exec"), namespace)
    except BaseException:
        traceback.print_exc()
        print(f"Completed {completed} of {len(cells)} code cells.", flush=True)
        sys.exit(1)
    completed += 1
    print(f"Completed original notebook cell {index}.", flush=True)
print(f"Completed all {completed} original code cells.", flush=True)
