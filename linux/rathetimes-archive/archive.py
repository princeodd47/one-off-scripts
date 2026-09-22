#!/usr/bin/env python3
"""Archive every article on rathetimes.com as HTML + PDF.

Crawls the paginated article listing (?page=N) to discover article slugs,
downloads each article's HTML, then renders each saved HTML file to PDF
using headless Chrome. Safe to re-run: existing files are skipped, so an
interrupted run just picks up where it left off.

Usage:
    python3 archive.py                 # full run: discover, download, convert
    python3 archive.py --limit 2       # only the 2 most recent articles (test run)
    python3 archive.py --skip-pdf      # only download HTML
    python3 archive.py --skip-html     # only convert already-downloaded HTML
    python3 archive.py --override      # re-download/re-render even if files already exist

Requires: google-chrome (or set CHROME_BIN) for the PDF step. No third-party
Python packages needed.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://rathetimes.com"
USER_AGENT = "Mozilla/5.0 (rathetimes-archive/1.0; personal archival script)"
REQUEST_DELAY_SECONDS = 0.75

SCRIPT_DIR = Path(__file__).resolve().parent
HTML_DIR = SCRIPT_DIR / "html"
PDF_DIR = SCRIPT_DIR / "pdf"

ARTICLE_HREF_RE = re.compile(r'href="/articles/([a-z0-9\-]+)"')

# The site's header contains an Alpine.js mobile-nav drawer that is
# position:fixed and only gets hidden once client-side JS runs. Headless
# Chrome's one-shot print doesn't reliably run that JS in time, so the drawer
# renders as a full-page overlay on every printed page. It also has a large
# tiled SVG background pattern that Chrome rasterizes at full resolution on
# every page. Neither is article content, so strip both before printing.
PRINT_CSS_OVERRIDE = (
    "<style>header{display:none!important}"
    ".bg-grid-light,.bg-grid-dark{background-image:none!important}</style></head>"
)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# Several articles embed a YouTube video. Chrome's --print-to-pdf loads the
# page via file://, and YouTube's player refuses to embed for an
# unrecognized origin there ("Video player configuration error, Error
# 153") instead of just failing to render — worse than blank space. Swap
# each embed for its thumbnail (via YouTube's oEmbed API) plus a caption
# pointing back to the video.
YOUTUBE_IFRAME_RE = re.compile(
    r'<iframe[^>]*\bsrc="(?:https?:)?//(?:www\.)?youtube(?:-nocookie)?\.com/embed/([\w-]+)[^"]*"[^>]*class="([^"]*)"[^>]*></iframe>'
)


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def fetch_youtube_thumbnail(video_id: str) -> tuple[str, str] | None:
    """(thumbnail_url, title) for a YouTube video via its oEmbed API, or
    None on any failure (caller then just leaves the iframe as-is)."""
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        data = json.loads(fetch(url))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    thumbnail_url = data.get("thumbnail_url")
    if not thumbnail_url:
        return None
    return thumbnail_url, data.get("title", "")


def replace_youtube_embeds(html: str) -> str:
    def _sub(match: re.Match) -> str:
        video_id, css_class = match.group(1), match.group(2)
        result = fetch_youtube_thumbnail(video_id)
        if not result:
            return match.group(0)
        thumbnail_url, title = result
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        caption = f"▶ {title} — watch at youtube.com/watch?v={video_id}" if title else f"▶ watch at {watch_url}"
        return (
            f'<a href="{watch_url}"><img src="{_escape_html(thumbnail_url)}" alt="{_escape_html(title)}" '
            f'class="{_escape_html(css_class)}" style="object-fit:cover"></a>'
            f'<div style="font-size:13px;color:#555;margin-top:4px">{_escape_html(caption)}</div>'
        )

    return YOUTUBE_IFRAME_RE.sub(_sub, html)


PUBLISHED_TIME_RE = re.compile(r'article:published_time"\s*content="([^"]+)"')
# A previously-saved stem is <date>-<HHMMSS>-<slug>; strip that known
# prefix to recover the slug (which may itself contain hyphens).
STEM_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-")


def stem_for(slug: str, html: str) -> str:
    """Filename stem for an article: its `article:published_time` OG meta
    tag (present on every article page, full YYYY-MM-DD HH:MM:SS precision)
    prefixed onto the slug, so html/*.html and pdf/*.pdf sort
    chronologically instead of alphabetically by title.

    Unlike fabtcg-stories-archive/fabrec-articles-archive (which get every
    article's date from one WP REST API listing call, letting same-date
    ties be resolved globally before anything is downloaded), rathetimes
    has no such listing metadata — dates are only discoverable by fetching
    each article's own page, one at a time. So instead of a date + seq-on-
    collision scheme requiring batch coordination, always include the full
    HH:MM:SS time — trivial to compute per-article in isolation, and the
    site's real timestamps are unique to the second for 543/555 articles
    anyway (the slug suffix, already unique, breaks the rare tie)."""
    match = PUBLISHED_TIME_RE.search(html)
    if not match:
        return slug
    date, time_str = match.group(1).split(" ")
    return f"{date}-{time_str.replace(':', '')}-{slug}"


def existing_html_stems() -> dict[str, Path]:
    """slug -> existing html/*.html Path, for the "already saved" skip
    check — since the filename is no longer just f"{slug}.html"."""
    mapping: dict[str, Path] = {}
    for f in HTML_DIR.glob("*.html"):
        m = STEM_DATE_PREFIX_RE.match(f.stem)
        slug = f.stem[m.end():] if m else f.stem
        mapping[slug] = f
    return mapping


def discover_article_slugs(limit: int | None = None) -> list[str]:
    """Newest-first. With `limit` set, stops as soon as that many slugs are
    found instead of crawling every page — useful for a quick test run."""
    print("Discovering articles via pagination...")
    slugs: list[str] = []
    seen = set()
    page = 1
    while True:
        url = f"{BASE_URL}/?page={page}"
        try:
            html = fetch(url)
        except urllib.error.URLError as exc:
            print(f"  page {page}: request failed ({exc}), stopping")
            break

        found = ARTICLE_HREF_RE.findall(html)
        new_on_page = [s for s in found if s not in seen]
        if not found:
            print(f"  page {page}: no articles found, stopping")
            break

        for slug in found:
            if slug not in seen:
                seen.add(slug)
                slugs.append(slug)

        print(f"  page {page}: {len(found)} links, {len(new_on_page)} new (total {len(slugs)})")

        if limit is not None and len(slugs) >= limit:
            slugs = slugs[:limit]
            print(f"  reached limit of {limit}, stopping")
            break

        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    return slugs


def download_html(slugs: list[str], override: bool = False) -> None:
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    existing = existing_html_stems()
    print(f"\nDownloading {len(slugs)} articles to {HTML_DIR}/")
    for i, slug in enumerate(slugs, 1):
        if slug in existing and not override:
            print(f"  [{i}/{len(slugs)}] {slug} (already saved)")
            continue
        url = f"{BASE_URL}/articles/{slug}"
        try:
            html = fetch(url)
        except urllib.error.URLError as exc:
            print(f"  [{i}/{len(slugs)}] {slug} FAILED: {exc}")
            continue
        dest = HTML_DIR / f"{stem_for(slug, html)}.html"
        dest.write_text(html, encoding="utf-8")
        print(f"  [{i}/{len(slugs)}] {slug}")
        time.sleep(REQUEST_DELAY_SECONDS)


def find_chrome() -> str:
    import os

    candidate = os.environ.get("CHROME_BIN")
    if candidate and shutil.which(candidate):
        return candidate
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    sys.exit("No Chrome/Chromium binary found. Install one or set CHROME_BIN.")


def render_pdf(chrome_bin: str, html_file: Path, pdf_file: Path) -> tuple[bool, str]:
    """Render html_file to pdf_file via headless Chrome, with print CSS
    overrides applied to a temp copy so the saved archival HTML is untouched."""
    html = html_file.read_text(encoding="utf-8", errors="replace")
    html = replace_youtube_embeds(html)
    stripped_html = html.replace("</head>", PRINT_CSS_OVERRIDE, 1)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", dir=html_file.parent, delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(stripped_html)
        tmp_path = Path(tmp.name)

    try:
        result = subprocess.run(
            [
                chrome_bin,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--print-to-pdf=" + str(pdf_file),
                "--no-pdf-header-footer",
                "--virtual-time-budget=15000",
                tmp_path.resolve().as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    if result.returncode != 0 or not pdf_file.exists():
        return False, result.stderr.strip()[:300]
    return True, ""


def compress_pdf(gs_bin: str, pdf_file: Path) -> None:
    """Recompress in place with Ghostscript. Chrome's print-to-pdf embeds
    photos as oversized raw bitmaps (tens of MB per article); the /ebook
    preset downsamples them to something reasonable for archival use."""
    compressed = pdf_file.with_suffix(".compressed.pdf")
    result = subprocess.run(
        [
            gs_bin,
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            "-dPDFSETTINGS=/ebook",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            "-sOutputFile=" + str(compressed),
            str(pdf_file),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 0 and compressed.exists():
        compressed.replace(pdf_file)
    else:
        compressed.unlink(missing_ok=True)


def convert_to_pdf(chrome_bin: str, gs_bin: str | None, override: bool = False) -> None:
    html_files = sorted(HTML_DIR.glob("*.html"))
    if not html_files:
        print(f"No HTML files found in {HTML_DIR}/, nothing to convert.")
        return

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nConverting {len(html_files)} articles to PDF in {PDF_DIR}/")
    for i, html_file in enumerate(html_files, 1):
        pdf_file = PDF_DIR / f"{html_file.stem}.pdf"
        if pdf_file.exists() and not override:
            print(f"  [{i}/{len(html_files)}] {html_file.stem} (already converted)")
            continue

        ok, err = render_pdf(chrome_bin, html_file, pdf_file)
        if not ok:
            print(f"  [{i}/{len(html_files)}] {html_file.stem} FAILED: {err}")
            continue

        if gs_bin:
            compress_pdf(gs_bin, pdf_file)

        size_kb = pdf_file.stat().st_size // 1024
        print(f"  [{i}/{len(html_files)}] {html_file.stem} ({size_kb} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-html", action="store_true", help="skip discovery/download, convert existing HTML only")
    parser.add_argument("--skip-pdf", action="store_true", help="only discover and download HTML, skip PDF conversion")
    parser.add_argument("--limit", type=int, help="only process the N most recent articles (for test runs)")
    parser.add_argument("--override", action="store_true", help="re-download HTML and re-render PDFs even if already present")
    args = parser.parse_args()

    if not args.skip_html:
        slugs = discover_article_slugs(limit=args.limit)
        if not slugs:
            sys.exit("No articles discovered; aborting.")
        download_html(slugs, override=args.override)

    if not args.skip_pdf:
        chrome_bin = find_chrome()
        gs_bin = shutil.which("gs")
        if not gs_bin:
            print("Note: Ghostscript ('gs') not found, skipping PDF compression "
                  "(files will be much larger).")
        convert_to_pdf(chrome_bin, gs_bin, override=args.override)

    print("\nDone.")


if __name__ == "__main__":
    main()
