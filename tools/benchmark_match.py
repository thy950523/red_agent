"""Benchmark a running local server; includes HTTP loopback upload time."""

import argparse
import json
from pathlib import Path
from statistics import median
from time import perf_counter
from urllib.request import Request, urlopen

import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/match")
    parser.add_argument("--photo", type=Path, default=Path("tests/fixtures/astronaut-face.jpg"))
    parser.add_argument("--runs", type=int, default=50)
    args = parser.parse_args()
    boundary = "fish-benchmark-boundary"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"portrait.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode() + args.photo.read_bytes() + f"\r\n--{boundary}--\r\n".encode())
    durations = []
    stages = {name: [] for name in ("decode", "face", "features", "score", "total")}
    for index in range(args.runs + 1):
        request = Request(args.url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        started = perf_counter()
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
            timing = response.headers.get("Server-Timing", "")
        elapsed = (perf_counter() - started) * 1000
        if result["match"]["id"] not in {f"{i:02d}" for i in range(2, 42)}:
            raise RuntimeError("invalid match response")
        if index:
            durations.append(elapsed)
            for item in timing.split(", "):
                name, duration = item.split(";dur=")
                stages[name].append(float(duration))
    print(f"loopback_end_to_end n={len(durations)} p50={median(durations):.1f}ms p95={np.percentile(durations,95):.1f}ms")
    for name, samples in stages.items():
        print(f"server_{name} n={len(samples)} p50={median(samples):.1f}ms p95={np.percentile(samples,95):.1f}ms")


if __name__ == "__main__":
    main()
