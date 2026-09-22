#!/usr/bin/env python3
"""Archive every story on fabtcg.com/stories/ as HTML + PDF, tuned for
printing into a physical lore binder.

Discovers articles via the WordPress REST API (much more reliable than
scraping the listing page), downloads each article's live HTML, then
renders each saved HTML file to PDF using headless Chrome. Safe to re-run:
existing files are skipped, so an interrupted run just picks up where it
left off.

PDFs are print-oriented: site nav/breadcrumbs/footer and the "World of
Rathe" related-links block are stripped so each PDF is just the story and
its art, and images are compressed at 300 DPI (good for paper) rather than
screen resolution.

fabtcg.com sits behind a WAF that 403s requests without a browser-looking
User-Agent (anything not starting with "Mozilla/5.0" gets blocked) — see
USER_AGENT below.

Usage:
    python3 archive.py                 # full run: discover, download, convert
    python3 archive.py --limit 2       # only the 2 most recent articles (test run)
    python3 archive.py --skip-pdf      # only download HTML
    python3 archive.py --skip-html     # only convert already-downloaded HTML
    python3 archive.py --override      # re-download/re-render even if files already exist
    python3 archive.py --combine       # also merge pdf/*.pdf into combined.pdf
    python3 archive.py --combine-only  # skip discover/download/convert, just (re-)merge existing PDFs

Requires: google-chrome (or set CHROME_BIN) for the PDF step, and gs
(Ghostscript) for PDF compression and for --combine/--combine-only (optional
for a plain run, required for those two flags). No third-party Python
packages needed.
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

API_URL = "https://fabtcg.com/api/wp/v2/story"
# Must start with "Mozilla/5.0" or the site's WAF returns 403.
USER_AGENT = "Mozilla/5.0 (compatible; fabtcg-archive/1.0; personal archival script)"
REQUEST_DELAY_SECONDS = 0.75
PER_PAGE = 100

SCRIPT_DIR = Path(__file__).resolve().parent
HTML_DIR = SCRIPT_DIR / "html"
PDF_DIR = SCRIPT_DIR / "pdf"
COMBINED_PDF = SCRIPT_DIR / "combined.pdf"

# For printing into a binder we only want the story itself: strip the site
# nav/menu, breadcrumb trail, and footer via CSS, and remove the "World of
# Rathe" related-links block WordPress appends to the end of every story
# (it has no stable wrapper of its own, so it's removed via a small script
# instead of a CSS selector). Applied to a temp copy only — the saved
# archival HTML in html/*.html is left untouched.
PRINT_OVERRIDE = """
<style>
header#masthead, .breadcrumbs, footer#colophon, a.sr-only { display:none!important }
</style>
<script>
document.querySelectorAll('h2.wp-block-heading').forEach(function (h) {
  if (h.textContent.trim() === 'World of Rathe') {
    var next = h.nextElementSibling;
    h.remove();
    if (next && next.classList.contains('wp-block-fl-fl-page-list-ssr')) next.remove();
  }
});
</script>
</body>
"""


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def discover_articles(limit: int | None = None) -> list[dict]:
    """Newest-first (the API's default order). With `limit` set, stops as
    soon as that many articles are found — useful for a quick test run."""
    print("Discovering stories via the WP REST API...")
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
        print(f"  page {page}: {len(batch)} stories (total {len(articles)})")

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
    print(f"\nDownloading {len(articles)} stories to {HTML_DIR}/")
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
    """Render html_file to pdf_file via headless Chrome, with the print
    overrides applied to a temp copy so the saved archival HTML is untouched."""
    html = html_file.read_text(encoding="utf-8", errors="replace")
    stripped_html = html.replace("</body>", PRINT_OVERRIDE, 1)

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
    photos as oversized raw bitmaps (tens of MB per article); the /printer
    preset (300 DPI, good for physical printing) brings that down to
    something reasonable while keeping the art crisp on paper."""
    compressed = pdf_file.with_suffix(".compressed.pdf")
    result = subprocess.run(
        [
            gs_bin,
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            "-dPDFSETTINGS=/printer",
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
    print(f"\nConverting {len(html_files)} stories to PDF in {PDF_DIR}/")
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


def combine_pdfs(gs_bin: str) -> None:
    """Merge every pdf/*.pdf into a single combined.pdf via Ghostscript, in
    the same (alphabetical-by-slug) order they're written in."""
    pdf_files = sorted(PDF_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"No PDFs found in {PDF_DIR}/, nothing to combine.")
        return

    print(f"\nCombining {len(pdf_files)} PDFs into {COMBINED_PDF}")
    result = subprocess.run(
        [
            gs_bin,
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            "-sOutputFile=" + str(COMBINED_PDF),
            *(str(p) for p in pdf_files),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0 or not COMBINED_PDF.exists():
        print(f"  FAILED: {result.stderr.strip()[:300]}")
        return

    size_mb = COMBINED_PDF.stat().st_size / (1024 * 1024)
    print(f"  wrote {COMBINED_PDF.name} ({size_mb:.1f} MB, {len(pdf_files)} stories)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-html", action="store_true", help="skip discovery/download, convert existing HTML only")
    parser.add_argument("--skip-pdf", action="store_true", help="only discover and download HTML, skip PDF conversion")
    parser.add_argument("--limit", type=int, help="only process the N most recent articles (for test runs)")
    parser.add_argument("--override", action="store_true", help="re-download HTML and re-render PDFs even if already present")
    parser.add_argument("--combine", action="store_true", help="after converting, also merge pdf/*.pdf into combined.pdf")
    parser.add_argument("--combine-only", action="store_true", help="skip discover/download/convert; just (re-)merge existing PDFs into combined.pdf")
    args = parser.parse_args()

    skip_html = args.skip_html or args.combine_only
    skip_pdf = args.skip_pdf or args.combine_only
    combine = args.combine or args.combine_only

    if not skip_html:
        articles = discover_articles(limit=args.limit)
        if not articles:
            sys.exit("No articles discovered; aborting.")
        download_html(articles, override=args.override)

    if not skip_pdf:
        chrome_bin = find_chrome()
        gs_bin = shutil.which("gs")
        if not gs_bin:
            print("Note: Ghostscript ('gs') not found, skipping PDF compression "
                  "(files will be much larger).")
        convert_to_pdf(chrome_bin, gs_bin, override=args.override)

    if combine:
        gs_bin = shutil.which("gs")
        if not gs_bin:
            sys.exit("Ghostscript ('gs') is required for --combine/--combine-only.")
        combine_pdfs(gs_bin)

    print("\nDone.")


if __name__ == "__main__":
    main()
