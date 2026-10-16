---
name: epub-quality-audit
description: Audit generated EPUB quality for missing, duplicated, truncated, or out-of-order content, malformed navigation, and package errors; optionally compare against a book2epub project database and source-page evidence.
---

# EPUB Quality Audit

Use this skill when a generated EPUB must be assessed before release. The EPUB is the object under review; when a book2epub project directory is supplied, corrected blocks and scanned pages are the source of truth.

## Workflow

1. Run `scripts/audit_epub.py EPUB [--project-dir PROJECT] --json-output REPORT.json --markdown-output REPORT.md`.
2. Read every `fatal` and `error` finding. Inspect the cited database blocks, units, XHTML locations, and source scans before drawing conclusions about ambiguous text.
3. Distinguish container validity from content validity. A valid ZIP/XML package can still fail because text is missing, duplicated, truncated, or reordered.
4. Visually inspect every source page cited by a severe content finding. Do not claim whole-book visual verification unless every page was reviewed.
5. Report the release recommendation (`pass`, `pass_with_warnings`, or `fail`), the most consequential findings, and exact evidence locations.

Do not repair files unless the user asks. Never treat provenance alone as proof of coverage: source text must actually be represented. Read [references/checks.md](references/checks.md) when interpreting findings or extending the checker.

