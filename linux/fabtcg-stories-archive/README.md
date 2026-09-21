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
2. Downloads each article's live HTML as-is to `html/<slug>.html`. Note
   some story URLs live under `/articles/story/<slug>/` and others under
   `/hero/<hero>/story/<slug>` — the script always uses the API's `link`
   field rather than constructing URLs, so this doesn't matter.
3. Renders each to `pdf/<slug>.pdf` via headless Chrome, stripping the site
   nav, breadcrumb trail, footer, and the "World of Rathe" related-links
   block WordPress appends to every story, so each PDF is just the story
   and its art — no wasted pages on site chrome. (Only the temp copy used
   for rendering is modified; the saved `html/*.html` files are untouched.)
4. Recompresses the PDF with Ghostscript at 300 DPI (the `/printer`
   preset) — Chrome's print-to-pdf embeds photos as oversized raw bitmaps
   (tens of MB per article), so this is still a large size reduction, just
   tuned for crisp paper output rather than the smallest possible file.

Order is whatever the API returns (newest-first); there's no in-universe
chronology exposed anywhere in the site's data, so this doesn't attempt to
sort by story timeline. Each article is its own PDF rather than one
combined file, so you can reprint or reorder individual stories in the
binder.

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
```

Output goes to `html/` and `pdf/` next to the script (git-ignored).

## Requirements

- Python 3.10+, stdlib only (no pip installs).
- `google-chrome` (or another Chromium build) for the PDF step. Set
  `CHROME_BIN` to override which binary is used.
- `gs` (Ghostscript) for PDF compression. Optional — if missing, PDFs are
  still produced, just much larger.

Checked `robots.txt` first: `User-agent: * / Disallow:` — nothing
disallowed. The script adds a ~0.75s delay between requests to stay polite.
