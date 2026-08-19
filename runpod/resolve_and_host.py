"""Resolve any podcast-ish URL to a direct audio URL hostable for RunPod.

Designed to run on a residential-IP host (so YouTube / Spotify / paywalled
sources don't blanket-block the request) and upload the resulting audio to a
publicly-fetchable location that RunPod's datacenter workers can reach.

Usage:
    python3 runpod/resolve_and_host.py "<input-url>"
    # stdout: https://voice.vgh-usa.com/audio/transcribe-<uuid>.mp3

Pipeline:
    1. Classify input URL (direct media / listennotes / yt-dlp-supported / other)
    2. Direct media → pass through (no fetch needed; RunPod can grab it)
    3. listennotes / lnns.co → scrape the episode page for the publisher's CDN URL
    4. yt-dlp path → download, transcode to mp3, scp to AUDIO_HOST
    5. Print the final public URL on stdout

Required environment variables (set before invoking, e.g. via your shell
profile or a sourced env file):
    AUDIO_HOST_USER       SSH user on the audio-host (e.g. root)
    AUDIO_HOST            SSH host of the audio-host (hostname or IP)
    AUDIO_HOST_DIR        absolute path on the audio-host (e.g. /var/www/audio)
    AUDIO_PUBLIC_PREFIX   public URL prefix that maps to AUDIO_HOST_DIR
                          (e.g. https://audio.example.com/audio)
Optional:
    YTDLP_BIN             default: yt-dlp (prefers ~/.local/bin/yt-dlp if found)

The script will also auto-load $HOME/.vgh.env (a shell-style KEY=value file)
if it exists, so any of the above set there are picked up transparently.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Best-effort loader for a shell-style KEY=value file. Doesn't override
    values already in os.environ (so explicit env vars win)."""
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


_load_env_file(Path.home() / ".vgh.env")


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(
            f"error: required environment variable {name} is not set.\n"
            f"Set it in your shell or in ~/.vgh.env. See script docstring for details.\n"
        )
        sys.exit(2)
    return val


AUDIO_HOST_USER = _require_env("AUDIO_HOST_USER")
AUDIO_HOST = _require_env("AUDIO_HOST")
AUDIO_HOST_DIR = _require_env("AUDIO_HOST_DIR")
AUDIO_PUBLIC_PREFIX = _require_env("AUDIO_PUBLIC_PREFIX")

# Prefer pipx-installed yt-dlp over the (often stale) apt one
_LOCAL_YTDLP = Path.home() / ".local" / "bin" / "yt-dlp"
YTDLP_BIN = os.environ.get(
    "YTDLP_BIN",
    str(_LOCAL_YTDLP) if _LOCAL_YTDLP.exists() else "yt-dlp",
)

# YouTube now requires an external JS runtime + EJS challenge scripts.
# Non-interactive SSH PATH is thin, so resolve absolute paths explicitly.
_JS_RUNTIME_CANDIDATES = (
    ("deno", Path.home() / ".deno" / "bin" / "deno"),
    ("deno", Path("/usr/local/bin/deno")),
    ("deno", Path("/usr/bin/deno")),
    ("node", Path.home() / ".hermes" / "node" / "bin" / "node"),
    ("node", Path.home() / ".local" / "bin" / "node"),
    ("node", Path("/usr/local/bin/node")),
    ("node", Path("/usr/bin/node")),
)

DIRECT_AUDIO_CONTENT_TYPES = ("audio/", "application/octet-stream")
LISTENNOTES_HOSTS = ("lnns.co", "www.listennotes.com", "listennotes.com")
SPOTIFY_HOSTS = ("open.spotify.com", "spotify.link")


def log(msg: str) -> None:
    print(f"[resolve_and_host] {msg}", file=sys.stderr)


def _first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def _resolve_js_runtime() -> tuple[str, Path] | None:
    """Return (runtime_name, absolute_path) for the preferred JS runtime.

    Preference order:
      1. YTDLP_JS_RUNTIME env override ("deno[/path]" or "node[/path]")
      2. deno on PATH / common install locations
      3. node on PATH / common install locations
    """
    override = os.environ.get("YTDLP_JS_RUNTIME", "").strip()
    if override:
        if ":" in override:
            name, raw_path = override.split(":", 1)
            path = Path(raw_path).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return name.strip().lower(), path
        which = shutil.which(override)
        if which:
            return Path(which).name.lower().replace(".exe", ""), Path(which)

    # Explicit candidate paths first (reliable under thin SSH PATH), then PATH.
    by_name: dict[str, list[Path]] = {"deno": [], "node": []}
    for name, path in _JS_RUNTIME_CANDIDATES:
        by_name[name].append(path)
    for name in ("deno", "node"):
        which = shutil.which(name)
        if which:
            by_name[name].append(Path(which))
        found = _first_existing(by_name[name])
        if found:
            return name, found
    return None


def _ytdlp_base_youtube_args() -> list[str]:
    """Flags required for current YouTube media extraction.

    As of 2026, bare yt-dlp without a JS runtime frequently ends at:
      HTTP Error 403: Forbidden
    while selecting android/web formats. Stack that works in production:
      1. JS runtime (deno preferred) + yt-dlp-ejs
      2. bgutil PO-token provider on 127.0.0.1:4416 (plugin auto-discovers)
      3. curl_cffi 0.10–0.15 for browser impersonation
      4. optional cookies-from-browser fallback for stubborn titles
    """
    args: list[str] = []
    runtime = _resolve_js_runtime()
    if runtime is None:
        log(
            "WARNING: no JS runtime found (deno/node). YouTube downloads will "
            "likely 403. Install deno (~/.deno/bin) or ensure node is on PATH."
        )
    else:
        name, path = runtime
        args.extend(["--js-runtimes", f"{name}:{path}"])
        # If the pipx install lacks yt-dlp-ejs, allow runtime fetch as fallback.
        # Harmless when the package is already present.
        if name in {"deno", "bun"}:
            args.extend(["--remote-components", "ejs:npm"])
        else:
            args.extend(["--remote-components", "ejs:github"])
        log(f"using JS runtime {name}:{path}")

    # Prefer clients that cooperate with PO tokens / progressive media.
    player_client = os.environ.get(
        "YTDLP_PLAYER_CLIENT",
        "mweb,web_safari,default",
    ).strip()
    if player_client:
        args.extend(["--extractor-args", f"youtube:player_client={player_client}"])

    # Browser TLS fingerprint when curl_cffi is installed/supported.
    impersonate = os.environ.get("YTDLP_IMPERSONATE", "chrome").strip()
    if impersonate and impersonate.lower() not in {"0", "false", "no", "off"}:
        args.extend(["--impersonate", impersonate])
        log(f"using impersonate target {impersonate}")

    # Optional explicit PO-provider base URL (defaults to 127.0.0.1:4416).
    pot_base = os.environ.get("YTDLP_POT_BASE_URL", "").strip()
    if pot_base:
        args.extend(
            ["--extractor-args", f"youtubepot-bgutilhttp:base_url={pot_base}"]
        )
    return args


def _cookies_from_browser_arg() -> list[str] | None:
    """Optional browser-cookie fallback for stubborn YouTube 403s.

    Opt-in only via YTDLP_COOKIES_FROM_BROWSER=chrome|chromium|brave|firefox|...
    Headless Linux Chrome profiles often store v11 cookies that need a desktop
    keyring key; auto-detecting them just burns a failed attempt ("no key found").
    """
    raw = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if not raw or raw.lower() in {"0", "false", "no", "off", "none"}:
        return None
    return ["--cookies-from-browser", raw]


def _ytdlp_env() -> dict[str, str]:
    env = os.environ.copy()
    extra_paths = [
        str(Path.home() / ".deno" / "bin"),
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / ".hermes" / "node" / "bin"),
    ]
    env["PATH"] = os.pathsep.join(extra_paths + [env.get("PATH", "")])
    return env


def classify(url: str) -> str:
    """Pick a resolution strategy based on the URL shape (cheap heuristic)."""
    parsed = urllib.parse.urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if host in LISTENNOTES_HOSTS:
        return "listennotes"
    if host in SPOTIFY_HOSTS:
        return "spotify"
    if any(path.endswith(ext) for ext in (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus")):
        return "direct"
    return "ytdlp"


def head_content_type(url: str, timeout: int = 10) -> str | None:
    """Best-effort Content-Type detection via HEAD (some hosts reject HEAD; fall back)."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers.get("Content-Type") or ""
    except Exception:
        return None


def is_direct_audio_url(url: str) -> bool:
    ct = head_content_type(url)
    if not ct:
        return False
    return any(ct.lower().startswith(p) for p in DIRECT_AUDIO_CONTENT_TYPES)


def resolve_listennotes(url: str) -> str:
    """Follow lnns.co / listennotes.com redirects to the episode page, then
    scrape for the publisher's audio URL.

    Listennotes embeds the canonical audio URL in a JSON-LD AudioObject
    `contentUrl` field in the episode page HTML.
    """
    log(f"resolving listennotes URL: {url}")
    # Follow redirects to the final HTML page
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 transcribe-resolver/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # 1) Try JSON-LD audio object (most reliable)
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            if t == "PodcastEpisode" or t == "AudioObject":
                for key in ("contentUrl", "url", "audio"):
                    val = item.get(key)
                    if isinstance(val, str) and val.startswith("http"):
                        log(f"  found JSON-LD audio URL: {val}")
                        return val

    # 2) Fallback: scan for any *.mp3 in the page (loose)
    m = re.search(r'https?://[^\s"\'<>]+\.(?:mp3|m4a|wav|ogg)', html)
    if m:
        log(f"  found audio URL via mp3-scan: {m.group(0)}")
        return m.group(0)

    raise RuntimeError(
        "Could not extract a direct audio URL from the listennotes page. "
        "The episode may be hosted somewhere the scraper doesn't know about. "
        "Try the underlying podcast publisher's URL directly."
    )


def _fuzzy_match(target: str, candidates: list[str]) -> int | None:
    """Return the index of the candidate that best matches target.

    Uses normalized substring matching + word-overlap ratio. Returns None if
    nothing scores above the threshold.
    """
    def norm(s: str) -> set[str]:
        return {w.lower() for w in re.findall(r"\w+", s or "") if len(w) > 2}

    target_words = norm(target)
    if not target_words:
        return None
    best_idx, best_score = None, 0.0
    for i, cand in enumerate(candidates):
        cand_words = norm(cand)
        if not cand_words:
            continue
        overlap = len(target_words & cand_words)
        score = overlap / max(len(target_words), 1)
        if score > best_score:
            best_idx, best_score = i, score
    return best_idx if best_score >= 0.5 else None


def resolve_spotify(url: str) -> str:
    """Resolve open.spotify.com episode URL to the publisher's CDN MP3.

    Spotify DRMs their audio, so we can't fetch it directly. But almost every
    podcast on Spotify also has a public RSS feed that publishes the same
    episode. Pipeline:

      1. Spotify oEmbed → episode title + show author
      2. iTunes Search API → RSS feed URL for the show
      3. Fetch RSS feed → find matching episode → return enclosure URL

    Spotify-exclusive shows (e.g. Joe Rogan during the exclusivity window)
    have no public feed and will fail here. That's a real Spotify limitation,
    not a bug in the resolver.
    """
    log(f"resolving Spotify URL: {url}")

    # Step 1: oEmbed
    oembed_url = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url)
    try:
        with urllib.request.urlopen(oembed_url, timeout=15) as resp:
            oembed = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        raise RuntimeError(f"Spotify oEmbed lookup failed: {e}")
    episode_title = oembed.get("title") or ""
    show_author = oembed.get("author_name") or oembed.get("provider_name") or ""
    log(f"  oEmbed: title={episode_title!r}, author={show_author!r}")
    if not episode_title:
        raise RuntimeError("Spotify oEmbed returned no title — can't look up the episode")

    # Step 2: iTunes Search to find the show's RSS feed
    # Many Spotify episode titles include the show name as a prefix or suffix.
    # We search using the show author (often the show name) first; fall back
    # to the episode title.
    search_term = show_author or episode_title
    itunes_url = (
        "https://itunes.apple.com/search?term="
        + urllib.parse.quote(search_term)
        + "&entity=podcast&limit=10&country=US"
    )
    log(f"  iTunes Search: {search_term!r}")
    try:
        with urllib.request.urlopen(itunes_url, timeout=15) as resp:
            itunes = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        raise RuntimeError(f"iTunes Search API failed: {e}")
    results = itunes.get("results") or []
    if not results:
        raise RuntimeError(
            f"iTunes returned no podcast matches for {search_term!r}. "
            "The show may be Spotify-exclusive."
        )
    # Pick the first result with a feedUrl (usually the right one)
    feed_url = None
    for r in results:
        if r.get("feedUrl"):
            feed_url = r["feedUrl"]
            log(f"  matched show: {r.get('collectionName')!r} → {feed_url}")
            break
    if not feed_url:
        raise RuntimeError("iTunes returned matches but none had a feedUrl")

    # Step 3: Fetch RSS feed and find the matching episode
    req = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "Mozilla/5.0 transcribe-resolver/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rss = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"Failed to fetch RSS feed {feed_url}: {e}")

    # Parse <item> entries. Don't bring in feedparser — the regex is enough
    # for the title + enclosure URL we need.
    items = re.findall(r"<item\b[^>]*>(.*?)</item>", rss, flags=re.DOTALL | re.IGNORECASE)
    log(f"  RSS feed has {len(items)} items")
    titles = []
    enclosures = []
    for item in items:
        title_m = re.search(
            r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
            item, flags=re.DOTALL | re.IGNORECASE,
        )
        enc_m = re.search(r'<enclosure[^>]*\burl=["\']([^"\']+)["\']', item, flags=re.IGNORECASE)
        titles.append(title_m.group(1).strip() if title_m else "")
        enclosures.append(enc_m.group(1).strip() if enc_m else "")

    idx = _fuzzy_match(episode_title, titles)
    if idx is None:
        log(f"  no fuzzy match for episode title — trying first item with audio")
        for i, enc in enumerate(enclosures):
            if enc:
                idx = i
                log(f"  using first audio item: {titles[i]!r}")
                break
    if idx is None or not enclosures[idx]:
        raise RuntimeError(
            f"Couldn't find episode in RSS feed. Searched for: {episode_title!r}\n"
            f"Available titles (first 5): {titles[:5]}"
        )

    log(f"  matched episode: {titles[idx]!r}")
    log(f"  enclosure URL: {enclosures[idx]}")
    return enclosures[idx]


def _find_ytdlp_audio(out_dir: Path) -> Path | None:
    for cand in out_dir.glob("audio.*"):
        if cand.suffix.lower() in (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac"):
            return cand
    return None


def download_with_ytdlp(url: str, out_dir: Path) -> Path:
    """Run yt-dlp to fetch the best audio stream and convert to mp3.

    Attempt order:
      1. JS runtime + EJS + PO provider + impersonation (no cookies)
      2. Same stack + browser cookies (if a local profile exists / env set)
    """
    log(f"yt-dlp downloading: {url}")
    out_template = str(out_dir / "audio.%(ext)s")
    base = [
        YTDLP_BIN,
        *_ytdlp_base_youtube_args(),
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",  # best quality
        "--no-playlist",
        "-o", out_template,
    ]
    env = _ytdlp_env()

    attempts: list[tuple[str, list[str]]] = [("no-cookies", base + [url])]
    cookies = _cookies_from_browser_arg()
    if cookies:
        attempts.append(("cookies-from-browser", base + cookies + [url]))

    last_err: Exception | None = None
    for label, cmd in attempts:
        # Clean partials from a prior failed attempt in the same temp dir.
        for leftover in out_dir.glob("audio.*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        log(f"  attempt={label} running: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, env=env)
        except subprocess.CalledProcessError as exc:
            last_err = exc
            log(f"  attempt={label} failed: {exc}")
            continue
        found = _find_ytdlp_audio(out_dir)
        if found:
            return found
        last_err = RuntimeError(f"yt-dlp succeeded but no audio file found in {out_dir}")
        log(f"  attempt={label}: {last_err}")

    if last_err is not None:
        raise last_err
    raise RuntimeError(f"yt-dlp failed with no attempts for {url}")


def download_direct(url: str, out_dir: Path) -> Path:
    """Download a direct media URL via curl (handles redirects, large files).

    Uses --fail so HTTP errors (404/500/etc.) exit non-zero instead of silently
    producing a 0-byte file. Adds a post-download size sanity check to catch
    edge cases where curl exits 0 but the response is too small to be real
    audio (e.g. servers that 200 with an HTML error page).
    """
    log(f"downloading direct media: {url}")
    suffix = Path(urllib.parse.urlparse(url).path).suffix or ".mp3"
    dest = out_dir / f"audio{suffix}"
    result = subprocess.run(
        ["curl", "--fail", "--show-error", "-sSL", "--retry", "3",
         "--max-time", "600", "-o", str(dest), url],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"curl exited {result.returncode}: {result.stderr.strip()[:300]}"
        )
    size = dest.stat().st_size if dest.exists() else 0
    if size < 1024:
        # Either the URL 404'd and curl wrote nothing, or the response was an
        # HTML error page that snuck past --fail (e.g. some CDNs 200 with
        # text/html on missing assets).
        raise RuntimeError(
            f"Downloaded file is too small ({size} bytes) to be real audio. "
            f"The episode may be unavailable at the publisher's CDN, or the "
            f"resolver picked up a stale URL.\nURL: {url}"
        )
    log(f"  downloaded {size} bytes")
    return dest


def upload(local_path: Path) -> str:
    """scp the local audio file to AUDIO_HOST and return its public URL."""
    remote_name = f"transcribe-{uuid.uuid4().hex[:12]}{local_path.suffix.lower()}"
    remote_target = f"{AUDIO_HOST_USER}@{AUDIO_HOST}:{AUDIO_HOST_DIR}/{remote_name}"
    log(f"scp {local_path} -> {remote_target}")
    subprocess.run(
        ["scp", "-q", "-o", "ConnectTimeout=10", str(local_path), remote_target],
        check=True,
    )
    public_url = f"{AUDIO_PUBLIC_PREFIX}/{remote_name}"
    return public_url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Input URL (Spotify, YouTube, podcast page, lnns.co, direct mp3, etc.)")
    parser.add_argument(
        "--passthrough-direct",
        action="store_true",
        help="If the URL is already direct audio, print it unchanged instead of mirroring through the audio host.",
    )
    args = parser.parse_args()

    url = args.url.strip()
    strategy = classify(url)

    # If classify says direct, double-check via HEAD before short-circuiting.
    if strategy == "direct" or is_direct_audio_url(url):
        if args.passthrough_direct:
            log(f"direct audio URL; printing unchanged")
            print(url)
            return 0
        log("direct audio URL; mirroring through audio host for stable serving")
        strategy = "direct"

    with tempfile.TemporaryDirectory(prefix="transcribe-resolve-") as tmpdir:
        out_dir = Path(tmpdir)
        if strategy == "listennotes":
            resolved = resolve_listennotes(url)
            local_path = download_direct(resolved, out_dir)
        elif strategy == "spotify":
            resolved = resolve_spotify(url)
            local_path = download_direct(resolved, out_dir)
        elif strategy == "direct":
            local_path = download_direct(url, out_dir)
        else:
            local_path = download_with_ytdlp(url, out_dir)

        public_url = upload(local_path)
        print(public_url)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        log(f"subprocess failed: {e}")
        sys.exit(1)
    except Exception as e:
        log(f"error: {type(e).__name__}: {e}")
        sys.exit(2)
