#!/usr/bin/env python3
"""Archive every article on fabrec.gg/articles/ as HTML + PDF.

Discovers articles via the WordPress REST API (much more reliable than
scraping the listing page), downloads each article's live HTML, then
renders each saved HTML file to PDF using headless Chrome. Safe to re-run:
existing files are skipped, so an interrupted run just picks up where it
left off.

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
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# The site's REST API for the WordPress instance backing /articles/ lives
# under that same path prefix (not at the domain root).
API_URL = "https://fabrec.gg/articles/wp-json/wp/v2/posts"
USER_AGENT = "Mozilla/5.0 (compatible; fabrec-archive/1.0; personal archival script)"
REQUEST_DELAY_SECONDS = 0.75
PER_PAGE = 100

SCRIPT_DIR = Path(__file__).resolve().parent
HTML_DIR = SCRIPT_DIR / "html"
PDF_DIR = SCRIPT_DIR / "pdf"

# Strip the site nav and footer, plus the tags sidebar (not article content),
# via injected CSS. Applied to a temp copy only — the saved archival HTML in
# html/*.html is left untouched.
PRINT_CSS_OVERRIDE = (
    "<style>nav.navbar,.footer,.blog-sidebar{display:none!important}</style></head>"
)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def discover_articles(limit: int | None = None) -> list[dict]:
    """Newest-first (the API's default order). With `limit` set, stops as
    soon as that many articles are found — useful for a quick test run."""
    print("Discovering articles via the WP REST API...")
    articles: list[dict] = []
    page = 1
    while True:
        url = f"{API_URL}?per_page={PER_PAGE}&page={page}&_fields=slug,link"
        try:
            body = fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                # WP returns 400 once you page past the last page.
                break
            print(f"  page {page}: request failed ({exc}), stopping")
            break
        except urllib.error.URLError as exc:
            print(f"  page {page}: request failed ({exc}), stopping")
            break

        batch = json.loads(body)
        if not batch:
            break

        articles.extend(batch)
        print(f"  page {page}: {len(batch)} articles (total {len(articles)})")

        if limit is not None and len(articles) >= limit:
            articles = articles[:limit]
            print(f"  reached limit of {limit}, stopping")
            break

        if len(batch) < PER_PAGE:
            break

        page += 1
        time.sleep(REQUEST_DELAY_SECONDS)

    return articles


def download_html(articles: list[dict], override: bool = False) -> None:
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nDownloading {len(articles)} articles to {HTML_DIR}/")
    for i, article in enumerate(articles, 1):
        slug = article["slug"]
        dest = HTML_DIR / f"{slug}.html"
        if dest.exists() and not override:
            print(f"  [{i}/{len(articles)}] {slug} (already saved)")
            continue
        try:
            html = fetch(article["link"])
        except urllib.error.URLError as exc:
            print(f"  [{i}/{len(articles)}] {slug} FAILED: {exc}")
            continue
        dest.write_bytes(html)
        print(f"  [{i}/{len(articles)}] {slug}")
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
    html = html_file.read_text(encoding="utf-8")
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
        articles = discover_articles(limit=args.limit)
        if not articles:
            sys.exit("No articles discovered; aborting.")
        download_html(articles, override=args.override)

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
