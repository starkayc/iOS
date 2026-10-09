#!/usr/bin/env python3
"""
Shared library for the AltStore source pipeline.

Every script imports this module as ``import altstore_lib as lib`` and
reaches the constants through the module object, so a test can point the
whole pipeline at a temp directory by patching ``lib``.

Sections:
  - Paths and source-level metadata
  - File naming: canonical names, versioned IPA file names
  - Config file I/O: sources.json, current_releases.json, custom meta
  - GitHub HTTP: github_api, download_file, request_bytes, _auth
  - Release checking: latest_release_info, check_for_updates

The release-asset side, which finds, fetches and publishes the IPAs, lives
in release.py.  Anything that opens an IPA lives in ipa.py.
"""

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

import cli_common as diag


REPO_ROOT = Path(__file__).resolve().parent.parent
IPAS_DIR = REPO_ROOT / "ipas"
ICONS_DIR = REPO_ROOT / "icons"
REPO_JSON = REPO_ROOT / "repo.json"
SOURCES_JSON = REPO_ROOT / "sources.json"
CURRENT_RELEASES_JSON = REPO_ROOT / "current_releases.json"

SOURCE_NAME = "Star's Repository"
SOURCE_IDENTIFIER = "moe.starkayc.repo"
SOURCE_SUBTITLE = "Personal AltStore source"
SOURCE_DESCRIPTION = "Personal AltStore source for IPA distribution."

GITHUB_USER = "starkayc"
GITHUB_REPO = "iOS"
PAGES_BASE = f"https://{GITHUB_USER}.github.io/{GITHUB_REPO}"

RELEASE_TAG = "ipa-assets"
RELEASE_BASE = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}/releases/download/{RELEASE_TAG}"

SOURCE_ICON_URL = f"https://github.com/{GITHUB_USER}.png"
SOURCE_WEBSITE = "https://github.com/starkayc/iOS"
SOURCE_TINT_COLOR = "3c94fc"


def parse_version_tuple(version: str) -> tuple:
    """Parse a version string into a comparable tuple of ints.

    >>> parse_version_tuple("1.6") > parse_version_tuple("1.5.2")
    True

    Non-numeric segments are treated as 0, so a trailing "-beta" or "-rc"
    suffix does not change the tuple.  This is a rough ordering helper for
    picking the newer of two IPAs; it does not try to order pre-releases.
    """
    parts = []
    for segment in version.split("."):
        m = re.match(r"(\d+)", segment)
        if m:
            parts.append(int(m.group(1)))
        else:
            parts.append(0)
    return tuple(parts)


def url_encode_path(path: str) -> str:
    """Percent-encode a URL path component.

    Slashes separate path segments and parentheses are legal in URLs
    (our versioned file names use them, e.g. "Feather(rel-2.10.0).ipa").
    Everything else gets percent-encoded.
    """
    return quote(path, safe="/()")


def canonical_filename(name: str) -> str:
    """Return the name both the local file and the stored asset carry.

    GitHub's release API rewrites characters it does not allow, so a
    repo.json downloadURL built from the on-disk name can stop matching
    the stored asset.  Applying the same rule locally makes the file, the
    uploaded asset and the URL match.  A space becomes a dash first,
    because GitHub leaves dashes unchanged but rewrites spaces to dots.
    ``canonicalize_ipa_files`` renames a hand-dropped file to this form on
    disk, so "My App(1).ipa" becomes "My-App.1.ipa".
    """
    return github_asset_name(name.replace(" ", "-"))


def safe_stem(name: str) -> str:
    """A file-name stem that can't escape its directory.

    A bundle ID from an IPA's Info.plist and a --name or --version from a
    custom upload both reach the file system, and both come from outside.
    Anything outside [A-Za-z0-9._-], path separators above all, becomes an
    underscore.  A leading dot is dropped.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")
    return cleaned or "app"


def sanitize_version(version: str) -> str:
    """Clean a release tag for use in file names.

    Drops a leading "release" prefix and a trailing pre-release suffix:
    "release-1.4.1" becomes "1.4.1", "0.5.1-beta" becomes "0.5.1",
    "2.0.0-rc.2" becomes "2.0.0", and "1.0" stays as it is.
    current_releases.json keeps the raw tag, so a move from beta to stable
    still counts as a change.  File names use this sanitized form.
    """
    stripped = re.sub(r"^release[-_.]?", "", version, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"[-_.]?(alpha|beta|rc|preview|pre)[-_.]?\d*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    ).strip("- . _")
    return cleaned or version


def github_asset_name(name: str) -> str:
    """Mirror GitHub's release-asset name sanitization.

    The GitHub upload API, and the web UI, rewrite characters that are not
    allowed in an asset name.  Each unsafe character becomes a dot, and runs
    of dots collapse.  Observed: a space becomes a dot, "(" becomes a dot
    and ")" disappears, so "Feather(rel-2.9.0).ipa" is stored as
    "Feather.rel-2.9.0.ipa".  The download endpoint matches names exactly,
    so every comparison between a local file name and a server asset name
    goes through this function.
    """
    sanitized = re.sub(r"[^A-Za-z0-9._-]", ".", name)
    return re.sub(r"\.{2,}", ".", sanitized)


def ipa_stem(name: str) -> str:
    """The versioned file name's stem for a source app name."""
    return safe_stem(canonical_filename(name))


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
    stem = ipa_stem(name)
    if commit:
        return f"{stem}.pre-{commit[:7]}.ipa"
    return f"{stem}.rel-{safe_stem(sanitize_version(version or '1.0'))}.ipa"


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
            diag.warn(f"cannot rename {p.name} — {canonical} already exists")
            continue
        p.rename(target)
        print(f"  ↯ renamed {p.name} → {canonical}")


def _load_json(path: Path, default):
    """Parse a JSON state file, returning ``default`` when it is missing."""
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_sources() -> list[dict]:
    """Return the sources.json source entries ([] if missing)."""
    return _load_json(SOURCES_JSON, {}).get("sources", [])


def load_current_releases() -> dict:
    """Return current_releases.json ({} if missing)."""
    return _load_json(CURRENT_RELEASES_JSON, {})


def write_json(path: Path, data) -> None:
    """Write a JSON state file atomically.

    The data goes to ``<path>.part`` and is renamed into place, so a crash,
    a kill or a timeout mid-write leaves the previous file intact.  The
    workflows commit these files, including on a failed run.
    """
    tmp = Path(str(path) + ".part")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_current_releases(data: dict) -> None:
    """Write current_releases.json."""
    write_json(CURRENT_RELEASES_JSON, data)


def load_custom_meta() -> dict:
    """Return the custom-IPA sidecar {filename: {description, subtitle}}.

    The sidecar lives at ipas/.custom_meta.json and is written by
    add_custom_ipa.py.  The path resolves at call time, so a test can point
    IPAS_DIR at a temp directory.
    """
    return _load_json(IPAS_DIR / ".custom_meta.json", {})


API_BASE = "https://api.github.com"
UPLOADS_BASE = "https://uploads.github.com"


class GitHubError(RuntimeError):
    """A failed GitHub request, with the HTTP status and response body.

    ``status`` holds the HTTP status code, or None for a network-level
    error, and ``body`` holds the response body, cut to 500 characters.
    GitHub puts the real reason in that body, so the error message keeps
    it.
    """

    def __init__(self, message: str, status: Optional[int] = None,
                 body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _content_length(resp) -> Optional[int]:
    """The response's Content-Length as an int, or None if absent/unusable."""
    try:
        return int(resp.headers.get("Content-Length"))
    except (AttributeError, TypeError, ValueError):
        return None


def _stream_to(resp, dest: Path) -> int:
    """Write a response body to ``dest``; return how many bytes arrived."""
    written = 0
    with open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    return written


class _Retryable(Exception):
    """The op wants the same request tried again (a body that came up short)."""


def _http_error(url: str, e: urllib.error.HTTPError) -> GitHubError:
    """A GitHubError built from an HTTPError, with GitHub's reason attached."""
    body = ""
    try:
        body = e.read().decode("utf-8", "replace").strip()
    except Exception:
        pass
    detail = body[:500]
    message = f"HTTP {e.code} {e.reason} for {url}"
    if detail:
        message += f" — {detail}"
    return GitHubError(message, status=e.code, body=detail)


def _retry(op, url: str, retries: int = 3):
    """Run ``op()`` until it returns, retrying only transient failures.

    A client error (4xx) is raised at once with GitHub's response body
    attached, because retrying it wastes time and hides the real message.
    A server error (5xx), a network or timeout error, an incomplete body
    (``http.client.HTTPException``, meaning a response that ends before its
    Content-Length) and a ``_Retryable`` raised by the op are all retried
    with a linear backoff.
    """
    last_err: Optional[GitHubError] = None
    for attempt in range(1, retries + 1):
        try:
            return op()
        except urllib.error.HTTPError as e:
            # HTTPError is a subclass of URLError, so handle it first.
            err = _http_error(url, e)
            if 400 <= e.code < 500:
                raise err
            last_err = err
        except _Retryable as e:
            last_err = GitHubError(f"{e} for {url}")
        except (urllib.error.URLError, http.client.HTTPException,
                TimeoutError, ConnectionError, OSError) as e:
            last_err = GitHubError(f"{type(e).__name__}: {e} for {url}")
        if attempt < retries:
            delay = 2 * attempt
            diag.debug(
                f"retrying {url} in {delay}s "
                f"(attempt {attempt}/{retries}): {last_err}"
            )
            time.sleep(delay)
    raise last_err or GitHubError(f"no response from {url}")


def request_bytes(
    url: str,
    headers: dict,
    data: Optional[bytes] = None,
    method: Optional[str] = None,
    timeout: int = 30,
) -> Optional[bytes]:
    """Send a request and return its body (retries transient failures)."""

    def op() -> bytes:
        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return _retry(op, url)


def _auth(token: Optional[str], url: str) -> dict:
    """The Authorization header for a GitHub request, or nothing.

    This function owns the only rule about whether the token may be sent,
    and the token never goes to a non-GitHub host.  Download URLs can come
    from a user, as add_custom_ipa's --url does, and the token grants write
    access to this repo, so the check lives here instead of at each call
    site.  ``url`` is required, so no call site can leave the check out.
    ``_SafeRedirect`` applies the same rule to every later hop.
    """
    if not token or not _is_github_host(url):
        return {}
    return {"Authorization": f"Bearer {token}"}


def _is_github_host(url: str) -> bool:
    """Whether ``url`` points at GitHub (asset download hosts included)."""
    host = urlsplit(url).hostname or ""
    return host == "github.com" or host.endswith(
        (".github.com", ".githubusercontent.com")
    )


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but drop the token when one leaves GitHub.

    urllib copies every request header except Content-Length/Content-Type
    into the redirected request, so a 302 to a foreign host would re-send
    the Authorization header that ``_auth`` only vetted for the first hop.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None and not _is_github_host(newurl):
            for hdrs in (new_req.headers, new_req.unredirected_hdrs):
                for key in [k for k in hdrs if k.lower() == "authorization"]:
                    del hdrs[key]
        return new_req


# urlopen() goes through the module-level opener, so installing ours here
# is what makes the rule above apply to every request in the process.
urllib.request.install_opener(urllib.request.build_opener(_SafeRedirect))


def github_api(url: str, token: Optional[str] = None):
    """Call the GitHub API and return parsed JSON (retries transient errors)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "altstore-source-generator",
        **_auth(token, url),
    }
    diag.debug(f"GET {url}")
    raw = request_bytes(url, headers, timeout=30)
    return json.loads(raw) if raw else None


def download_file(
    url: str,
    dest: Path,
    token: Optional[str] = None,
    expected_size: Optional[int] = None,
) -> None:
    """Download a file to disk, retrying transient network errors.

    The bytes stream to a ``<dest>.part`` file, which is renamed on success,
    so an interrupted download cannot leave a half-written file behind.
    That matters for the ~200 MB IPAs in the source.  A body that does not
    match ``expected_size``, or the response's Content-Length, came up short
    and is retried like any other dropped connection.
    """
    headers = {"Accept": "application/octet-stream", **_auth(token, url)}

    def op() -> None:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            part = dest.with_name(dest.name + ".part")
            try:
                written = _stream_to(resp, part)
                want = expected_size
                if want is None:
                    want = _content_length(resp)
                if want is not None and written != want:
                    raise _Retryable(
                        f"short download: got {written:,} of {want:,} bytes"
                    )
                part.replace(dest)
            except Exception:
                part.unlink(missing_ok=True)
                raise

    diag.debug(f"downloading {url} → {dest.name}")
    _retry(op, url)



def version_from_tag(tag: str) -> str:
    """A release tag as a version string, minus its "v" prefix.

    Only a leading "v" followed by a digit is dropped, so "version-2.0"
    keeps its name (str.lstrip("v") would eat into it).
    """
    return re.sub(r"^v(?=\d)", "", tag)


def latest_release_info(
    repo: str, release_type: str, token: Optional[str] = None
) -> Optional[dict]:
    """Return info about a repo's latest release, or None.

    The dict holds "tag", "published_at" and "release", the raw release
    dict, plus either "version" for a stable release, which is the tag
    without a leading "v", or "commit" for a prerelease, which is the
    first 7 characters of the sha of the tag.  A tag like "beta" moves and
    a sha does not, so prereleases track the sha.
    """
    releases = github_api(
        f"{API_BASE}/repos/{repo}/releases?per_page=100", token
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
        info["version"] = version_from_tag(info["tag"])
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
        release_assets = {a["name"]: a for a in (rel or {}).get("assets", [])}
    except GitHubError as e:
        if e.status == 404:
            diag.debug(f"release {RELEASE_TAG} does not exist yet")
        else:
            diag.warn(
                f"could not read the {RELEASE_TAG} release — assuming it has "
                f"no assets: {e}"
            )
    except Exception as e:
        diag.warn(f"could not read the {RELEASE_TAG} release: {e}")

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
            diag.error(f"{name}: API error: {e}")
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


def repo_json_asset_names() -> set[str]:
    """Asset names the downloadURLs in repo.json point at.

    Only URLs under the ipa-assets release count.  A hand-added external
    link is not ours to sync or delete.
    """
    if not REPO_JSON.exists():
        return set()
    with open(REPO_JSON, encoding="utf-8") as f:
        repo = json.load(f)
    names: set[str] = set()
    for app in repo.get("apps", []):
        urls = [app.get("downloadURL", "")]
        for v in app.get("versions", []):
            urls.append(v.get("downloadURL", ""))
        for url in urls:
            if RELEASE_TAG in url:
                names.add(unquote(url.rsplit("/", 1)[-1]))
    return names


def repo_json_references(
    filename: str, asset_names: Optional[set[str]] = None
) -> bool:
    """Whether any downloadURL in repo.json points at this asset name.

    Names are compared with GitHub's sanitization applied, so a URL that
    references "Feather(rel-2.9.0).ipa" still counts as a reference to the
    stored asset "Feather.rel-2.9.0.ipa".  Pass ``asset_names`` from
    repo_json_asset_names when checking many apps, so repo.json is read
    once for the whole run instead of once per app.
    """
    if asset_names is None:
        asset_names = repo_json_asset_names()
    san = github_asset_name(filename)
    return any(github_asset_name(name) == san for name in asset_names)


def get_token(flag: Optional[str]) -> Optional[str]:
    """Resolve a GitHub token from a flag, the environment, or a file."""
    token = flag or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    token_file = REPO_ROOT / ".github-token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return None
