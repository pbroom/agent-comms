# Public release checks

The first public commit is a sanitized source snapshot. Private development history stays local;
setup screenshots, raw runtime evidence, private conversation URLs, and account/tunnel identifiers
are excluded. The review and verification summaries are retained without those private details.

Before publication:

- All private development-history blobs were compared with known local board credentials and the
  tunnel runtime API key without printing their values; no matches were found.
- The public source and its complete publication history were scanned with Gitleaks 8.30.1,
  using redacted output; no secrets were detected.
- Known local credentials were also checked directly against every public Git blob and built
  package. No credential material, live agent registry, database, or private evidence is included.
- Personal absolute paths were replaced with portable defaults or explicit setup placeholders.
- Python tests, dashboard tests, and source/wheel builds passed.

`.gitignore` excludes local environment files, agent registries, databases, token/key files,
private evidence, and build output. Ignore rules alone do not protect already tracked files;
review staged changes and scan history before publishing future work.

To repeat the history scan:

```bash
gitleaks git . --redact --log-opts=main
```

A clean scan is evidence, not a guarantee against every possible secret format. Never commit
real credentials as examples. The application threat boundary and older-server migration caveat
are documented in DESIGN_NOTES.md.
