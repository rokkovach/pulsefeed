# Security boundaries

Provider and admin credentials are server-side environment variables. `.env` files, local databases, cookies, certificates, logs, downloaded models and raw validation artifacts are excluded from Git. Only reviewed screenshots and allowlisted handoff files belong in the public repository.

Before committing, stage the intended files and run `python3 scripts/check-release.py`. It scans the staged bytes and nested archive/document members, rejects private runtime paths, and detects common credential formats. For an operator check, add one or more `--known-secret-file` arguments pointing to ignored private JSON or environment files; matching values are never printed. Pattern scanning cannot prove the absence of every unknown secret, so review the staged diff and images as well.

The app validates RSS destinations and redirects, bounds fetch size and time, checks ownership and expiry, protects mutations with CSRF, and reserves shared inference allowance before model calls. Unknown provider acceptance freezes dispatch instead of replaying an uncertain request. Model workers validate fixed jobs and do not expose provider credentials to visitors.

The demo has deliberate limits: one app replica/process, SQLite, anonymous visitor sessions and a single active global batch. Remaining upstream dependency findings and the scope of prior scans are recorded in [the remediation report](docs/dependency-remediation-2026-10-20.md). Do not post credentials or private visitor data in a public issue. Report a suspected vulnerability privately to the repository owner first.
