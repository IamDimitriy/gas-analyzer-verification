import csv
import json
import math
from pathlib import Path
from raschet import cmh

base = Path(__file__).resolve().parent / "dannye"
with (base / "straty-eris-414.csv").open(encoding="utf-8-sig") as f:
    rows = [{k: int(v) if k.endswith(("_total", "_bad")) else v for k,v in row.items()} for row in csv.DictReader(f)]
expected = json.loads((base / "svodka-eris-414.json").read_text(encoding="utf-8"))
for year in sorted({r["year"] for r in rows}) + ["combined"]:
    group = rows if year == "combined" else [r for r in rows if r["year"] == year]
    target = expected["combined"] if year == "combined" else expected["years"][year]
    for key, value in cmh(group).items():
        assert math.isclose(value, target[key], rel_tol=1e-9), (year, key)
print("OK:", len(rows), "groups")
