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
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
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
#
# Article images are also full-bleed/full-width by the site's own screen
# CSS, with nothing constraining print size, so many render at near-full-
# page, pushing pages down to a single sentence of text. Cap display size
# for print instead — this does trade off some of the "big hero art" look
# this archive is otherwise tuned for, in exchange for a more compact,
# printable binder.
PRINT_OVERRIDE = """
<style>
header#masthead, .breadcrumbs, footer#colophon, a.sr-only { display:none!important }
img { max-width:100%!important; max-height:4in!important; width:auto!important;
      height:auto!important; object-fit:contain!important }
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


# Chrome's --print-to-pdf doesn't render cross-origin iframes (e.g. Vimeo's
# player), leaving blank space where a story's embedded video was. Swap
# each one for its Vimeo thumbnail (fetched via Vimeo's oEmbed API) plus a
# caption pointing back to the video, so the PDF shows *something* instead
# of empty space. Applied to a temp copy only, like PRINT_OVERRIDE above.
VIMEO_IFRAME_RE = re.compile(
    r'<iframe[^>]*\bsrc="https://player\.vimeo\.com/video/(\d+)[^"]*"[^>]*></iframe>'
)


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def fetch_vimeo_thumbnail(video_id: str) -> tuple[str, str] | None:
    """(thumbnail_url, title) for a Vimeo video via its oEmbed API, or None
    on any failure (caller then just leaves the iframe as-is)."""
    url = f"https://vimeo.com/api/oembed.json?url=https://vimeo.com/{video_id}&width=960"
    try:
        data = json.loads(fetch(url))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    thumbnail_url = data.get("thumbnail_url")
    if not thumbnail_url:
        return None
    return thumbnail_url, data.get("title", "")


def replace_vimeo_embeds(html: str) -> str:
    def _sub(match: re.Match) -> str:
        video_id = match.group(1)
        result = fetch_vimeo_thumbnail(video_id)
        if not result:
            return match.group(0)
        thumbnail_url, title = result
        caption = f"▶ {title} — watch at vimeo.com/{video_id}" if title else f"▶ watch at vimeo.com/{video_id}"
        return (
            f'<img src="{_escape_html(thumbnail_url)}" alt="{_escape_html(title)}" '
            'style="position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover">'
            '<div style="position:absolute;bottom:0;left:0;right:0;background:rgba(0,0,0,.65);'
            f'color:#fff;font-size:13px;padding:6px 10px">{_escape_html(caption)}</div>'
        )

    return VIMEO_IFRAME_RE.sub(_sub, html)


def sanitize_slug(slug: str, fallback: str) -> str:
    """Make a WP slug safe/sane for use in a filename. A couple of
    fabtcg.com story slugs (in-universe "corrupted text" effects) are
    percent-encoded combining-mark soup, e.g. a slug containing literal
    `%cc%b8%cd%8d...` that decodes to "p̸͍̬̭̭̺͉̣̐̾͆̚r̴͔͍͐ȯ̴̤̰͠t̵̰̘͑o̶͍" — dozens of
    stacked Unicode combining marks on five base letters. Percent-decode,
    NFKD-normalize and drop combining marks to recover the base letters
    ("proto"), then strip anything left that isn't alphanumeric/hyphen. If
    nothing usable survives, fall back to the WP post id so the filename
    still exists and is unique."""
    decoded = urllib.parse.unquote(slug)
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFKD", decoded)
        if not unicodedata.combining(ch)
    )
    cleaned = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")
    return cleaned or f"story-{fallback}"


def assign_stems(articles: list[dict]) -> dict[str, str]:
    """Filename stem per story: its WordPress `date` prefixed onto the slug,
    so files sort chronologically and combine_pdfs' plain alphabetical merge
    produces a chronologically ordered combined.pdf.

    `date` alone isn't fine-grained enough: fabtcg.com bulk-migrated most of
    its stories into this WordPress instance in one batch, so ~90% of them
    share one of a handful of dates from that migration week rather than
    their real original publish date. Where multiple stories share a date,
    break the tie with a zero-padded sequence number ordered by WP post
    `id` (monotonically increasing, no ties) instead of falling back to
    alphabetical-by-slug — `id` order tracks the site's original ordering
    far more reliably than the title text does."""
    by_date: dict[str, list[dict]] = {}
    for article in articles:
        by_date.setdefault(article["date"][:10], []).append(article)

    stems: dict[str, str] = {}
    for date, group in by_date.items():
        if len(group) == 1:
            article = group[0]
            slug = sanitize_slug(article["slug"], article["id"])
            stems[article["slug"]] = f"{date}-{slug}"
            continue
        for seq, article in enumerate(sorted(group, key=lambda a: a["id"])):
            slug = sanitize_slug(article["slug"], article["id"])
            stems[article["slug"]] = f"{date}-{seq:03d}-{slug}"
    return stems


def discover_articles(limit: int | None = None) -> list[dict]:
    """Newest-first (the API's default order). With `limit` set, stops as
    soon as that many articles are found — useful for a quick test run."""
    print("Discovering stories via the WP REST API...")
    articles: list[dict] = []
    page = 1
    while True:
        url = f"{API_URL}?per_page={PER_PAGE}&page={page}&_fields=slug,link,date,id"
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


def download_html(articles: list[dict], stems: dict[str, str], override: bool = False) -> None:
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nDownloading {len(articles)} stories to {HTML_DIR}/")
    for i, article in enumerate(articles, 1):
        stem = stems[article["slug"]]
        dest = HTML_DIR / f"{stem}.html"
        if dest.exists() and not override:
            print(f"  [{i}/{len(articles)}] {stem} (already saved)")
            continue
        try:
            html = fetch(article["link"])
        except urllib.error.URLError as exc:
            print(f"  [{i}/{len(articles)}] {stem} FAILED: {exc}")
            continue
        dest.write_bytes(html)
        print(f"  [{i}/{len(articles)}] {stem}")
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
    html = replace_vimeo_embeds(html)
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



# Slugs (filename substring match, checked against the stem) of story-listed
# entries that aren't actually lore stories, so combine_pdfs() leaves them
# out of the combined reading copy. Still archived normally in pdf/ like
# everything else — this only affects the merge.
#   "roll-of-honor": a leaderboard feature (e.g. "Roll of Honor: Viserai").
#   "learn": "<Hero> – Learn" how-to-play guides (rhinar-learn,
#     azalea-learn, azalea-learn-aiming-high, the bare "learn" for katsu).
#   "primer": set pre-release preview articles (outsiders-pre-release-
#     primer, the-hunted-primer), not lore.
COMBINE_EXCLUDE_MARKERS = ("roll-of-honor", "learn", "primer")


def combine_pdfs(gs_bin: str) -> None:
    """Merge pdf/*.pdf into a single combined.pdf via Ghostscript. Filenames
    are date-prefixed (see assign_stems), so the plain alphabetical glob
    order here is also chronological, oldest story first. Entries matching
    COMBINE_EXCLUDE_MARKERS are skipped (see above)."""
    all_pdfs = sorted(PDF_DIR.glob("*.pdf"))
    pdf_files = [p for p in all_pdfs if not any(m in p.stem for m in COMBINE_EXCLUDE_MARKERS)]
    excluded = len(all_pdfs) - len(pdf_files)
    if not pdf_files:
        print(f"No PDFs found in {PDF_DIR}/, nothing to combine.")
        return

    print(f"\nCombining {len(pdf_files)} PDFs into {COMBINED_PDF}"
          + (f" ({excluded} non-story entries excluded)" if excluded else ""))
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
        download_html(articles, assign_stems(articles), override=args.override)

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
