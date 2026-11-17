"""
Convert an existing run into the Automation Master Controller format (no browser needed).

    python tools/export_master.py runs/<run_folder>/testcase.json
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from tcgen.master_export import export_from_json  # noqa: E402

if len(sys.argv) < 2:
    sys.exit(__doc__)
src = Path(sys.argv[1])
out, warns = export_from_json(src, config.TEST_DATA_FILE, src.parent / "Automation_Master_Controller.xlsx", config)
print("Written:", out)
for w in warns:
    print("  !", w)