#!/usr/bin/env python3
"""Find a free, legal EPUB for a book request and add it to the inbox.

Sources, in order of preference:
  1. Standard Ebooks (standardebooks.org) - carefully produced public-domain editions
  2. Project Gutenberg, through its own OPDS search

Usage:
  fetch_book.py add "<request text>"   find, download, add to manifest, rebuild site
  fetch_book.py build                  rebuild manifest-derived files (opds.xml, index.html)
  fetch_book.py find "<request text>"  print the best match only, download nothing

Standard library only. Writes a short result summary to $GITHUB_OUTPUT when set.
"""
import datetime as dt
import html
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOKS_DIR = os.path.join(ROOT, "books")
MANIFEST = os.path.join(ROOT, "manifest.json")
UA = "kindle-inbox/1.0 (+https://github.com/1mposer/kindle-inbox)"
MAX_BYTES = 60 * 1024 * 1024
STOP = {"the", "a", "an", "of", "and", "by", "to", "in", "on", "for", "book", "ebook", "epub"}
MIN_SCORE = 0.6


def site_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "1mposer/kindle-inbox")
    owner, name = repo.split("/", 1)
    return os.environ.get("SITE_URL", f"https://{owner.lower()}.github.io/{name}").rstrip("/")


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("file too large")
    return data if binary else data.decode("utf-8", "replace")


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def tokens(s):
    return [t for t in norm(s).split() if t not in STOP]


def slugify(s, limit=60):
    return re.sub(r"\s+", "-", norm(s))[:limit].strip("-") or "book"


def score(query, title, authors):
    """How well a candidate matches the request, 0..~1.3."""
    q = set(tokens(query))
    if not q:
        return 0.0
    t = set(tokens(title))
    a = set(tokens(" ".join(authors)))
    covered = len(q & (t | a)) / len(q)          # how much of the request is explained
    precision = len(q & t) / len(t) if t else 0  # penalise long titles that merely contain a word
    exact = 0.3 if norm(title) in norm(query) and t else 0.0
    return round(covered * 0.7 + precision * 0.3 + exact, 3)


def parse_request(text):
    """Accept an issue-form body, a plain issue title, or 'Title by Author'."""
    full = text.strip()
    text = full
    m = re.search(r"###\s*Book title\s*\n+(.+?)(?:\n###|\Z)", full, re.S | re.I)
    if m:
        text = m.group(1).strip()
        a = re.search(r"###\s*Author[^\n]*\n+(.+?)(?:\n###|\Z)", full, re.S | re.I)
        if a and a.group(1).strip() not in ("_No response_", ""):
            text = f"{text} {a.group(1).strip()}"
    text = re.sub(r"^(book|add|send|get)\s*[:\-]\s*", "", text, flags=re.I)
    text = text.replace(" by ", " ")
    return re.sub(r"\s+", " ", text).strip()[:200]


def search_standard_ebooks(query):
    page = get("https://standardebooks.org/ebooks?" + urllib.parse.urlencode({"query": query}))
    out = []
    for block in page.split('<li typeof="schema:Book"')[1:]:
        m = re.search(r'about="(/ebooks/[^"]+)"', block)
        n = re.search(r'property="schema:url"><span property="schema:name">([^<]+)</span>', block)
        if not m or not n:
            continue
        authors = re.findall(r'property="schema:author"[^>]*><a [^>]*><span property="schema:name">([^<]+)</span>', block)
        out.append({
            "source": "standardebooks",
            "path": m.group(1),
            "title": html.unescape(n.group(1)),
            "authors": [html.unescape(x) for x in authors],
            "in_progress": "in-progress" in block[:200],
        })
    for c in out:
        c["score"] = score(query, c["title"], c["authors"])
    return out


def standard_ebooks_epub(path):
    page = get("https://standardebooks.org" + path)
    links = re.findall(r'href="(' + re.escape(path) + r'/downloads/[^"]+\.epub)"', page)
    plain = [l for l in links if not l.endswith((".kepub.epub", "_advanced.epub"))]
    if not plain:
        return None
    return "https://standardebooks.org" + plain[0] + "?source=download"


def search_gutenberg(query):
    """Project Gutenberg's own OPDS search (relevance ordered)."""
    feed = get("https://www.gutenberg.org/ebooks/search.opds/?" + urllib.parse.urlencode({"query": query}))
    out = []
    for rank, entry in enumerate(feed.split("<entry>")[1:]):
        m = re.search(r"<id>https?://www\.gutenberg\.org/ebooks/(\d+)\.opds</id>", entry)
        t = re.search(r"<title>([^<]+)</title>", entry)
        if not m or not t:
            continue
        c = re.search(r'<content type="text">([^<]*)</content>', entry)
        authors = [html.unescape(c.group(1)).strip()] if c and c.group(1).strip() else []
        title = html.unescape(t.group(1)).split(";")[0].split(":")[0].strip()
        out.append({
            "source": "gutenberg",
            "id": int(m.group(1)),
            "title": title,
            "authors": authors,
            "rank": rank,
            "score": score(query, title, authors) - rank * 0.002,
        })
        if len(out) >= 20:
            break
    return out


def best_match(query):
    se, pg = [], []
    errors = []
    try:
        se = [c for c in search_standard_ebooks(query) if not c["in_progress"]]
    except Exception as e:  # keep going with Gutenberg
        errors.append(f"Standard Ebooks: {e}")
    try:
        pg = search_gutenberg(query)
    except Exception as e:
        errors.append(f"Gutenberg: {e}")
    se.sort(key=lambda c: -c["score"])
    pg.sort(key=lambda c: -c["score"])
    s, p = (se[0] if se else None), (pg[0] if pg else None)
    # Prefer Standard Ebooks unless Gutenberg is clearly the better title match.
    if s and s["score"] >= MIN_SCORE and (not p or s["score"] >= p["score"] - 0.15):
        return s, errors
    if p and p["score"] >= MIN_SCORE:
        return p, errors
    return None, errors


def load_manifest():
    if os.path.exists(MANIFEST):
        with open(MANIFEST, encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "books": []}


def save_manifest(m):
    m["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(m, f, indent=2, ensure_ascii=False)
        f.write("\n")


def add(request_text, requester=""):
    query = parse_request(request_text)
    if not query:
        return {"status": "error", "message": "The request was empty."}
    match, errors = best_match(query)
    if not match:
        msg = f'No free edition found for "{query}" on Standard Ebooks or Project Gutenberg.'
        if errors:
            msg += " (" + "; ".join(errors) + ")"
        return {"status": "notfound", "message": msg, "query": query}

    if match["source"] == "standardebooks":
        book_id = "se:" + match["path"].removeprefix("/ebooks/")
        url = standard_ebooks_epub(match["path"])
        page = "https://standardebooks.org" + match["path"]
        if not url:
            return {"status": "error", "message": f"Found {page} but no EPUB link.", "query": query}
    else:
        book_id = f"pg:{match['id']}"
        url = f"https://www.gutenberg.org/ebooks/{match['id']}.epub3.images"
        page = f"https://www.gutenberg.org/ebooks/{match['id']}"

    m = load_manifest()
    for b in m["books"]:
        if b["id"] == book_id:
            return {"status": "duplicate", "message": f'"{b["title"]}" is already in the inbox.',
                    "query": query, "book": b}

    data = get(url, binary=True)
    if data[:2] != b"PK":
        return {"status": "error", "message": f"Download from {url} was not an EPUB.", "query": query}
    author = ", ".join(match["authors"]) or "Unknown"
    fname = f"{slugify(match['authors'][0] if match['authors'] else 'unknown', 30)}_{slugify(match['title'])}.epub"
    os.makedirs(BOOKS_DIR, exist_ok=True)
    with open(os.path.join(BOOKS_DIR, fname), "wb") as f:
        f.write(data)
    book = {
        "id": book_id,
        "title": match["title"],
        "author": author,
        "source": match["source"],
        "source_url": page,
        "file": f"books/{fname}",
        "size": len(data),
        "added": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request": query,
    }
    m["books"].append(book)
    save_manifest(m)
    build()
    return {"status": "added", "message": f'Added "{book["title"]}" by {author}.', "query": query, "book": book}


def build():
    m = load_manifest()
    base = site_url()
    books = sorted(m["books"], key=lambda b: b["added"], reverse=True)
    now = m.get("updated") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = []
    for b in books:
        entries.append(f"""  <entry>
    <title>{escape(b['title'])}</title>
    <id>urn:kindle-inbox:{escape(b['id'])}</id>
    <updated>{b['added']}</updated>
    <author><name>{escape(b['author'])}</name></author>
    <summary>From {escape(b['source'])}: {escape(b['source_url'])}</summary>
    <link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="{escape(base + '/' + b['file'])}"/>
  </entry>""")
    opds = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
  <id>urn:kindle-inbox:{escape(base)}</id>
  <title>Kindle Inbox</title>
  <updated>{now}</updated>
  <link rel="self" type="application/atom+xml;profile=opds-catalog;kind=acquisition" href="{escape(base)}/opds.xml"/>
  <link rel="start" type="application/atom+xml;profile=opds-catalog;kind=acquisition" href="{escape(base)}/opds.xml"/>
{chr(10).join(entries)}
</feed>
"""
    with open(os.path.join(ROOT, "opds.xml"), "w", encoding="utf-8") as f:
        f.write(opds)
    rows = "\n".join(
        f'<li><a href="{html.escape(b["file"])}">{html.escape(b["title"])}</a> '
        f'<span>{html.escape(b["author"])} &middot; {html.escape(b["source"])}</span></li>' for b in books)
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kindle Inbox</title>
<style>body{{font:16px/1.5 Georgia,serif;max-width:40rem;margin:2rem auto;padding:0 1rem;color:#222;background:#fff}}
li{{margin:.4rem 0}} span{{color:#666;font-size:.9em}} a{{color:#222}}</style></head>
<body><h1>Kindle Inbox</h1>
<p>Public-domain books on their way to a Kindle. Feeds: <a href="opds.xml">OPDS</a>, <a href="manifest.json">JSON</a>.</p>
<ul>
{rows}
</ul></body></html>
"""
    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)


def write_output(result):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"status={result['status']}\n")
        f.write("message<<__EOF__\n" + result["message"] + "\n__EOF__\n")
        b = result.get("book")
        f.write(f"source_url={b['source_url'] if b else ''}\n")


def main(argv):
    if len(argv) >= 2 and argv[1] == "build":
        build()
        return 0
    if len(argv) >= 3 and argv[1] == "find":
        q = parse_request(argv[2])
        match, errors = best_match(q)
        print(json.dumps({"query": q, "match": match, "errors": errors}, indent=2))
        return 0
    if len(argv) >= 3 and argv[1] == "add":
        result = add(argv[2])
        print(json.dumps(result, indent=2))
        write_output(result)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
