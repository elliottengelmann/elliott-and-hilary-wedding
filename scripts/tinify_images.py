#!/usr/bin/env python3
"""Compress all images under images/ in place via the TinyPNG API.

Walks images/ (top level) and images/guests/, sends each .jpg/.png to
https://api.tinify.com/shrink, and overwrites the original with the
compressed version. Uses a small JSON cache (keyed by sha256) so reruns
skip files that have already been compressed.

Run:
    TINIFY_KEY=... python3 scripts/tinify_images.py
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent.parent
IMG_ROOT = ROOT / "images"
CACHE_PATH = ROOT / ".tinify-cache.json"
API_URL = "https://api.tinify.com/shrink"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_session(key: str) -> requests.Session:
    s = requests.Session()
    auth = base64.b64encode(f"api:{key}".encode()).decode()
    s.headers["Authorization"] = f"Basic {auth}"
    retry = Retry(
        total=6,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def download_compressed(session: requests.Session, url: str) -> bytes:
    last_err: Exception | None = None
    for attempt in range(5):
        try:
            r = session.get(url, timeout=(30, 180), stream=True)
            r.raise_for_status()
            chunks: list[bytes] = []
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    chunks.append(chunk)
            return b"".join(chunks)
        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.ReadTimeout,
        ) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"download failed after retries: {last_err}")


def tinify_one(session: requests.Session, path: Path) -> dict:
    raw = path.read_bytes()
    orig_size = len(raw)
    last_err: Exception | None = None
    location: str | None = None
    for attempt in range(4):
        try:
            r = session.post(API_URL, data=raw, timeout=(30, 180))
            if r.status_code not in (200, 201):
                raise RuntimeError(
                    f"shrink {r.status_code} {r.text[:200]!r}"
                )
            location = r.headers.get("Location")
            if not location:
                raise RuntimeError(f"no Location header: {dict(r.headers)}")
            break
        except (requests.exceptions.RequestException, RuntimeError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    if not location:
        raise RuntimeError(f"shrink failed for {path}: {last_err}")
    compressed = download_compressed(session, location)
    new_size = len(compressed)
    if new_size >= orig_size:
        return {
            "skipped": True,
            "reason": "no-savings",
            "orig": orig_size,
            "new": new_size,
        }
    path.write_bytes(compressed)
    return {
        "skipped": False,
        "orig": orig_size,
        "new": new_size,
        "saved_pct": round(100 * (orig_size - new_size) / orig_size, 1),
    }


def collect_images() -> list[Path]:
    exts = {".jpg", ".jpeg", ".png"}
    paths = [
        p for p in sorted(IMG_ROOT.rglob("*"))
        if p.is_file() and p.suffix.lower() in exts
    ]
    return paths


def load_cache() -> dict:
    if CACHE_PATH.exists():
        return json.loads(CACHE_PATH.read_text())
    return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)
    key = os.environ.get("TINIFY_KEY")
    if not key:
        print("TINIFY_KEY env var required", file=sys.stderr)
        return 2
    paths = collect_images()
    cache = load_cache()
    pending: list[tuple[Path, str]] = []
    for p in paths:
        digest = sha256_of(p)
        rel = str(p.relative_to(ROOT))
        entry = cache.get(rel)
        if entry and entry.get("sha256_after") == digest:
            continue
        pending.append((p, digest))
    print(f"{len(paths)} total, {len(pending)} pending")

    session = make_session(key)
    total_orig = 0
    total_new = 0
    errors: list[str] = []

    def work(item: tuple[Path, str]) -> tuple[Path, str, dict | Exception]:
        p, before_digest = item
        try:
            res = tinify_one(session, p)
            return p, before_digest, res
        except Exception as e:  # noqa: BLE001
            return p, before_digest, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(work, item): item for item in pending}
        for i, fut in enumerate(
            concurrent.futures.as_completed(futures, timeout=None), start=1
        ):
            try:
                p, before_digest, res = fut.result(timeout=120)
            except concurrent.futures.TimeoutError:
                item = futures[fut]
                p = item[0]
                before_digest = item[1]
                res = RuntimeError("timed out after 120s")
            rel = str(p.relative_to(ROOT))
            if isinstance(res, Exception):
                errors.append(f"{rel}: {res}")
                print(f"[{i}/{len(pending)}] ERR  {rel}: {res}")
                continue
            after_digest = sha256_of(p)
            cache[rel] = {
                "sha256_before": before_digest,
                "sha256_after": after_digest,
                "orig_bytes": res["orig"],
                "new_bytes": res["new"],
            }
            total_orig += res["orig"]
            total_new += res["new"]
            if res.get("skipped"):
                print(
                    f"[{i}/{len(pending)}] keep {rel} (no savings, "
                    f"{res['orig']} -> {res['new']})"
                )
            else:
                print(
                    f"[{i}/{len(pending)}] ok   {rel} "
                    f"{res['orig']:>9} -> {res['new']:>9} "
                    f"({res['saved_pct']}% saved)"
                )
            if i % 10 == 0:
                save_cache(cache)

    save_cache(cache)
    if total_orig:
        pct = round(100 * (total_orig - total_new) / total_orig, 1)
        print(
            f"\nTotal: {total_orig:,} -> {total_new:,} bytes "
            f"({pct}% saved over {len(pending) - len(errors)} files)"
        )
    if errors:
        print(f"\n{len(errors)} errors:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
