# fabrec-articles-archive

Archives every article on [fabrec.gg/articles/](https://fabrec.gg/articles/)
(Flesh & Blood TCG strategy/recommendation content) as HTML, then converts
each to PDF for offline/long-term storage. Sibling to
[`../rathetimes-archive`](../rathetimes-archive) and
[`../fabtcg-stories-archive`](../fabtcg-stories-archive), same overall
approach.

## What it does

1. Discovers every article via the WordPress REST API. The listing at
   `fabrec.gg/articles/` is actually served by a WordPress instance proxied
   in under that path prefix (not the site root), so the API lives at
   `fabrec.gg/articles/wp-json/wp/v2/posts` rather than the usual
   `fabrec.gg/wp-json/...`. One paginated crawl at `per_page=100` covers the
   whole catalog (270 articles as of this writing, 3 pages).
2. Downloads each article's HTML as-is to
   `html/<date>[-<seq>]-<slug>.html` (see "File naming" below).
3. Renders each to `pdf/<date>[-<seq>]-<slug>.pdf` via headless Chrome,
   stripping the site nav, footer, and the tags sidebar so the PDF is just
   the article, its art, and the author bio. (Only the temp copy used for
   rendering is modified; the saved `html/*.html` files are untouched.)
4. Recompresses the PDF with Ghostscript (`/ebook` preset) — Chrome's
   `--print-to-pdf` embeds photos as raw full-resolution bitmaps, so an
   uncompressed run produces large PDFs; this brings each down to a few
   hundred KB with no visible quality loss on screen.

Article images are also full-bleed/full-width by the site's own screen
CSS, with nothing constraining print size, so some rendered at near-full-
page, pushing pages down to a single sentence of text. `PRINT_CSS_OVERRIDE`
caps display size (`max-height:4in`) for print instead.

### File naming

Files are named `<date>-<slug>` (e.g.
`2024-10-30-precon-progression-verdance-blitz-deck.pdf`), using each
article's WordPress `date` field, so `html/`/`pdf/` sort chronologically
instead of alphabetically by title. Unlike `../fabtcg-stories-archive`'s
bulk-migrated catalog, fabrec's dates look like genuine original publish
dates — 263 of 270 articles have a distinct date, spanning 2023-05-02 to
2024-10-30. Where a handful of articles do share a date, the filename gets
a zero-padded sequence number too — `<date>-<seq>-<slug>` — ordered by
each article's WordPress post `id` (monotonically increasing, no ties)
rather than falling back to alphabetical-by-title.

Order is whatever the API returns (newest-first) for the discovery/log
output above, but the saved filenames are chronological as described.
Each article is its own PDF rather than one combined file.

A few articles embed a YouTube video. Chrome's `--print-to-pdf` loads the
page via `file://`, and YouTube's player refuses to embed for an
unrecognized origin there ("Video player configuration error, Error 153")
instead of just failing to render — worse than blank space.
`replace_youtube_embeds()` swaps each one for its thumbnail (via YouTube's
oEmbed API) plus a caption pointing back to the video, applied to the temp
copy only, same as the nav/footer stripping above. (One article has a
native, self-hosted `<video>` instead — those aren't cross-origin iframes,
so Chrome renders them fine and no fix is needed.)

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

Output goes to `html/` and `pdf/` next to the script (git-ignored).

## Requirements

- Python 3.10+, stdlib only (no pip installs).
- `google-chrome` (or another Chromium build) for the PDF step. Set
  `CHROME_BIN` to override which binary is used.
- `gs` (Ghostscript) for PDF compression. Optional — if missing, PDFs are
  still produced, just much larger.

No WAF or special `User-Agent` requirement was found (unlike fabtcg.com);
the script still sends a browser-looking one out of politeness. `robots.txt`
isn't a real file on this site — any unmatched path (including
`/robots.txt`) falls through to the Next.js front end's custom 404 page —
so there's nothing to check there. The script adds a ~0.75s delay between
requests to stay polite regardless.
