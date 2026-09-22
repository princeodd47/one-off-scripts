# rathetimes-archive

Archives every article on [rathetimes.com](https://rathetimes.com) (a Flesh &
Blood strategy site that announced it's shutting down) as HTML, then converts
each to PDF for offline/long-term storage.

## What it does

1. Crawls the paginated article listing (`?page=N`) to discover every
   `/articles/<slug>` URL — no API or sitemap exists, so this is the only
   enumeration path. Stops automatically at the first empty page.
2. Downloads each article's HTML as-is to `html/<slug>.html`.
3. Renders each to `pdf/<slug>.pdf` via headless Chrome, then recompresses
   with Ghostscript. Chrome's `--print-to-pdf` embeds photos as raw
   full-resolution bitmaps, so an uncompressed run produces ~20-25 MB PDFs
   *per article*; the Ghostscript `/ebook` pass brings that down to a few
   hundred KB with no visible quality loss.

The site's header contains a mobile-nav drawer (Alpine.js, `position:
fixed`) that only gets hidden once client-side JS finishes running.
Headless Chrome's one-shot print doesn't reliably wait for that, so the
drawer would otherwise render as a full-page overlay blocking the article
content on every printed page. The script strips the header and a
decorative tiled background (the other big contributor to file size) via
injected print CSS — but only in the temp copy used for PDF rendering; the
saved `html/*.html` files are untouched, byte-for-byte copies of what the
server returned.

Re-running the script is safe: both the download and convert steps skip
files that already exist, so an interrupted run just resumes.

## Usage

```
python3 archive.py                 # discover, download, and convert everything
python3 archive.py --limit 2       # only the 2 most recent articles (test run)
python3 archive.py --skip-pdf      # only download HTML (no Chrome needed)
python3 archive.py --skip-html     # only convert already-downloaded HTML
python3 archive.py --override      # re-download/re-render even if files already exist
```

The listing is newest-first, so `--limit N` stops discovery as soon as it has
N slugs rather than crawling all ~28 pages.

Output goes to `html/` and `pdf/` next to the script (git-ignored — this
produces ~550 articles worth of files, not something to commit).

## Requirements

- Python 3.10+, stdlib only (no pip installs).
- `google-chrome` (or another Chromium build) for the PDF step. Set
  `CHROME_BIN` to override which binary is used.
- `gs` (Ghostscript) for PDF compression. Optional — if missing, PDFs are
  still produced, just much larger.

Checked `robots.txt` first: `User-agent: * / Disallow:` — nothing
disallowed. The script adds a ~0.75s delay between requests to stay polite.
