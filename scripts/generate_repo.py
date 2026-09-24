#!/usr/bin/env python3
"""
AltStore Source Generator

Scans the ipas/ folder for .ipa files, extracts metadata (bundle ID, version,
icon, etc.) from each, and generates/updates repo.json.

Preserves manually-set fields from existing repo.json entries (descriptions,
subtitles, developer names, tint colors) so you only need to write them once.
Apps that reference external download URLs (not in ipas/) are kept as-is.
IPAs dropped manually onto the ipa-assets release are downloaded and included
too — new ones are named after their file name.

Usage:
    python scripts/generate_repo.py
    python scripts/generate_repo.py --dry-run   # show changes without writing
    python scripts/upload_ipa.py                # drop-in uploader (see its docstring)
"""

import json
import os
import plistlib
import re
import struct
import sys
import time
import urllib.request
import zlib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
IPAS_DIR = REPO_ROOT / "ipas"
ICONS_DIR = REPO_ROOT / "icons"
REPO_JSON = REPO_ROOT / "repo.json"
SOURCES_JSON = REPO_ROOT / "sources.json"

# ── Source-level metadata ────────────────────────────────────────────────────
SOURCE_NAME = "Star's Repository"
SOURCE_IDENTIFIER = "moe.starkayc.repo"
SOURCE_SUBTITLE = "Personal AltStore source"
SOURCE_DESCRIPTION = "Personal AltStore source for IPA distribution."

# Base URL for GitHub Pages — files in the repo root are served from here.
GITHUB_USER = "starkayc"
GITHUB_REPO = "iOS"
PAGES_BASE = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}"

# GitHub Release used to host IPA binaries (avoids LFS-pointer issues with
# GitHub Pages).  Workflow uploads each IPA as a release asset.
RELEASE_TAG = "ipa-assets"
RELEASE_BASE = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/releases/download/{RELEASE_TAG}"

SOURCE_ICON_URL = f"https://github.com/{GITHUB_USER}.png"
SOURCE_WEBSITE = "https://github.com/starkayc/iOS"
SOURCE_TINT_COLOR = "3c94fc"


# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_version_tuple(version: str) -> tuple:
    """Parse a version string into a comparable tuple of ints.

    >>> parse_version_tuple("1.6") > parse_version_tuple("1.5.2")
    True
    >>> parse_version_tuple("2.0.0-beta") < parse_version_tuple("2.0.0")
    True  (shorter = prerelease, sorts lower)
    """
    # Split on dots, then extract leading digits from each segment.
    parts = []
    for segment in version.split("."):
        m = re.match(r"(\d+)", segment)
        if m:
            parts.append(int(m.group(1)))
        else:
            # Non-numeric segment → treat as 0 so "beta" sorts lower than any
            # numbered release.
            parts.append(0)
    return tuple(parts)


def url_encode_path(path: str) -> str:
    """Percent-encode a URL path component (but keep slashes)."""
    return quote(path, safe="/")


def find_app_bundle(zf: zipfile.ZipFile) -> Optional[str]:
    """Return the first .app/ directory inside Payload/."""
    for name in zf.namelist():
        if name.startswith("Payload/") and name.endswith(".app/"):
            # Should be exactly Payload/Name.app/ (not nested deeper).
            parts = name[len("Payload/"):].rstrip("/").split("/")
            if len(parts) == 1:
                return name
    return None


# ── IPA metadata extraction ──────────────────────────────────────────────────

def extract_info_plist(zf: zipfile.ZipFile, app_dir: str) -> Optional[dict]:
    """Read and parse the Info.plist from the app bundle."""
    plist_path = app_dir + "Info.plist"
    if plist_path not in zf.namelist():
        return None
    with zf.open(plist_path) as f:
        return plistlib.load(f)


def find_icon_files(info: dict, zf: zipfile.ZipFile, app_dir: str) -> list[str]:
    """Return a list of icon file paths inside the IPA, sorted by resolution
    (largest file size last)."""
    candidates: set[str] = set()

    # Modern path: CFBundleIcons → CFBundlePrimaryIcon → CFBundleIconFiles
    bundle_icons = info.get("CFBundleIcons", {})
    primary = bundle_icons.get("CFBundlePrimaryIcon", {})
    for name in primary.get("CFBundleIconFiles", []):
        candidates.add(name)

    # Legacy path: CFBundleIconFiles (array)
    for name in info.get("CFBundleIconFiles", []):
        candidates.add(name)

    # Legacy singular
    singular = info.get("CFBundleIconFile")
    if singular:
        candidates.add(singular)

    # Build a set of all files in the app bundle for quick lookup.
    all_files = {n for n in zf.namelist() if n.startswith(app_dir)}

    found: list[tuple[int, str]] = []  # (size, path)

    for icon_name in candidates:
        # The name might or might not include extension / @2x suffix.
        base = os.path.splitext(icon_name)[0]
        for full_path in all_files:
            fname = os.path.basename(full_path)
            fbase = os.path.splitext(fname)[0]
            # Match exact, or base-with-@2x/@3x suffix, or AppIcon variants.
            if fbase == icon_name or fbase == base or fbase.startswith(base):
                if full_path.lower().endswith((".png", ".jpg", ".jpeg")):
                    try:
                        info_entry = zf.getinfo(full_path)
                        found.append((info_entry.file_size, full_path))
                    except KeyError:
                        pass

    # If nothing matched specific icon names, fall back to any large PNG that
    # looks like an icon.
    if not found:
        for full_path in all_files:
            fname = os.path.basename(full_path).lower()
            if any(
                keyword in fname
                for keyword in ("appicon", "icon", "app_icon")
            ):
                if full_path.lower().endswith((".png", ".jpg", ".jpeg")):
                    try:
                        info_entry = zf.getinfo(full_path)
                        found.append((info_entry.file_size, full_path))
                    except KeyError:
                        pass

    # Sort by size — largest = highest resolution.
    found.sort(key=lambda x: x[0])
    return [p for _, p in found]


def extract_metadata(ipa_path: Path) -> Optional[dict]:
    """Open an IPA and extract all relevant metadata.

    Returns a dict with keys:
        bundleIdentifier, name, version, buildVersion, minOSVersion,
        icon_paths (list of paths inside the IPA, largest last),
    or None if the IPA couldn't be parsed.
    """
    try:
        with zipfile.ZipFile(ipa_path, "r") as zf:
            app_dir = find_app_bundle(zf)
            if not app_dir:
                print(f"  ⚠ No .app bundle found in Payload/")
                return None

            info = extract_info_plist(zf, app_dir)
            if not info:
                print(f"  ⚠ No Info.plist found")
                return None

            bundle_id = info.get("CFBundleIdentifier", "unknown")
            name = (
                info.get("CFBundleDisplayName")
                or info.get("CFBundleName")
                or bundle_id
            )
            version = info.get("CFBundleShortVersionString", "1.0")
            build = info.get("CFBundleVersion", "1")
            min_os = info.get("MinimumOSVersion", "12.0")

            icon_paths = find_icon_files(info, zf, app_dir)

            return {
                "bundleIdentifier": bundle_id,
                "name": name,
                "version": version,
                "buildVersion": build,
                "minOSVersion": min_os,
                "icon_paths": icon_paths,
            }
    except (zipfile.BadZipFile, KeyError, plistlib.InvalidFileException) as e:
        print(f"  ✗ Failed to read IPA: {e}")
        return None


def patch_bundle_id(ipa_path: Path, new_bundle_id: str) -> bool:
    """Rewrite CFBundleIdentifier in the IPA's Info.plist.

    AltStore re-signs apps on install, so changing the bundle ID lets two
    builds of the same app coexist in one source (e.g. "Nuvio" and
    "Nuvio Enhanced").  The Info.plist is re-serialized in its original
    format and the zip rebuilt in place.  Idempotent.
    """
    with zipfile.ZipFile(ipa_path, "r") as zf:
        app_dir = find_app_bundle(zf)
        if not app_dir:
            print(f"    ⚠ Cannot patch bundle ID — no .app bundle in {ipa_path.name}")
            return False
        plist_path = app_dir + "Info.plist"
        if plist_path not in zf.namelist():
            print(f"    ⚠ Cannot patch bundle ID — no Info.plist in {ipa_path.name}")
            return False
        raw = zf.read(plist_path)

    info = plistlib.loads(raw)
    if info.get("CFBundleIdentifier") == new_bundle_id:
        print(f"    ✓ bundle ID already {new_bundle_id}")
        return True

    info["CFBundleIdentifier"] = new_bundle_id
    is_binary = raw.startswith(b"bplist00")
    new_raw = plistlib.dumps(
        info, fmt=plistlib.FMT_BINARY if is_binary else plistlib.FMT_XML
    )

    # Rebuild the zip with the patched plist.  Written to a temp file
    # first — Windows can't replace a file that still has a handle open.
    tmp_path = ipa_path.with_suffix(".ipa.patched")
    with zipfile.ZipFile(ipa_path, "r") as zin, zipfile.ZipFile(
        tmp_path, "w"
    ) as zout:
        for item in zin.infolist():
            if item.filename == plist_path:
                zout.writestr(
                    item, new_raw, compress_type=zipfile.ZIP_DEFLATED
                )
            else:
                zout.writestr(
                    item, zin.read(item.filename), compress_type=item.compress_type
                )
    tmp_path.replace(ipa_path)
    print(f"    ↯ bundle ID → {new_bundle_id}")
    return True


def _uncrush_png(data: bytes) -> bytes:
    """Convert an Apple crushed PNG (CgBI) to a standard PNG.

    iOS apps often ship "crushed" PNGs that have their red and blue colour
    channels swapped and use raw deflate instead of zlib-wrapped deflate.
    This function reverses both transforms so the result is a valid PNG
    that any image viewer can open.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return data  # not a PNG

    # ── Parse chunks ──────────────────────────────────────────────────
    chunks: list[tuple[bytes, bytes]] = []  # (type, data)
    pos = 8
    while pos < len(data):
        if pos + 12 > len(data):
            break
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        cdata = data[pos + 8 : pos + 8 + length]
        chunks.append((ctype, cdata))
        pos += 12 + length

    if not chunks or chunks[0][0] != b"CgBI":
        return data  # not crushed

    # ── Extract IHDR info ─────────────────────────────────────────────
    ihdr = None
    for ctype, cdata in chunks:
        if ctype == b"IHDR":
            ihdr = cdata
            break
    if not ihdr:
        return data

    width = struct.unpack(">I", ihdr[0:4])[0]
    height = struct.unpack(">I", ihdr[4:8])[0]
    bit_depth = ihdr[8]
    color_type = ihdr[9]

    # Bytes per pixel (PNG spec: ceil(bits_per_pixel / 8), min 1).
    samples_per_pixel = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 1)
    bpp = max(1, (samples_per_pixel * bit_depth) // 8)

    # ── Decompress IDAT (raw deflate — no zlib header) ────────────────
    idat_data = b"".join(
        cdata for ctype, cdata in chunks if ctype == b"IDAT"
    )
    try:
        raw = zlib.decompress(idat_data, -15)  # raw deflate
    except zlib.error:
        return data

    # ── Unfilter each scanline ────────────────────────────────────────
    stride = width * bpp + 1  # filter byte + pixel row
    prev_row: Optional[bytearray] = None
    rows: list[bytearray] = []

    for row_idx in range(height):
        start = row_idx * stride
        if start + stride > len(raw):
            break
        filt = raw[start]
        row = bytearray(raw[start + 1 : start + stride])

        # Reverse PNG filter.
        if filt == 1:  # Sub
            for i in range(bpp, len(row)):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif filt == 2 and prev_row:  # Up
            for i in range(len(row)):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif filt == 3:  # Average
            for i in range(len(row)):
                left = row[i - bpp] if i >= bpp else 0
                up = prev_row[i] if prev_row else 0
                row[i] = (row[i] + (left + up) // 2) & 0xFF
        elif filt == 4:  # Paeth
            for i in range(len(row)):
                left = row[i - bpp] if i >= bpp else 0
                up = prev_row[i] if prev_row else 0
                up_left = prev_row[i - bpp] if prev_row and i >= bpp else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                pr = left if pa <= pb and pa <= pc else up if pb <= pc else up_left
                row[i] = (row[i] + pr) & 0xFF

        # Swap R ↔ B for RGB / RGBA images (Apple stores as BGRA).
        if color_type in (2, 6):
            for p in range(0, len(row) - bpp + 1, bpp):
                row[p], row[p + 2] = row[p + 2], row[p]  # swap R,B

        rows.append(row)
        prev_row = row

    # For indexed images, swap R ↔ B in the PLTE palette instead.
    if color_type == 3:
        for ctype, cdata in chunks:
            if ctype == b"PLTE":
                plt = bytearray(cdata)
                for i in range(0, len(plt) - 2, 3):
                    plt[i], plt[i + 2] = plt[i + 2], plt[i]
                chunks = [
                    (b"PLTE", bytes(plt)) if t == b"PLTE" else (t, d)
                    for t, d in chunks
                ]
                break

    # ── Re-compress with filter None (type 0) ─────────────────────────
    re_encoded = b"".join(b"\x00" + bytes(r) for r in rows)
    new_idat = zlib.compress(re_encoded)

    # ── Rebuild PNG ───────────────────────────────────────────────────
    def _write_chunk(buf: bytearray, ctype: bytes, cdata: bytes) -> None:
        buf.extend(struct.pack(">I", len(cdata)))
        buf.extend(ctype)
        buf.extend(cdata)
        crc = zlib.crc32(ctype + cdata) & 0xFFFFFFFF
        buf.extend(struct.pack(">I", crc))

    out = bytearray(b"\x89PNG\r\n\x1a\n")
    for ctype, cdata in chunks:
        # Strip every Apple-specific / data chunk — we write fresh
        # IDAT + IEND ourselves.
        if ctype in (b"CgBI", b"iDOT", b"IDAT", b"IEND"):
            continue
        _write_chunk(out, ctype, cdata)
    _write_chunk(out, b"IDAT", new_idat)
    _write_chunk(out, b"IEND", b"")

    return bytes(out)


def extract_icon(ipa_path: Path, icon_paths: list[str], bundle_id: str) -> Optional[str]:
    """Extract the largest icon from the IPA, uncrush if needed, and save
    to icons/.

    Returns the output filename (e.g. 'com.example.app.png') on success,
    or None if no suitable icon was found.
    """
    if not icon_paths:
        print(f"    No icon candidates found")
        return None

    # Largest icon is last (sorted by size ascending).
    best = icon_paths[-1]
    ext = os.path.splitext(best)[1] or ".png"
    output_name = f"{bundle_id}{ext}"
    output_path = ICONS_DIR / output_name

    try:
        with zipfile.ZipFile(ipa_path, "r") as zf:
            raw = zf.read(best)

        # Convert Apple crushed-PNG → standard PNG when needed.
        uncrushed = _uncrush_png(raw)
        output_path.write_bytes(uncrushed)

        size_kb = output_path.stat().st_size // 1024
        tag = " (uncrushed)" if uncrushed != raw else ""
        print(f"    ✓ Icon extracted → icons/{output_name} ({size_kb} KB){tag}")
        return output_name
    except Exception as e:
        print(f"    ✗ Failed to extract icon: {e}")
        return None


# ── GitHub release fetching ───────────────────────────────────────────────────

def _github_api(url: str, token: Optional[str] = None) -> dict | list:
    """Call the GitHub API and return parsed JSON (retries transient errors)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "altstore-source-generator",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < 3:
                time.sleep(2 * attempt)
    raise last_err


def _download_file(url: str, dest: Path, token: Optional[str] = None) -> None:
    """Download a file to disk (retries transient network errors)."""
    headers = {"Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last_err: Optional[Exception] = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as resp:
                dest.write_bytes(resp.read())
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = e
            if attempt < 3:
                time.sleep(2 * attempt)
    raise last_err


def _find_ipa_asset(release: dict, pattern: Optional[str] = None) -> Optional[dict]:
    """Return the best .ipa asset from a GitHub release, or None.

    When a release has multiple IPA variants (e.g. base, GLASS,
    NOEXTENSIONS), the shortest name is usually the plain/default build.
    Pass ``pattern`` to match a specific variant instead (e.g.
    pattern="GLASS" matches *-GLASS.ipa but not *-GLASSICONS.ipa or
    *-GLASS-NOEXTENSIONS.ipa).
    """
    ipa_assets = [
        a for a in release.get("assets", [])
        if a["name"].lower().endswith(".ipa")
    ]
    if not ipa_assets:
        return None

    if pattern:
        pat = pattern.lower()

        # Split each filename into dash/dot-delimited segments so we
        # match whole "words" — "GLASS" matches *-GLASS.ipa but not
        # *-GLASSICONS.ipa or *-GLASS-NOEXTENSIONS.ipa.
        def _segment_count(asset: dict) -> int:
            base = asset["name"].rsplit(".", 1)[0]  # strip .ipa
            return len(re.split(r"[-.]", base))

        matching = [
            a for a in ipa_assets
            if pat in re.split(r"[-.]", a["name"].lower().rsplit(".", 1)[0])
        ]

        if matching:
            # Prefer the simplest match (fewest filename segments,
            # then shortest name).
            matching.sort(key=lambda a: (_segment_count(a), len(a["name"])))
            return matching[0]

        print(f"    ⚠ No IPA with segment '{pattern}', falling back to default")

    # Pick the shortest filename — the base build has no extra suffixes.
    ipa_assets.sort(key=lambda a: len(a["name"]))
    return ipa_assets[0]


def fetch_from_sources(
    token: Optional[str] = None,
) -> dict:
    """Download IPAs from the GitHub repos listed in sources.json, plus any
    IPAs manually dropped onto the ipa-assets release.

    Returns a dict (mostly keyed by IPA filename):
      - release_dates: ISO 8601 dates
      - repo_descriptions: the repo "About" text
      - developer_names: repo owner logins
      - manual_names: set of filenames from manual release drops
      - patch_overrides: bundle-ID overrides from sources.json
      - display_names: display-name overrides from sources.json
    """
    if not SOURCES_JSON.exists():
        return {}

    with open(SOURCES_JSON, encoding="utf-8") as f:
        config = json.load(f)

    sources = config.get("sources", [])
    if not sources:
        return {}

    IPAS_DIR.mkdir(exist_ok=True)
    print("Fetching IPAs from GitHub releases …\n")
    release_dates: dict[str, str] = {}
    repo_descriptions: dict[str, str] = {}
    developer_names: dict[str, str] = {}
    manual_names: set[str] = set()
    patch_overrides: dict[str, str] = {}
    display_names: dict[str, str] = {}

    for source in sources:
        name = source["name"]
        repo = source["repo"]
        release_type = source.get("release_type", "stable")

        print(f"  🔍 {name}  ({repo})")

        # ── Fetch repo info (for description/subtitle/developer) ────────
        try:
            repo_info = _github_api(
                f"https://api.github.com/repos/{repo}", token
            )
            repo_desc = (repo_info.get("description") or "").strip()
            repo_owner = (repo_info.get("owner", {}) or {}).get("login", "")
            if repo_desc:
                print(f"    About: {repo_desc[:80]}{'…' if len(repo_desc) > 80 else ''}")
        except Exception:
            repo_desc = ""
            repo_owner = ""

        # ── Fetch releases ──────────────────────────────────────────────
        try:
            api_url = f"https://api.github.com/repos/{repo}/releases?per_page=10"
            releases = _github_api(api_url, token)
        except Exception as e:
            print(f"    ✗ API error: {e}")
            continue

        # ── Find the right release ──────────────────────────────────────
        if release_type == "prerelease":
            target = next((r for r in releases if r.get("prerelease")), None)
        else:
            target = next((r for r in releases if not r.get("prerelease")), None)

        if not target:
            print(f"    ✗ No {release_type} release found")
            continue

        tag = target["tag_name"]
        published = target.get("published_at", "")

        dest_name = f"{name}.ipa"
        dest_path = IPAS_DIR / dest_name

        asset = _find_ipa_asset(target, source.get("asset_pattern"))
        if not asset:
            # Some repos ship the IPA zipped (e.g. Ferrite) — extract it.
            zip_assets = [
                a for a in target.get("assets", [])
                if a["name"].lower().endswith(".ipa.zip")
            ]
            if zip_assets:
                zip_asset = zip_assets[0]

                # Skip if we already have a current build locally.
                if dest_path.exists():
                    existing_meta = extract_metadata(dest_path)
                    if existing_meta and parse_version_tuple(
                        existing_meta["version"]
                    ) >= parse_version_tuple(tag.lstrip("v")):
                        print(f"    ✓ already current  ({tag}, zipped asset)")
                        release_dates[dest_name] = published
                        repo_descriptions[dest_name] = repo_desc
                        developer_names[dest_name] = repo_owner
                        continue

                print(
                    f"    ↓ downloading {zip_asset['name']} "
                    f"({zip_asset['size']:,} bytes, zipped) … ",
                    end="",
                    flush=True,
                )
                tmp_zip = IPAS_DIR / (dest_name + ".zip")
                try:
                    _download_file(
                        zip_asset["browser_download_url"], tmp_zip, token
                    )
                    with zipfile.ZipFile(tmp_zip) as zf:
                        ipa_entry = next(
                            (
                                n for n in zf.namelist()
                                if n.lower().endswith(".ipa")
                            ),
                            None,
                        )
                        ipa_bytes = zf.read(ipa_entry) if ipa_entry else None
                    # Unlink after the zip is closed — Windows can't
                    # delete a file that still has a handle open.
                    tmp_zip.unlink()
                    if ipa_bytes is None:
                        print(
                            f"\n    ✗ no .ipa found inside "
                            f"{zip_asset['name']}"
                        )
                        continue
                    dest_path.write_bytes(ipa_bytes)
                    print(f"done (extracted {ipa_entry})")
                    release_dates[dest_name] = published
                    repo_descriptions[dest_name] = repo_desc
                    developer_names[dest_name] = repo_owner
                    continue
                except Exception as e:
                    print(f"failed: {e}")
                    tmp_zip.unlink(missing_ok=True)
                    continue
            print(f"    ✗ No .ipa asset in {tag}")
            continue

        # ── Skip if already up-to-date ─────────────────────────────────
        asset_size = asset["size"]

        # Bookkeeping used when building repo.json.  The local file name
        # is canonical: the workflow uploads it to the ipa-assets release
        # under exactly this name.
        if source.get("bundle_id_override"):
            patch_overrides[dest_name] = source["bundle_id_override"]
        if source.get("display_name"):
            display_names[dest_name] = source["display_name"]

        if dest_path.exists():
            # Quick check: same file size → probably same version.
            if dest_path.stat().st_size == asset_size:
                print(f"    ✓ already current  ({tag}, {asset_size:,} bytes)")
                release_dates[dest_name] = published
                repo_descriptions[dest_name] = repo_desc
                developer_names[dest_name] = repo_owner
                continue

            # Size differs — new version available.  Double-check by
            # peeking at the existing IPA's version so we don't
            # accidentally downgrade.
            existing_meta = extract_metadata(dest_path)
            if existing_meta and parse_version_tuple(
                existing_meta["version"]
            ) >= parse_version_tuple(tag.lstrip("v")):
                print(
                    f"    ✓ local is newer  "
                    f"(local v{existing_meta['version']} ≥ {tag})"
                )
                release_dates[dest_name] = published
                repo_descriptions[dest_name] = repo_desc
                developer_names[dest_name] = repo_owner
                continue

        # ── Download ────────────────────────────────────────────────────
        print(
            f"    ↓ downloading {asset['name']}  "
            f"({asset_size:,} bytes) … ",
            end="",
            flush=True,
        )
        try:
            _download_file(asset["browser_download_url"], dest_path, token)
            release_dates[dest_name] = published
            repo_descriptions[dest_name] = repo_desc
            developer_names[dest_name] = repo_owner
            print("done")
        except Exception as e:
            print(f"failed: {e}")

    # ── Pick up IPAs manually dropped onto the ipa-assets release ─────────
    # Anything uploaded there that wasn't produced by sources.json is a
    # hand-built IPA — download it so it joins the source on the next scan.
    try:
        release = _github_api(
            f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}"
            f"/releases/tags/{RELEASE_TAG}",
            token,
        )
    except Exception:
        release = None  # release doesn't exist yet

    if release:
        for a in release.get("assets", []):
            asset_name = a["name"]
            if not asset_name.lower().endswith(".ipa"):
                continue
            dest_path = IPAS_DIR / asset_name
            if dest_path.exists():
                continue  # already fetched from sources.json
            print(f"  📥 manual drop: {asset_name} ({a['size']:,} bytes)")
            try:
                _download_file(a["browser_download_url"], dest_path, token)
                manual_names.add(asset_name)
                release_dates[asset_name] = a.get("updated_at", "")
            except Exception as e:
                print(f"    ✗ failed: {e}")
        print()

    return {
        "release_dates": release_dates,
        "repo_descriptions": repo_descriptions,
        "developer_names": developer_names,
        "manual_names": manual_names,
        "patch_overrides": patch_overrides,
        "display_names": display_names,
    }


# ── Repo.json management ─────────────────────────────────────────────────────

def load_existing_repo() -> Optional[dict]:
    """Load the current repo.json, or None if it doesn't exist."""
    if REPO_JSON.exists():
        with open(REPO_JSON, encoding="utf-8") as f:
            return json.load(f)
    return None


def index_by_bundle_id(repo: Optional[dict]) -> dict[str, dict]:
    """Build {bundleIdentifier: app_entry} from an existing repo.json."""
    if not repo:
        return {}
    return {app["bundleIdentifier"]: app for app in repo.get("apps", [])}


# ── Main ─────────────────────────────────────────────────────────────────────

def generate_repo(
    fetch: bool = False,
    token: Optional[str] = None,
    dry_run: bool = False,
    manual_names: Optional[set[str]] = None,
) -> bool:
    """Scan ipas/, update repo.json, return True if changes were made.

    With --fetch, downloads IPAs from sources.json first.
    Pass --github-token for authenticated GitHub API calls.
    manual_names marks IPAs as hand-dropped (named after their file).
    """
    print("=" * 60)
    print("  AltStore Source Generator")
    print("=" * 60)

    # Ensure directories exist.
    ICONS_DIR.mkdir(exist_ok=True)
    IPAS_DIR.mkdir(exist_ok=True)

    # ── Fetch from GitHub releases (if requested) ──────────────────────────
    fetch_info = fetch_from_sources(token) if fetch else {}
    release_dates = fetch_info.get("release_dates", {})
    repo_descriptions = fetch_info.get("repo_descriptions", {})
    developer_names = fetch_info.get("developer_names", {})
    manual_names = manual_names or fetch_info.get("manual_names", set())
    patch_overrides = fetch_info.get("patch_overrides", {})
    display_names = fetch_info.get("display_names", {})

    # Load existing state.
    existing = load_existing_repo()
    existing_apps = index_by_bundle_id(existing)
    print(f"Loaded existing repo.json — {len(existing_apps)} app(s)")

    # ── Scan IPAs ─────────────────────────────────────────────────────────
    ipa_files = sorted(IPAS_DIR.glob("*.ipa"))
    if not ipa_files:
        print("\nNo .ipa files found in ipas/ folder.")
        print("Add your .ipa files there and run again.\n")
        return False

    print(f"Scanning {len(ipa_files)} IPA(s) in ipas/ …\n")

    processed: dict[str, dict] = {}   # bundle_id → new app entry
    processed_manual: dict[str, bool] = {}  # bundle_id → came from manual drop
    processed_file: dict[str, Path] = {}    # bundle_id → source IPA file
    changes: list[str] = []           # human-readable change log

    for ipa_path in ipa_files:
        print(f"  📦 {ipa_path.name}")

        # Bundle-ID override: lets two builds of the same app coexist in
        # one source (AltStore re-signs on install, so the patched ID
        # becomes the app's real identity).
        if ipa_path.name in patch_overrides:
            patch_bundle_id(ipa_path, patch_overrides[ipa_path.name])

        meta = extract_metadata(ipa_path)
        if not meta:
            continue

        bundle_id = meta["bundleIdentifier"]
        old = existing_apps.get(bundle_id)
        is_manual = ipa_path.name in manual_names

        # If we've already seen this bundle ID (duplicate IPAs), keep the
        # higher version.  On a tie, a sources.json build wins over a
        # manual drop.
        if bundle_id in processed:
            prev_ver = processed[bundle_id]["version"]
            new_t = parse_version_tuple(meta["version"])
            prev_t = parse_version_tuple(prev_ver)
            keep = (
                new_t > prev_t
                or (new_t == prev_t and processed_manual[bundle_id] and not is_manual)
            )
            if keep:
                print(f"    (replacing v{prev_ver} with v{meta['version']})")
                if fetch:
                    loser = processed_file[bundle_id]
                    print(f"      (removing {loser.name} — same bundle ID)")
                    loser.unlink()
            else:
                print(
                    f"    ⚠ duplicate bundle ID {bundle_id} — keeping "
                    f"{processed[bundle_id].get('name', '?')} v{prev_ver}, "
                    f"skipping {ipa_path.name}"
                )
                # Collision losers are dropped so the workflow cleans up
                # their stale release assets too.
                if fetch:
                    print(f"      (removing {ipa_path.name})")
                    ipa_path.unlink()
                continue

        # Extract icon.
        icon_filename = extract_icon(ipa_path, meta["icon_paths"], bundle_id)
        icon_url = f"{PAGES_BASE}/icons/{url_encode_path(icon_filename)}" if icon_filename else ""

        # IPA size + download URL (served from GitHub Releases, not
        # Pages, to avoid Git-LFS pointer files being served as IPAs).
        # The local file name is canonical: files fetched from sources
        # are uploaded under it by the workflow, and manual drops are
        # downloaded by their asset name, so the two always match.
        ipa_size = ipa_path.stat().st_size
        safe_ipa_name = url_encode_path(ipa_path.name)
        download_url = f"{RELEASE_BASE}/{safe_ipa_name}"

        # Use the GitHub release date when available, otherwise fall
        # back to the file modification time.
        release_date_str = release_dates.get(ipa_path.name)
        if release_date_str:
            ipa_mtime = datetime.fromisoformat(
                release_date_str.replace("Z", "+00:00")
            )
        else:
            ipa_mtime = datetime.fromtimestamp(
                ipa_path.stat().st_mtime, tz=timezone.utc
            )

        # Build the version object.
        version_obj = {
            "downloadURL": download_url,
            "size": ipa_size,
            "version": meta["version"],
            "buildVersion": meta["buildVersion"],
            "date": ipa_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "localizedDescription": "",
            "minOSVersion": meta["minOSVersion"],
        }

        # Display name: sources.json override, then the file name for
        # manual drops, then the IPA's own display name.
        default_name = display_names.get(ipa_path.name, "") or (
            ipa_path.stem if is_manual else meta["name"]
        )

        # ── Merge with existing entry (preserve manual fields) ──────────
        if old:
            app_entry = {
                "name": old.get("name", meta["name"]),
                "bundleIdentifier": bundle_id,
                "developerName": old.get("developerName") or developer_names.get(ipa_path.name, ""),
                "iconURL": icon_url or old.get("iconURL", ""),
                "localizedDescription": old.get("localizedDescription") or repo_descriptions.get(ipa_path.name, ""),
                "subtitle": old.get("subtitle") or repo_descriptions.get(ipa_path.name, ""),
                "tintColor": old.get("tintColor", SOURCE_TINT_COLOR),
                "category": old.get("category", "utilities"),
                "versions": [version_obj],
                "appPermissions": old.get("appPermissions", {}),
                # Top-level convenience fields (AltStore uses these too).
                "version": meta["version"],
                "versionDate": ipa_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": ipa_size,
                "downloadURL": download_url,
            }
            prev_ver = old.get("version", "—")
            if prev_ver != meta["version"]:
                changes.append(
                    f"  ↑ {meta['name']}: {prev_ver} → {meta['version']}"
                )
            else:
                # Version didn't change, but metadata might have (e.g.
                # back-filled developer name or description).
                meta_keys = (
                    "developerName", "localizedDescription",
                    "subtitle", "iconURL", "downloadURL",
                )
                if any(
                    old.get(k) != app_entry.get(k)
                    for k in meta_keys
                ):
                    changes.append(
                        f"  ✎ {meta['name']}: metadata updated"
                    )
                else:
                    print(f"    (unchanged — v{meta['version']})")
        else:
            # Use the repo "About" text and owner as defaults for new
            # apps.  You can override any field in repo.json and it
            # won't be overwritten on subsequent runs.
            gh_desc = repo_descriptions.get(ipa_path.name, "")
            gh_dev = developer_names.get(ipa_path.name, "")
            app_entry = {
                "name": default_name,
                "bundleIdentifier": bundle_id,
                "developerName": gh_dev,
                "iconURL": icon_url,
                "localizedDescription": gh_desc,
                "subtitle": gh_desc,
                "tintColor": SOURCE_TINT_COLOR,
                "category": "utilities",
                "versions": [version_obj],
                "appPermissions": {},
                "version": meta["version"],
                "versionDate": ipa_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": ipa_size,
                "downloadURL": download_url,
            }
            changes.append(f"  ✦ NEW: {default_name} ({bundle_id})")

        processed[bundle_id] = app_entry
        processed_manual[bundle_id] = is_manual
        processed_file[bundle_id] = ipa_path

    # ── Preserve external-only apps ───────────────────────────────────────
    external_count = 0
    for bid, app in existing_apps.items():
        if bid not in processed:
            print(f"  🔗 {app.get('name', bid)} (external — preserved)")
            processed[bid] = app
            external_count += 1

    if external_count:
        print(f"\n  ({external_count} external app(s) preserved)")

    # ── Check if anything changed ─────────────────────────────────────────
    if not changes:
        print("\n✓ No changes. repo.json is already up to date.\n")
        return False

    # ── Build output ──────────────────────────────────────────────────────
    repo = {
        "name": SOURCE_NAME,
        "identifier": SOURCE_IDENTIFIER,
        "subtitle": SOURCE_SUBTITLE,
        "description": SOURCE_DESCRIPTION,
        "iconURL": SOURCE_ICON_URL,
        "website": SOURCE_WEBSITE,
        "tintColor": SOURCE_TINT_COLOR,
        "apps": list(processed.values()),
    }

    if dry_run:
        print(f"\n── DRY RUN — would write repo.json ──")
        print(json.dumps(repo, indent=2, ensure_ascii=False))
        print(f"\n  {len(changes)} change(s):")
        for c in changes:
            print(c)
        print()
        return True

    # Write.
    with open(REPO_JSON, "w", encoding="utf-8") as f:
        json.dump(repo, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\n{'=' * 60}")
    print(f"  {len(changes)} change(s):")
    for c in changes:
        print(c)
    print(f"  ✓ repo.json written ({len(processed)} total apps)")
    print(f"{'=' * 60}\n")

    return True


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    do_fetch = "--fetch" in sys.argv

    token = None
    for arg in sys.argv:
        if arg.startswith("--github-token="):
            token = arg.split("=", 1)[1]
            break

    changed = generate_repo(fetch=do_fetch, token=token, dry_run=dry)
    sys.exit(0 if changed else 0)
