# Audit checks

## Severity

- `fatal`: the EPUB cannot be reliably parsed or opened.
- `error`: released reading content may be missing, duplicated, reordered, corrupt, or unreachable.
- `warning`: suspicious content or metadata needs review but is not proven corrupt.
- `info`: inventory or positive validation evidence.

## Generic EPUB checks

Check ZIP integrity, an uncompressed first `mimetype` entry, container/package parsing, manifest and spine targets, navigation targets and fragments, duplicate XHTML IDs, readable internal provenance markers, exact repeated paragraphs, suspicious sentence endings, and navigation numbering.

Sentence-boundary warnings are heuristic. Do not call a quotation, heading, list item, deliberate fragment, or page-spanning paragraph defective without surrounding context.

## book2epub deep checks

Use the latest assembly run. Compare active manuscript-unit provenance with corrected source blocks after excluding only furniture recorded by assembly reconciliation. Report missing ownership, duplicate ownership, and `(page, block ordinal)` reversals. Confirm that EPUB body text represents the assembly stream, allowing heading text to be replaced by the accepted structure title at the same unit anchor.

Provenance is not coverage. For every unit claiming multiple source blocks, verify normalized text fragments from each claimed block occur in the unit. Flag claims with substantial absent text.

When structure versions exist, distinguish agent inspection turns from rejected proposals. Inspection is expected; a final `failed_validation` status or hard structure findings are release-blocking. Verify that every assembly unit ID occurs in exactly one content XHTML resource and that front/main/back partition boundaries do not overlap.
