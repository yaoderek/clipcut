"""Batch-extract features for all downloaded MicroLens videos -> data/microlens/features.jsonl (resumable)."""
import json, sys, os, multiprocessing as mp
from pathlib import Path
D = Path(__file__).resolve().parent.parent / "data" / "microlens"
OUT = D / "features.jsonl"

def work(p):
    os.environ["OMP_NUM_THREADS"] = "1"
    from clipcut.features import extract
    try: r = extract(p); r["item"] = int(Path(p).stem); return r
    except Exception as e: return {"item": int(Path(p).stem), "error": str(e)[:200]}

if __name__ == "__main__":
    sys.path.insert(0, str(D.parent.parent))
    done = {json.loads(l)["item"] for l in open(OUT)} if OUT.exists() else set()
    todo = [str(p) for p in sorted((D / "videos").glob("*.mp4")) if int(p.stem) not in done and p.stat().st_size > 200000]
    print("todo", len(todo), flush=True)
    with mp.get_context("spawn").Pool(int(sys.argv[1]) if len(sys.argv) > 1 else 6) as pool, open(OUT, "a") as f:
        for i, r in enumerate(pool.imap_unordered(work, todo)):
            f.write(json.dumps(r) + "\n"); f.flush()
            if i % 50 == 0: print(i, flush=True)
