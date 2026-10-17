"""Download pinned native installers; installation also requires Authenticode checks.

Ported from petsc-native-windows config/native_windows/download_toolchain.py at
b110b88419df3f98805b66533dfeeb18f868a315 (SHA-256 c944026f...112c). Changes:
the pin file is native/toolchain-pins.json, and the default cache is
%LOCALAPPDATA%/msmpi-native-windows/toolchain-cache.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import urllib.request
import time


def verify(path, pin):
    if "bytes" in pin and path.stat().st_size != pin["bytes"]:
        raise ValueError(f"Unexpected installer size: {path}")
    sha256, sha384 = hashlib.sha256(), hashlib.sha384()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            sha256.update(chunk)
            sha384.update(chunk)
    if "sha384" in pin and sha384.hexdigest() != pin["sha384"]:
        raise ValueError(f"Installer SHA384 mismatch: {path}")
    if "sha256" in pin and sha256.hexdigest() != pin["sha256"]:
        raise ValueError(f"Installer SHA256 mismatch: {path}")
    return {"file": str(path), "bytes": path.stat().st_size, "sha256": sha256.hexdigest()}


def reuse_completed_ranges(cache, pin, chunk_bytes):
    """Split old complete ranges locally; only the final vendor hash is trusted."""
    legacy = cache / (pin['filename'] + '.parts')
    directory = cache / (pin['filename'] + f'.parts-{chunk_bytes}')
    directory.mkdir(exist_ok=True)
    if not legacy.exists():
        return
    for part in legacy.iterdir():
        match = re.fullmatch(r'(\d+)-(\d+)', part.name)
        if not match:
            continue
        first, last = map(int, match.groups())
        if first % chunk_bytes or last >= pin['bytes'] or part.stat().st_size != last - first + 1:
            continue
        with part.open('rb') as source:
            for offset in range(first, last + 1, chunk_bytes):
                end = min(offset + chunk_bytes - 1, last)
                target = directory / f'{offset}-{end}'
                if target.exists() and target.stat().st_size == end - offset + 1:
                    source.seek(end - offset + 1, 1)
                    continue
                temporary = target.with_suffix('.partial')
                temporary.write_bytes(source.read(end - offset + 1))
                temporary.replace(target)


def download_part(cache, name, pin, first, last, chunk_bytes):
    directory = cache / (pin["filename"] + f".parts-{chunk_bytes}")
    directory.mkdir(exist_ok=True)
    target = directory / f"{first}-{last}"
    if target.exists() and target.stat().st_size == last - first + 1:
        return target
    for attempt in range(3):
        try:
            request = urllib.request.Request(pin["url"], headers={"Range": f"bytes={first}-{last}"})
            with urllib.request.urlopen(request, timeout=60) as response:
                expected = f"bytes {first}-{last}/{pin['bytes']}"
                if response.status != 206 or response.headers.get("Content-Range") != expected:
                    raise ValueError(f"Server did not honor exact byte range: {name}")
                temporary = target.with_suffix(".partial")
                with temporary.open("wb") as stream:
                    while chunk := response.read(32768):
                        stream.write(chunk)
            if temporary.stat().st_size != last - first + 1:
                raise ValueError(f"Truncated byte range: {name}")
            temporary.replace(target)
            print(f"Downloaded {name} bytes {first}-{last}", flush=True)
            return target
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def download(cache, name, pin, refresh):
    target = cache / pin["filename"]
    if target.exists() and not refresh:
        try:
            verify(target, pin)
        except ValueError:
            refresh = True
    if refresh or not target.exists():
        temporary = target.with_suffix(target.suffix + ".partial")
        with urllib.request.urlopen(pin["url"], timeout=120) as response, temporary.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        verify(temporary, pin)
        temporary.replace(target)
    result = verify(target, pin)
    result.update(component=name, version=pin["version"], url=pin["url"])
    print(f"Verified download: {name} {pin['version']} ({result['bytes']} bytes)", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "msmpi-native-windows/toolchain-cache")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 33), default=16)
    parser.add_argument("--chunk-mib", type=int, choices=(1, 2, 4, 8), default=1)
    parser.add_argument("--component", action="append", help="Download only this manifest component (repeatable)")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    pins = json.loads((Path(__file__).resolve().parents[1] / "toolchain-pins.json").read_text())
    items = [(name, pin) for name, pin in pins.items() if isinstance(pin, dict) and 'url' in pin]
    if args.component:
        unknown = set(args.component) - {name for name, pin in items}
        if unknown:
            parser.error("Unknown components: " + ", ".join(sorted(unknown)))
        items = [(name, pin) for name, pin in items if name in args.component]
    # Ranges allow resumable transfers without trusting partial installers.
    # The complete vendor hash and Authenticode signature still gate installation.
    pending = []
    chunk_bytes = args.chunk_mib * 1024 * 1024
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for name, pin in items:
            if pin.get("bytes", 0) > 4 * 1024 * 1024 and (args.refresh or not (args.cache / pin["filename"]).exists()):
                futures = []
                reuse_completed_ranges(args.cache, pin, chunk_bytes)
                for first in range(0, pin["bytes"], chunk_bytes):
                    last = min(first + chunk_bytes - 1, pin["bytes"] - 1)
                    futures.append(pool.submit(download_part, args.cache, name, pin, first, last, chunk_bytes))
                pending.append((name, pin, futures))
            else:
                pending.append((name, pin, pool.submit(download, args.cache, name, pin, args.refresh)))
        results = []
        for name, pin, futures in pending:
            if not isinstance(futures, list):
                results.append(futures.result())
                continue
            target = args.cache / pin["filename"]
            temporary = target.with_suffix(target.suffix + ".partial")
            with temporary.open("wb") as output:
                for future in futures:
                    with future.result().open("rb") as part:
                        shutil.copyfileobj(part, output, length=1024 * 1024)
            result = verify(temporary, pin)
            temporary.replace(target)
            result.update(file=str(target), component=name, version=pin["version"], url=pin["url"])
            results.append(result)
            print(f"Verified complete download: {name}", flush=True)
    manifest = args.cache / "downloads.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else []
    names = {result['component'] for result in results}
    results = [result for result in previous if result['component'] not in names] + results
    manifest.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
