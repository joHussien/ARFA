#!/usr/bin/env python3
"""
download_usa_structures.py

Download, extract, and flatten FEMA / ORNL USA Structures state/territory
File Geodatabase packages from:

    https://disasters.geoplatform.gov/USA_Structures/

Workflow
--------
1. Scrape FEMA's USA Structures page for the current ZIP links.
2. Download each ZIP with resume support.
3. Extract each package.
4. Flatten the intermediate Deliverable... directory.

Example
-------
Input ZIP:
    Deliverable20230502AZ.zip

Typical ZIP contents:
    Deliverable20230502AZ/
        AZ_Structures.gdb/
        AZ_Structures_metadata.xml

Final output:
    <output>/
        AZ/
            AZ_Structures.gdb/
            AZ_Structures_metadata.xml

Requirements
------------
    pip install requests

Examples
--------
Download everything:
    python download_usa_structures.py \
        --output ./Data_USA_Structures/2025_06

Download selected states only:
    python download_usa_structures.py \
        --output ./Data_USA_Structures/2025_06 \
        --states VT AZ IN

Delete ZIPs after successful extraction:
    python download_usa_structures.py \
        --output ./Data_USA_Structures/2025_06 \
        --delete-zips

List discovered packages without downloading:
    python download_usa_structures.py --list
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import time
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

INDEX_URL = "https://disasters.geoplatform.gov/USA_Structures/"
CHUNK_SIZE = 1024 * 1024
TIMEOUT = 90
USER_AGENT = "USA-Structures-Downloader/1.0"


class ZipLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None
            self._text = []


def human_size(n):
    if n is None:
        return "unknown size"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def filename_from_url(url):
    return Path(urlparse(url).path).name


def state_code_from_filename(filename):
    stem = Path(filename).stem
    match = re.search(r"([A-Z]{2})$", stem, re.IGNORECASE)
    if not match:
        raise ValueError(f"Cannot determine state code from: {filename}")
    return match.group(1).upper()


def discover_packages(session):
    print(f"Reading FEMA index:\n  {INDEX_URL}")
    response = session.get(INDEX_URL, timeout=TIMEOUT)
    response.raise_for_status()

    parser = ZipLinkParser()
    parser.feed(response.text)

    packages = []
    seen = set()

    for href, label in parser.links:
        url = urljoin(INDEX_URL, href)

        if not url.lower().endswith(".zip"):
            continue
        if "fema-femadata.s3.amazonaws.com" not in url:
            continue
        if "USA_Structures" not in url:
            continue
        if url in seen:
            continue

        filename = filename_from_url(url)
        try:
            code = state_code_from_filename(filename)
        except ValueError:
            print(f"WARNING: skipping unrecognized ZIP: {url}")
            continue

        packages.append({
            "name": label or code,
            "code": code,
            "filename": filename,
            "url": url,
        })
        seen.add(url)

    packages.sort(key=lambda p: p["code"])

    if not packages:
        raise RuntimeError("No USA Structures ZIP files found.")

    return packages


def get_remote_size(session, url):
    try:
        response = session.head(url, allow_redirects=True, timeout=TIMEOUT)
        if response.ok and response.headers.get("Content-Length"):
            return int(response.headers["Content-Length"])
    except requests.RequestException:
        pass
    return None


def download_file(session, package, zip_path, force=False):
    code = package["code"]
    url = package["url"]
    part_path = Path(str(zip_path) + ".part")
    size = get_remote_size(session, url)

    if force:
        zip_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)

    if zip_path.exists():
        local_size = zip_path.stat().st_size
        if size is None or local_size == size:
            print(f"  [{code}] Reusing existing ZIP: {zip_path.name}")
            return zip_path
        print(f"  [{code}] Existing ZIP size differs; downloading again.")
        zip_path.unlink()

    downloaded = part_path.stat().st_size if part_path.exists() else 0
    headers = {}

    if downloaded and size and downloaded < size:
        headers["Range"] = f"bytes={downloaded}-"
        print(
            f"  [{code}] Resuming at {human_size(downloaded)} "
            f"of {human_size(size)}"
        )
    elif downloaded:
        part_path.unlink(missing_ok=True)
        downloaded = 0

    if downloaded == 0:
        print(
            f"  [{code}] Downloading {package['name']} "
            f"({human_size(size)})"
        )

    with session.get(
        url,
        headers=headers,
        stream=True,
        timeout=TIMEOUT,
    ) as response:
        response.raise_for_status()

        if downloaded and response.status_code != 206:
            print(f"  [{code}] Resume not supported; restarting.")
            part_path.unlink(missing_ok=True)
            downloaded = 0

        mode = "ab" if downloaded else "wb"
        total = downloaded
        last_report = 0

        with part_path.open(mode) as f:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                f.write(chunk)
                total += len(chunk)

                now = time.time()
                if now - last_report >= 1:
                    if size:
                        pct = min(100, total * 100 / size)
                        print(
                            f"\r      {pct:6.2f}% "
                            f"{human_size(total)} / {human_size(size)}",
                            end="",
                            flush=True,
                        )
                    else:
                        print(
                            f"\r      {human_size(total)} downloaded",
                            end="",
                            flush=True,
                        )
                    last_report = now

    print()

    if size is not None and part_path.stat().st_size != size:
        raise RuntimeError("Downloaded file size does not match server size.")

    if not zipfile.is_zipfile(part_path):
        raise RuntimeError("Downloaded file is not a valid ZIP archive.")

    part_path.replace(zip_path)
    print(f"  [{code}] Download complete.")
    return zip_path


def state_is_complete(state_dir, code):
    expected = state_dir / f"{code}_Structures.gdb"
    if expected.is_dir():
        return True

    return any(
        p.is_dir() and p.name.lower().endswith("_structures.gdb")
        for p in state_dir.glob("*.gdb")
    )


def move_contents(source, destination, force=False):
    destination.mkdir(parents=True, exist_ok=True)

    for child in source.iterdir():
        target = destination / child.name

        if target.exists():
            if not force:
                raise FileExistsError(
                    f"{target} already exists. Use --force to replace it."
                )

            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

        shutil.move(str(child), str(target))


def extract_and_flatten(zip_path, state_dir, code, force=False):
    if state_is_complete(state_dir, code) and not force:
        print(f"  [{code}] Already extracted; skipping.")
        return

    if force and state_dir.exists():
        shutil.rmtree(state_dir)

    state_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [{code}] Extracting...")

    with tempfile.TemporaryDirectory(
        prefix=f".extract_{code}_",
        dir=state_dir.parent,
    ) as temp_name:
        temp_dir = Path(temp_name)

        with zipfile.ZipFile(zip_path, "r") as archive:
            # Protect against ZIP path traversal.
            base = temp_dir.resolve()
            for member in archive.infolist():
                target = (temp_dir / member.filename).resolve()
                if target != base and base not in target.parents:
                    raise RuntimeError(
                        f"Unsafe ZIP member path: {member.filename}"
                    )

            archive.extractall(temp_dir)

        top_level = [
            p for p in temp_dir.iterdir()
            if p.name != "__MACOSX"
        ]

        deliverable_dirs = [
            p for p in top_level
            if p.is_dir() and p.name.lower().startswith("deliverable")
        ]

        if len(deliverable_dirs) == 1:
            source_root = deliverable_dirs[0]
        elif len(top_level) == 1 and top_level[0].is_dir():
            source_root = top_level[0]
        else:
            source_root = temp_dir

        move_contents(source_root, state_dir, force=force)

    if not state_is_complete(state_dir, code):
        raise RuntimeError(
            f"No *_Structures.gdb found after extracting {zip_path.name}"
        )

    print(f"  [{code}] Ready: {state_dir}")


def process_package(
    session,
    package,
    output_dir,
    force=False,
    delete_zips=False,
):
    code = package["code"]
    state_dir = output_dir / code
    zip_path = output_dir / package["filename"]

    if state_is_complete(state_dir, code) and not force:
        print(f"  [{code}] Final state folder already exists; skipping.")
        return

    zip_path = download_file(
        session,
        package,
        zip_path,
        force=force,
    )

    extract_and_flatten(
        zip_path,
        state_dir,
        code,
        force=force,
    )

    if delete_zips:
        zip_path.unlink(missing_ok=True)
        print(f"  [{code}] Deleted ZIP after successful extraction.")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Download FEMA/ORNL USA Structures ZIPs, extract them, "
            "and flatten Deliverable subfolders."
        )
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path.cwd() / "USA_Structures",
        help="Output directory.",
    )

    parser.add_argument(
        "--states",
        nargs="*",
        metavar="XX",
        help="Optional state/territory codes, e.g. --states VT AZ IN",
    )

    parser.add_argument(
        "--delete-zips",
        action="store_true",
        help="Delete ZIPs after successful extraction.",
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing downloaded/extracted data.",
    )

    parser.add_argument(
        "--list",
        action="store_true",
        help="List currently published FEMA packages and exit.",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    output_dir = args.output.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
    })

    try:
        packages = discover_packages(session)
    except Exception as exc:
        print(f"ERROR discovering packages: {exc}", file=sys.stderr)
        return 1

    if args.states:
        wanted = {state.upper() for state in args.states}
        available = {p["code"] for p in packages}
        missing = wanted - available

        if missing:
            print(
                "ERROR: these codes were not found on FEMA's page: "
                + ", ".join(sorted(missing)),
                file=sys.stderr,
            )
            return 2

        packages = [p for p in packages if p["code"] in wanted]

    print(f"\nFound {len(packages)} package(s):")
    for p in packages:
        print(
            f"  {p['code']:>2}  "
            f"{p['name']:<28} "
            f"{p['filename']}"
        )

    if args.list:
        return 0

    print(f"\nOutput directory:\n  {output_dir}")

    succeeded = []
    failed = []

    for index, package in enumerate(packages, start=1):
        print("\n" + "=" * 72)
        print(
            f"{index}/{len(packages)} "
            f"{package['code']} — {package['name']}"
        )
        print("=" * 72)

        try:
            process_package(
                session,
                package,
                output_dir,
                force=args.force,
                delete_zips=args.delete_zips,
            )
            succeeded.append(package["code"])

        except KeyboardInterrupt:
            print(
                "\nInterrupted. Rerun the same command to resume."
            )
            return 130

        except Exception as exc:
            print(
                f"ERROR [{package['code']}]: {exc}",
                file=sys.stderr,
            )
            failed.append((package["code"], str(exc)))

    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Successful/skipped: {len(succeeded)}/{len(packages)}")

    if failed:
        print(f"Failed: {len(failed)}")
        for code, message in failed:
            print(f"  {code}: {message}")
        print("\nRerun the same command to retry/resume failures.")
        return 1

    print("All requested packages are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
