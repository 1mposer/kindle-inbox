# kindle-inbox

**Type a book title on your phone. It shows up on your Kindle.**

Open an issue with a title. A GitHub Action finds a free public-domain EPUB
(Standard Ebooks first, then Project Gutenberg), commits it here, and publishes
a feed on GitHub Pages. The `inboxsync` KOReader plugin downloads new books
into the Kindle's `Inbox` folder.

## Send a book

From the GitHub app or a browser, open
[New issue > Send a book](https://github.com/1mposer/kindle-inbox/issues/new?template=book.yml),
type the title, submit. A plain issue whose title is the book title also works.

The Action replies on the issue within about a minute and closes it. Only the
repository owner's issues are processed.

## On the Kindle

Copy `inboxsync.koplugin` to `koreader/plugins/` and restart KOReader.
Books land in `<home folder>/Inbox`. The plugin checks:

- when KOReader starts, if Wi-Fi is connected
- when the Kindle wakes up (with *Network > Restore Wi-Fi after resume* on)
- every 30 minutes while awake
- on demand: *Tools > Book inbox > Check now*

Nothing downloads while the Kindle is asleep: the CPU is suspended and KOReader
turns Wi-Fi off. Wake it up and the book arrives within a minute.

You can also browse the feed in KOReader's OPDS catalog:
`https://1mposer.github.io/kindle-inbox/opds.xml`

## Your own inbox

1. *Use this template* to make your own copy (public, so Pages and the Kindle need no token).
2. *Settings > Pages > Source: GitHub Actions*.
3. On the Kindle, put your feed URL in `koreader/plugins/inboxsync.koplugin/feed_url.txt`:
   `https://<you>.github.io/<repo>/manifest.json`

Requests are public issues, so anyone can see what you asked for.

## Files

```
scripts/fetch_book.py     search, download, manifest, OPDS (stdlib only)
.github/workflows/        issue -> book -> commit -> Pages
inboxsync.koplugin/       KOReader plugin
manifest.json, opds.xml   generated feeds
books/                    the EPUBs
```

Only public-domain and freely licensed sources are used.
