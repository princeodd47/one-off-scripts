# fabtcg-stories-archive

Archives every story on [fabtcg.com/stories/](https://fabtcg.com/stories/)
(Flesh & Blood TCG's official lore section) as HTML, then converts each to
PDF for printing into a physical lore binder. Sibling to
[`../rathetimes-archive`](../rathetimes-archive), same overall approach,
adapted for this site and tuned for print rather than screen archival.

## What it does

1. Discovers every story via the WordPress REST API
   (`fabtcg.com/api/wp/v2/story`) instead of scraping the listing page —
   the site runs WordPress with that endpoint exposed, so this is far more
   reliable than parsing paginated HTML. One request with `per_page=100`
   covers the whole catalog (83 stories as of this writing).
2. Downloads each article's live HTML as-is to
   `html/<date>[-<seq>]-<slug>.html` (see "File naming" below). Note some
   story URLs live under `/articles/story/<slug>/` and others under
   `/hero/<hero>/story/<slug>` — the script always uses the API's `link`
   field rather than constructing URLs, so this doesn't matter.
3. Renders each to `pdf/<date>[-<seq>]-<slug>.pdf` via headless Chrome,
   stripping the site nav, breadcrumb trail, footer, and the "World of
   Rathe" related-links block WordPress appends to every story, so each PDF
   is just the story and its art — no wasted pages on site chrome. (Only
   the temp copy used for rendering is modified; the saved `html/*.html`
   files are untouched.)
4. Recompresses the PDF with Ghostscript at 300 DPI (the `/printer`
   preset) — Chrome's print-to-pdf embeds photos as oversized raw bitmaps
   (tens of MB per article), so this is still a large size reduction, just
   tuned for crisp paper output rather than the smallest possible file.

### File naming

Files are named `<date>-<slug>` (e.g. `2026-05-08-omens-in-the-sky.pdf`),
using each story's WordPress `date` field, so both `html/`/`pdf/` and the
combined PDF (below) sort chronologically instead of alphabetically by
title.

`date` alone isn't reliable, though: fabtcg.com bulk-migrated most of its
back catalog into this WordPress instance in one batch, so ~90% of stories
share one of a handful of dates from that migration week rather than a real
original publish date (e.g. 20 different stories are all dated
`2025-07-25`). Where multiple stories share a date, the filename gets a
zero-padded sequence number too — `<date>-<seq>-<slug>` — ordered by each
story's WordPress post `id` (monotonically increasing, no ties) rather than
falling back to alphabetical-by-title. `id` order tracks the site's
original ordering far more reliably than the title text does, since it's
assigned in whatever order the importer processed the old site's stories.

A couple of story slugs are "in-universe corrupted text" effects — stacks
of Unicode combining marks on a handful of base letters, percent-encoded in
the raw slug — which would otherwise produce unusable filenames.
`sanitize_slug()` decodes and strips those down to the base letters (e.g.
`teklovossen-story-proto`) before building the filename.

Each article is its own PDF, so you can reprint or reorder individual
stories in the binder. Pass `--combine` (or `--combine-only`) to also merge
everything in `pdf/` into a single `combined.pdf`, in the same (now
chronological) filename order — handy for taking the whole archive to a
print shop. "Roll of Honor" entries (leaderboard pages, not stories — e.g.
`roll-of-honor-viserai`) are still archived normally in `html/`/`pdf/` but
are excluded from `combined.pdf`.

**WAF note:** fabtcg.com blocks any request whose `User-Agent` doesn't
start with `Mozilla/5.0` (a 403, regardless of `robots.txt`, which allows
everything). `USER_AGENT` in the script is set accordingly.

Re-running the script is safe: both the download and convert steps skip
files that already exist, so an interrupted run just resumes.

## Usage

```
python3 archive.py                 # discover, download, and convert everything
python3 archive.py --limit 2       # only the 2 most recent articles (test run)
python3 archive.py --skip-pdf      # only download HTML (no Chrome needed)
python3 archive.py --skip-html     # only convert already-downloaded HTML
python3 archive.py --override      # re-download/re-render even if files already exist
python3 archive.py --combine       # also merge pdf/*.pdf into combined.pdf
python3 archive.py --combine-only  # skip discover/download/convert, just (re-)merge existing PDFs
```

Output goes to `html/` and `pdf/` next to the script (git-ignored), plus
`combined.pdf` if `--combine`/`--combine-only` is used.

## Requirements

- Python 3.10+, stdlib only (no pip installs).
- `google-chrome` (or another Chromium build) for the PDF step. Set
  `CHROME_BIN` to override which binary is used.
- `gs` (Ghostscript) for PDF compression. Optional for a plain run (PDFs
  are still produced, just much larger) but required for
  `--combine`/`--combine-only`.

Checked `robots.txt` first: `User-agent: * / Disallow:` — nothing
disallowed. The script adds a ~0.75s delay between requests to stay polite.
