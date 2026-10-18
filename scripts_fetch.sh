cd /workspace/clipcut/data/microlens
cat sample_ids.txt | xargs -P 16 -I{} sh -c '[ -s videos/{}.mp4 ] || /workspace/clipcut/.venv/bin/python -c "
import json,sys; sys.path.insert(0,\"/workspace/clipcut\")
from clipcut.microlens_fetch import fetch, IDX
from pathlib import Path
e={Path(x[\"name\"]).stem:x for x in json.load(open(IDX)) if x[\"csz\"]}[\"{}\"]
fetch(e,\"videos/{}.mp4\",max_mb=5)" 2>>fetch_err.log'
