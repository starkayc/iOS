#!/usr/bin/env python3
"""
Shared library for the AltStore source pipeline.

All scripts import from here with ``import altstore_lib as lib`` and
reference constants through the module (lib.IPAS_DIR, …) so tests can
point the whole pipeline at temp directories by patching the module.

Sections:
  - Paths and source-level metadata
  - File naming (canonical names, versioned IPA file names)
  - config file I/O (sources.json, current_releases.json, custom meta)
  - GitHub API (github_api, download_file, GitHubRelease client)
  - IPA metadata (extract, icons, bundle-ID patching)
  - Release checking (latest_release_info, check_for_updates)
"""

import json
import os
import plistlib
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

# Force UTF-8 output on Windows terminals that default to cp1252.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ── Paths ────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
IPAS_DIR = REPO_ROOT / "ipas"
ICONS_DIR = REPO_ROOT / "icons"
REPO_JSON = REPO_ROOT / "repo.json"
SOURCES_JSON = REPO_ROOT / "sources.json"
CURRENT_RELEASES_JSON = REPO_ROOT / "current_releases.json"

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
# GitHub Pages).  The workflow syncs ipas/*.ipa onto this release.
RELEASE_TAG = "ipa-assets"
RELEASE_BASE = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/releases/download/{RELEASE_TAG}"

SOURCE_ICON_URL = f"https://github.com/{GITHUB_USER}.png"
SOURCE_WEBSITE = "https://github.com/starkayc/iOS"
SOURCE_TINT_COLOR = "3c94fc"


# ── Small helpers ────────────────────────────────────────────────────────────

def parse_version_tuple(version: str) -> tuple:
    """Parse a version string into a comparable tuple of ints.

    >>> parse_version_tuple("1.6") > parse_version_tuple("1.5.2")
    True
    >>> parse_version_tuple("2.0.0-beta") < parse_version_tuple("2.0.0")
    True  (shorter = prerelease, sorts lower)
    """
    parts = []
    for segment in version.split("."):
        m = re.match(r"(\d+)", segment)
        if m:
            parts.append(int(m.group(1)))
        else:
            # Non-numeric segment → treat as 0 so "beta" sorts lower than
            # any numbered release.
            parts.append(0)
    return tuple(parts)


def url_encode_path(path: str) -> str:
    """Percent-encode a URL path component.

    Slashes separate path segments and parentheses are legal in URLs
    (our versioned file names use them, e.g. "Feather(rel-2.10.0).ipa").
    Everything else gets percent-encoded.
    """
    return quote(path, safe="/()")


# ── File naming ──────────────────────────────────────────────────────────────

def canonical_filename(name: str) -> str:
    """Swap spaces for dashes in an IPA file name.

    GitHub's release API (and web UI) normalizes spaces in asset names
    to dots, which desyncs repo.json URLs from the actual assets.
    Dashes are safe everywhere, so spaces never enter the pipeline.
    """
    return name.replace(" ", "-")


def sanitize_version(version: str) -> str:
    """Strip a trailing pre-release suffix for use in file names.

    "0.5.1-beta" → "0.5.1", "2.0.0-rc.2" → "2.0.0", "1.0" → "1.0".
    current_releases.json keeps the raw tag so a beta→stable move is
    still detected as a change; file names use this sanitized form.
    """
    cleaned = re.sub(
        r"[-_.]?(alpha|beta|rc|preview|pre)[-_.]?\d*$",
        "",
        version,
        flags=re.IGNORECASE,
    ).strip("-. _")
    return cleaned or version  # never return an empty name


def github_asset_name(name: str) -> str:
    """Mirror GitHub's release-asset name sanitization.

    GitHub's upload API (and web UI) rewrites characters it doesn't
    allow in asset names: each unsafe character becomes a dot and runs
    of dots collapse (observed: " " → ".", "(" → ".", ")" → removed;
    "Feather(rel-2.9.0).ipa" is stored as "Feather.rel-2.9.0.ipa").
    The download endpoint matches exactly, so every comparison between
    a local file name and a server asset name goes through this.
    """
    sanitized = re.sub(r"[^A-Za-z0-9._-]", ".", name)
    return re.sub(r"\.{2,}", ".", sanitized)


def ipa_filename(
    name: str,
    version: Optional[str] = None,
    commit: Optional[str] = None,
) -> str:
    """Build the canonical IPA file name.

    Parentheses can't survive GitHub's release assets (the API rewrites
    them to dots), so the separator is a dot:

    Stable:      ipa_filename("Nuvio Enhanced", version="0.5.1-beta")
                 → "Nuvio-Enhanced.rel-0.5.1.ipa"
    Pre-release: ipa_filename("Ksign", commit="03a3a9c1234")
                 → "Ksign.pre-03a3a9c.ipa"
    """
    stem = canonical_filename(name)
    if commit:
        return f"{stem}.pre-{commit[:7]}.ipa"
    return f"{stem}.rel-{sanitize_version(version or '1.0')}.ipa"


_IPA_NAME_RE = re.compile(r"^(.*)\.(rel|pre)-(.+)\.ipa$", re.IGNORECASE)


def parse_ipa_filename(filename: str) -> tuple[str, str, str]:
    """Split a versioned IPA file name into (stem, kind, value).

    "Nuvio-Enhanced.rel-0.5.1.ipa" → ("Nuvio-Enhanced", "rel", "0.5.1")
    "Ksign.pre-03a3a9c.ipa"        → ("Ksign", "pre", "03a3a9c")
    "My App.ipa"                   → ("My App", "", "")
    """
    m = _IPA_NAME_RE.match(filename)
    if m:
        return m.group(1), m.group(2).lower(), m.group(3)
    return filename.rsplit(".", 1)[0], "", ""


def canonicalize_ipa_files(ipas_dir: Path) -> None:
    """Rename any .ipa in the folder, swapping spaces for dashes."""
    for p in sorted(ipas_dir.glob("*.ipa")):
        canonical = canonical_filename(p.name)
        if canonical == p.name:
            continue
        target = p.with_name(canonical)
        if target.exists():
            print(f"  ⚠ cannot rename {p.name} — {canonical} already exists")
            continue
        p.rename(target)
        print(f"  ↯ renamed {p.name} → {canonical}")


# ── Config file I/O ──────────────────────────────────────────────────────────

def load_sources() -> list[dict]:
    """Return the sources.json source entries ([] if missing)."""
    if not SOURCES_JSON.exists():
        return []
    with open(SOURCES_JSON, encoding="utf-8") as f:
        return json.load(f).get("sources", [])


def load_current_releases() -> dict:
    """Return current_releases.json ({} if missing)."""
    if not CURRENT_RELEASES_JSON.exists():
        return {}
    with open(CURRENT_RELEASES_JSON, encoding="utf-8") as f:
        return json.load(f)


def save_current_releases(data: dict) -> None:
    """Write current_releases.json."""
    with open(CURRENT_RELEASES_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def load_custom_meta() -> dict:
    """Return the custom-IPA sidecar {filename: {description, subtitle}}.

    The sidecar lives at ipas/.custom_meta.json and is written by
    add_custom_ipa.py.  Path is resolved at call time so tests can
    point IPAS_DIR at a temp directory.
    """
    sidecar = IPAS_DIR / ".custom_meta.json"
    if not sidecar.exists():
        return {}
    with open(sidecar, encoding="utf-8") as f:
        return json.load(f)


# ── GitHub API ───────────────────────────────────────────────────────────────

API_BASE = "https://api.github.com"
UPLOADS_BASE = "https://uploads.github.com"


def github_api(url: str, token: Optional[str] = None) -> dict | list:
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


def download_file(url: str, dest: Path, token: Optional[str] = None) -> None:
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


def find_ipa_asset(release: dict, pattern: Optional[str] = None) -> Optional[dict]:
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


class GitHubRelease:
    """Thin client for the GitHub Releases API (asset level).

    Caches the release object, but invalidates it after every mutation
    so verification steps always read fresh state.  Kept as a class so
    tests can swap in a fake API backend.
    """

    def __init__(self, token: str):
        self.token = token
        self._release: Optional[dict] = None

    def api(
        self,
        url: str,
        data: Optional[bytes] = None,
        method: Optional[str] = None,
        content_type: str = "application/octet-stream",
    ):
        """Call the GitHub API; returns parsed JSON (or None for no body)."""
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "altstore-release-sync",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data is not None:
            headers["Content-Type"] = content_type
        last_err: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                req = urllib.request.Request(
                    url, data=data, headers=headers, method=method
                )
                with urllib.request.urlopen(req, timeout=600) as resp:
                    body = resp.read()
                    return json.loads(body) if body else None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2 * attempt)
        raise last_err

    def get_release(self) -> Optional[dict]:
        """Return the release dict, or None if it doesn't exist yet."""
        if self._release is None:
            url = (
                f"{API_BASE}/repos/{GITHUB_USER}/{GITHUB_REPO}"
                f"/releases/tags/{RELEASE_TAG}"
            )
            try:
                self._release = self.api(url)
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                self._release = None
        return self._release

    def create_release(self) -> dict:
        """Create the ipa-assets release and return it."""
        print(f"Creating release {RELEASE_TAG} …")
        self._release = self.api(
            f"{API_BASE}/repos/{GITHUB_USER}/{GITHUB_REPO}/releases",
            json.dumps(
                {"tag_name": RELEASE_TAG, "name": "IPA Assets"}
            ).encode(),
            content_type="application/json",
        )
        return self._release

    def list_assets(self) -> list[dict]:
        release = self.get_release()
        if not release:
            return []
        return release.get("assets", [])

    def delete_asset(self, asset_id: int) -> None:
        self.api(
            f"{API_BASE}/repos/{GITHUB_USER}/{GITHUB_REPO}"
            f"/releases/assets/{asset_id}",
            method="DELETE",
        )
        self._release = None  # asset list changed

    def upload_asset(self, name: str, data: bytes) -> None:
        """Upload an asset, replacing any existing asset of the same name.

        Same-name matching uses GitHub's sanitization, so a stale asset
        stored under a normalized name is removed first.
        """
        release = self.get_release()
        if not release:
            release = self.create_release()
        for asset in self.list_assets():
            if github_asset_name(asset["name"]) == github_asset_name(name):
                print(f"    replacing existing asset {asset['name']} …")
                self.delete_asset(asset["id"])
                break
        url = (
            f"{UPLOADS_BASE}/repos/{GITHUB_USER}/{GITHUB_REPO}"
            f"/releases/{release['id']}/assets?name={quote(name)}"
        )
        self.api(url, data)
        self._release = None  # asset list changed — force a re-fetch


# ── IPA metadata ─────────────────────────────────────────────────────────────

def find_app_bundle(zf: zipfile.ZipFile) -> Optional[str]:
    """Return the first .app/ directory inside Payload/.

    Standard IPAs have an explicit "Payload/Name.app/" directory entry,
    but some re-zipped IPAs omit directory entries entirely.  Fall back
    to inferring the app dir from its files' paths.
    """
    for name in zf.namelist():
        if name.startswith("Payload/") and name.endswith(".app/"):
            # Should be exactly Payload/Name.app/ (not nested deeper).
            parts = name[len("Payload/"):].rstrip("/").split("/")
            if len(parts) == 1:
                return name

    # No directory entries — derive the app dir from any file inside it.
    app_dirs = set()
    for name in zf.namelist():
        if not name.startswith("Payload/"):
            continue
        head, sep, _ = name[len("Payload/"):].partition("/")
        if sep and head.endswith(".app"):
            app_dirs.add(f"Payload/{head}/")
    return sorted(app_dirs)[0] if app_dirs else None


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


# ── Release checking ─────────────────────────────────────────────────────────

def latest_release_info(
    repo: str, release_type: str, token: Optional[str] = None
) -> Optional[dict]:
    """Return info about a repo's latest release, or None.

    Returns {"tag", "published_at", "release": raw release dict} plus
    "version" (tag minus a leading "v", for stable) or "commit" (7-char
    sha of the tag, for prerelease — tags like "beta" move, shas don't).
    """
    releases = github_api(
        f"{API_BASE}/repos/{repo}/releases?per_page=10", token
    )
    if release_type == "prerelease":
        target = next((r for r in releases if r.get("prerelease")), None)
    else:
        target = next((r for r in releases if not r.get("prerelease")), None)

    if not target:
        return None

    info = {
        "tag": target["tag_name"],
        "published_at": target.get("published_at", ""),
        "release": target,
    }
    if release_type == "prerelease":
        commit = github_api(
            f"{API_BASE}/repos/{repo}/commits/{info['tag']}", token
        )
        info["commit"] = commit["sha"][:7]
    else:
        info["version"] = info["tag"].lstrip("v")
    return info


def check_for_updates(token: Optional[str] = None) -> dict:
    """Compare sources.json against current_releases.json and our release.

    Returns {
      "entries": [{
        "source": the sources.json entry,
        "name", "repo", "release_type",
        "info": latest_release_info result (None on API/selection errors),
        "recorded": previously recorded version/commit (or None),
        "latest": latest version/commit,
        "changed": bool (recorded != latest),
        "expected_filename": versioned file name we'd download,
        "asset_in_release": whether that asset already exists,
      }, …],
      "any_changed": bool,
      "release_assets": {asset name: asset dict} from ipa-assets,
    }
    """
    sources = load_sources()
    recorded = load_current_releases()

    release_assets: dict[str, dict] = {}
    try:
        rel = github_api(
            f"{API_BASE}/repos/{GITHUB_USER}/{GITHUB_REPO}"
            f"/releases/tags/{RELEASE_TAG}",
            token,
        )
        release_assets = {a["name"]: a for a in rel.get("assets", [])}
    except Exception:
        pass  # release doesn't exist yet

    entries = []
    any_changed = False
    for source in sources:
        name = source["name"]
        repo = source["repo"]
        release_type = source.get("release_type", "stable")
        old = recorded.get(name, {})

        entry = {
            "source": source,
            "name": name,
            "repo": repo,
            "release_type": release_type,
            "info": None,
            "recorded": old.get("commit") if release_type == "prerelease"
            else old.get("version"),
            "latest": None,
            "changed": False,
            "expected_filename": "",
            "asset_in_release": False,
        }

        info = None
        try:
            info = latest_release_info(repo, release_type, token)
        except Exception as e:
            print(f"  ✗ {name}: API error: {e}")
        entry["info"] = info
        if info is None:
            entries.append(entry)
            continue

        if release_type == "prerelease":
            entry["latest"] = info["commit"]
            entry["expected_filename"] = ipa_filename(
                name, commit=info["commit"]
            )
        else:
            entry["latest"] = info["version"]
            entry["expected_filename"] = ipa_filename(
                name, version=info["version"]
            )

        entry["changed"] = entry["recorded"] != entry["latest"]
        # Compare with GitHub's name sanitization applied — the server
        # may have stored the asset under a normalized name.
        entry["asset_in_release"] = any(
            github_asset_name(a) == github_asset_name(entry["expected_filename"])
            for a in release_assets
        )
        if entry["changed"]:
            any_changed = True

        entries.append(entry)

    return {
        "entries": entries,
        "any_changed": any_changed,
        "release_assets": release_assets,
    }


def repo_json_references(filename: str) -> bool:
    """Whether any downloadURL in repo.json points at this asset name.

    Names are compared with GitHub's sanitization applied, so a URL
    referencing "Feather(rel-2.9.0).ipa" still counts as referencing
    the stored asset "Feather.rel-2.9.0.ipa".  Used to detect a
    committed repo.json that no longer matches the release.
    """
    if not REPO_JSON.exists():
        return False
    with open(REPO_JSON, encoding="utf-8") as f:
        repo = json.load(f)
    for app in repo.get("apps", []):
        urls = [app.get("downloadURL", "")]
        for v in app.get("versions", []):
            urls.append(v.get("downloadURL", ""))
        for url in urls:
            if RELEASE_TAG not in url:
                continue
            name = unquote(url.rsplit("/", 1)[-1])
            if github_asset_name(name) == github_asset_name(filename):
                return True
    return False


# ── Token handling ───────────────────────────────────────────────────────────

def get_token(flag: Optional[str]) -> Optional[str]:
    """Resolve a GitHub token from a flag, the environment, or a file."""
    token = flag or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    token_file = REPO_ROOT / ".github-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return None
