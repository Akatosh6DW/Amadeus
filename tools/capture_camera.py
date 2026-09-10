#!/usr/bin/env python3
"""Capture one camera frame for the local AMADEUS vision tool."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np


def open_camera(index: int) -> tuple[cv2.VideoCapture, str]:
    backends = [
        (cv2.CAP_DSHOW, "directshow"),
        (cv2.CAP_MSMF, "media-foundation"),
        (cv2.CAP_ANY, "auto"),
    ]
    for backend, name in backends:
        capture = cv2.VideoCapture(index, backend)
        if capture.isOpened():
            return capture, name
        capture.release()
    raise RuntimeError(f"camera {index} is unavailable")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--warmup-frames", type=int, default=12)
    args = parser.parse_args()

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    capture, backend = open_camera(max(0, args.index))
    capture.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    frame = None
    try:
        for _index in range(max(3, args.warmup_frames)):
            ok, candidate = capture.read()
            if ok and candidate is not None and candidate.size:
                frame = candidate
            time.sleep(0.025)
    finally:
        capture.release()

    if frame is None:
        raise RuntimeError(f"camera {args.index} returned no frame")
    mean = np.mean(frame, axis=(0, 1))
    stddev = np.std(frame, axis=(0, 1))
    if float(np.max(stddev)) < 0.8 and float(np.max(mean)) < 3.0:
        raise RuntimeError(f"camera {args.index} returned an all-black frame")
    if not cv2.imwrite(str(output), frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
        raise RuntimeError(f"failed to save camera frame to {output}")
    print(json.dumps({
        "ok": True,
        "path": str(output),
        "cameraIndex": args.index,
        "backend": backend,
        "width": int(frame.shape[1]),
        "height": int(frame.shape[0]),
        "mean": [round(float(value), 3) for value in mean],
        "stddev": [round(float(value), 3) for value in stddev],
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
        }, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
