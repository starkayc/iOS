#!/usr/bin/env python3
"""
Everything that opens an IPA (a zip archive with Payload/*.app/ inside).

Kept apart from altstore_lib: that module owns the paths, the naming rules
and the GitHub API, this one owns bytes-in-a-zip.  Nothing here reaches the
network, and the one thing imported from altstore_lib is its file-name
rule (altstore_lib never imports this module, so the two can't cycle).

``ipa_problem`` is the single gate for "is this a usable IPA".  Every IPA
that enters the repo goes through it before anything else touches it.
"""

import os
import plistlib
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Optional

import cli_common as diag

from altstore_lib import safe_stem


def ipa_problem(path: Path) -> str:
    """Say why ``path`` is not a usable IPA, or "" when it is one.

    An IPA is a zip archive holding ``Payload/*.app/``.  The check covers
    the magic bytes, the archive index, the bundle and its Info.plist,
    because every entry the pipeline publishes is built from that plist.
    """
    if not path.exists():
        return "file is missing"
    with open(path, "rb") as f:
        head = f.read(16)
    if head[:4] != b"PK\x03\x04":
        return (f"not a zip archive: starts with {head[:8].hex(' ')} "
                f"({head[:8]!r})")
    try:
        with zipfile.ZipFile(path) as zf:
            app_dir = find_app_bundle(zf)
            if app_dir is None:
                return "zip has no Payload/*.app bundle — not an IPA"
            if extract_info_plist(zf, app_dir) is None:
                return "app bundle has no readable Info.plist"
            bad = zf.testzip()
            if bad:
                return f"corrupt entry in zip: {bad}"
    except zipfile.BadZipFile as ex:
        return f"broken zip (truncated or damaged): {ex}"
    except OSError as ex:
        return f"unreadable: {type(ex).__name__}: {ex}"
    return ""



def find_app_bundle(zf: zipfile.ZipFile) -> Optional[str]:
    """Return the first .app/ directory inside Payload/.

    Standard IPAs have an explicit "Payload/Name.app/" directory entry,
    but some re-zipped IPAs omit directory entries entirely.  Fall back
    to inferring the app dir from its files' paths.
    """
    for name in zf.namelist():
        if name.startswith("Payload/") and name.endswith(".app/"):
            parts = name[len("Payload/"):].rstrip("/").split("/")
            if len(parts) == 1:
                return name

    app_dirs = set()
    for name in zf.namelist():
        if not name.startswith("Payload/"):
            continue
        head, sep, _ = name[len("Payload/"):].partition("/")
        if sep and head.endswith(".app"):
            app_dirs.add(f"Payload/{head}/")
    return sorted(app_dirs)[0] if app_dirs else None


def extract_info_plist(zf: zipfile.ZipFile, app_dir: str) -> Optional[dict]:
    """Read and parse the app bundle's Info.plist, or None when unusable.

    A malformed plist raises out of plistlib.  This is the one place that
    decides a plist is unreadable, and callers read None the same way as a
    missing plist.
    """
    plist_path = app_dir + "Info.plist"
    if plist_path not in zf.namelist():
        return None
    try:
        with zf.open(plist_path) as f:
            return plistlib.load(f)
    except Exception:
        return None


def find_icon_files(info: dict, zf: zipfile.ZipFile, app_dir: str) -> list[str]:
    """Return the icon file paths inside the IPA, smallest file first."""
    candidates: set[str] = set()

    bundle_icons = info.get("CFBundleIcons", {})
    primary = bundle_icons.get("CFBundlePrimaryIcon", {})
    for name in primary.get("CFBundleIconFiles", []):
        candidates.add(name)

    for name in info.get("CFBundleIconFiles", []):
        candidates.add(name)

    singular = info.get("CFBundleIconFile")
    if singular:
        candidates.add(singular)

    all_files = {n for n in zf.namelist() if n.startswith(app_dir)}

    found: list[tuple[int, str]] = []

    for icon_name in candidates:
        base = os.path.splitext(icon_name)[0]
        for full_path in all_files:
            fname = os.path.basename(full_path)
            fbase = os.path.splitext(fname)[0]
            if fbase == icon_name or fbase == base or fbase.startswith(base):
                if full_path.lower().endswith((".png", ".jpg", ".jpeg")):
                    info_entry = zf.getinfo(full_path)
                    found.append((info_entry.file_size, full_path))

    if not found:
        for full_path in all_files:
            fname = os.path.basename(full_path).lower()
            if any(
                keyword in fname
                for keyword in ("appicon", "icon", "app_icon")
            ):
                if full_path.lower().endswith((".png", ".jpg", ".jpeg")):
                    info_entry = zf.getinfo(full_path)
                    found.append((info_entry.file_size, full_path))

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
                diag.warn("No .app bundle found in Payload/")
                return None

            info = extract_info_plist(zf, app_dir)
            if not info:
                diag.warn("No readable Info.plist found")
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
        diag.error(f"Failed to read IPA: {e}")
        return None


def patch_bundle_id(ipa_path: Path, new_bundle_id: str) -> bool:
    """Rewrite CFBundleIdentifier in the IPA's Info.plist.

    AltStore re-signs apps on install, so a second bundle ID lets two
    builds of the same app coexist in one source (e.g. "Nuvio" and
    "Nuvio Enhanced").  The plist keeps its original format and the zip is
    rebuilt in place.  Running it a second time changes nothing.
    """
    with zipfile.ZipFile(ipa_path, "r") as zf:
        app_dir = find_app_bundle(zf)
        if not app_dir:
            diag.warn(f"Cannot patch bundle ID — no .app bundle in {ipa_path.name}")
            return False
        plist_path = app_dir + "Info.plist"
        if plist_path not in zf.namelist():
            diag.warn(f"Cannot patch bundle ID — no Info.plist in {ipa_path.name}")
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

    # Rebuild the zip with the patched plist.  The new archive goes to a
    # temp file first, because Windows can't replace a file that still has
    # a handle open.
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
        return data

    chunks: list[tuple[bytes, bytes]] = []
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
        return data

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
    bits_per_pixel = samples_per_pixel * bit_depth
    bpp = max(1, (bits_per_pixel + 7) // 8)

    idat_data = b"".join(
        cdata for ctype, cdata in chunks if ctype == b"IDAT"
    )
    try:
        raw = zlib.decompress(idat_data, -15)  # raw deflate
    except zlib.error:
        return data

    # The row length is the real pixel bytes per row.  For an indexed or
    # grey image under 8 bits deep, that is not width * bpp.
    stride = (width * bits_per_pixel + 7) // 8 + 1
    prev_row: Optional[bytearray] = None
    rows: list[bytearray] = []

    for row_idx in range(height):
        start = row_idx * stride
        if start + stride > len(raw):
            break
        filt = raw[start]
        row = bytearray(raw[start + 1 : start + stride])

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

        # Swap R ↔ B for RGB / RGBA images (Apple stores as BGRA).  Only
        # the 8-bit-per-channel case is a plain byte swap.
        if color_type in (2, 6) and bit_depth == 8:
            for p in range(0, len(row) - bpp + 1, bpp):
                row[p], row[p + 2] = row[p + 2], row[p]

        rows.append(row)
        prev_row = row

    # For indexed images, swap red and blue in the PLTE palette instead.
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

    re_encoded = b"".join(b"\x00" + bytes(r) for r in rows)
    new_idat = zlib.compress(re_encoded)

    def _write_chunk(buf: bytearray, ctype: bytes, cdata: bytes) -> None:
        buf.extend(struct.pack(">I", len(cdata)))
        buf.extend(ctype)
        buf.extend(cdata)
        crc = zlib.crc32(ctype + cdata) & 0xFFFFFFFF
        buf.extend(struct.pack(">I", crc))

    out = bytearray(b"\x89PNG\r\n\x1a\n")
    for ctype, cdata in chunks:
        if ctype in (b"CgBI", b"iDOT", b"IDAT", b"IEND"):
            continue
        _write_chunk(out, ctype, cdata)
    _write_chunk(out, b"IDAT", new_idat)
    _write_chunk(out, b"IEND", b"")

    return bytes(out)


def extract_icon(
    ipa_path: Path,
    icon_paths: list[str],
    bundle_id: str,
    icons_dir: Path,
) -> Optional[str]:
    """Extract the largest icon from the IPA, uncrush it if needed, and
    save it to icons/.

    Returns the output filename (e.g. 'com.example.app.png') on success,
    or None if no suitable icon was found.
    """
    if not icon_paths:
        print(f"    No icon candidates found")
        return None

    best = icon_paths[-1]
    ext = os.path.splitext(best)[1] or ".png"
    # The bundle ID is an untrusted Info.plist field that reaches the file
    # system here, so it goes through the shared file-name rule.
    output_name = f"{safe_stem(bundle_id)}{ext}"
    output_path = icons_dir / output_name

    try:
        with zipfile.ZipFile(ipa_path, "r") as zf:
            raw = zf.read(best)

        uncrushed = _uncrush_png(raw)
        output_path.write_bytes(uncrushed)

        size_kb = output_path.stat().st_size // 1024
        tag = " (uncrushed)" if uncrushed != raw else ""
        print(f"    ✓ Icon extracted → icons/{output_name} ({size_kb} KB){tag}")
        return output_name
    except Exception as e:
        diag.error(f"Failed to extract icon: {e}")
        return None
