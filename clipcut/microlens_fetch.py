"""Fetch the first N MB of individual MicroLens-100k videos straight out of the 477 GB split zip via HTTP Range
(reading the central directory locally), inflating the deflate stream on the fly. Prefix-decodable if moov is at front."""
import json, struct, zlib, subprocess, sys, os
from pathlib import Path
B = "https://recsys.westlake.edu.cn/MicroLens-100k-Dataset/MicroLens-100k_videos"
PART = 2147483648
IDX = Path(__file__).resolve().parent.parent / "data" / "microlens" / "zip_index.json"

def part_url(disk): return f"{B}.z{disk+1:02d}" if disk < 222 else f"{B}.zip"

def rng(disk, a, b):
    """bytes [a,b) of logical disk, spilling into next disk if needed"""
    out = b""
    while a < b:
        end = min(b, PART)
        r = subprocess.run(["curl", "-s", "-f", "-m", "120", "-r", f"{a}-{end-1}", part_url(disk)], capture_output=True)
        if r.returncode: raise RuntimeError(f"curl {r.returncode}")
        out += r.stdout; b -= end; a = 0; disk += 1; b = max(b, 0)
        if end == b: break
    return out

def fetch(e, out, max_mb=6):
    hdr = rng(e["disk"], e["off"], e["off"] + 30 + 512)
    nl, el = struct.unpack("<HH", hdr[26:30]); start = e["off"] + 30 + nl + el
    disk = e["disk"]
    if start >= PART: disk += 1; start -= PART
    want = min(e["csz"], int(max_mb * 1e6))
    end = start + want
    if end <= PART: comp = rng(disk, start, end)
    else: comp = rng(disk, start, PART) + rng(disk + 1, 0, end - PART)
    data = zlib.decompressobj(-15).decompress(comp)
    Path(out).write_bytes(data)
    return len(data), want == e["csz"]

if __name__ == "__main__":
    ents = {Path(e["name"]).stem: e for e in json.load(open(IDX)) if e["csz"]}
    vid = sys.argv[1]; print(fetch(ents[vid], f"/tmp/{vid}.mp4"))
