"""Second pass -- sweep the already-redacted page for PII that got through.

The first pass (mask.py) removes the values it was *handed*: the Salesforce
Contact's name/phone/email, plus whatever server.detect_pii() recognised in
the resume text. That is a closed list, and three things routinely fall
outside it:

  * a second number the candidate typed into the resume and nowhere else --
    "Alternate Mobile", "Res.", a WhatsApp number in the footer;
  * a personal address alongside the work one, likewise resume-only;
  * a value the first pass *found* but could not *place*. Detection reads
    page.get_text(); placement uses page.search_for(), which is an exact
    substring match. When a PDF kerns an address into two word boxes, the
    text says "rahul.sharma@ gmail.com" and search_for("rahul.sharma@gmail.com")
    returns nothing, so the address is detected, reported, and left on the page.

This module closes all three. It runs against the page *after*
apply_redactions(), so it only ever sees what actually survived, and it works
off the word boxes rather than off a search string -- which is what fixes the
third case, since a rect can be assembled from several words.

Two non-text leaks are handled here as well, because they are the same defect
(PII still in the delivered file) and neither is visible on the page:

  * a mailto:/tel: link annotation, whose URI keeps the address verbatim
    after the glyphs under it have been deleted;
  * the document Info dictionary, where Word writes /Author and /Title from
    the original filename -- "Rahul Sharma CV 2024.docx".

Geometry stays in mask.py. This module reports where a hit is and which text
line it belongs to; the caller clips it to that line and absorbs the label in
front of it, exactly as it does for a first-pass hit.
"""
from __future__ import annotations

import fitz

from . import pii


def _line_layout(words: list) -> list[tuple[tuple[int, int], str, list]]:
    """Rebuild each text line as a string, keeping the word each span came from.

    Returns (line_key, line_text, [(start, end, word), ...]) per line, with
    words joined by a single space in reading order. Working line-by-line
    rather than over page.get_text() is what keeps a scan honest: a phone
    candidate can never form across two stacked lines of a two-column layout,
    and the text to a hit's left is genuinely the label in front of it.
    """
    lines: dict[tuple[int, int], list] = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)

    out = []
    for key, group in lines.items():
        group = sorted(group, key=lambda w: w[0])
        parts: list[str] = []
        spans: list[tuple[int, int, tuple]] = []
        pos = 0
        for w in group:
            if parts:
                parts.append(" ")
                pos += 1
            spans.append((pos, pos + len(w[4]), w))
            pos += len(w[4])
            parts.append(w[4])
        out.append((key, "".join(parts), spans))
    return out


def find_residual_rects(page: fitz.Page,
                        words: list | None = None) -> list[tuple[fitz.Rect, tuple[int, int]]]:
    """Every (rect, line_key) still holding a phone number or email address.

    Names are deliberately not swept for. There is no way to tell a
    candidate's name from any other capitalised words on the page, so a name
    only ever comes from the Contact record -- guessing at one here would
    blank out employers and universities.
    """
    if words is None:
        words = page.get_text("words")

    out: list[tuple[fitz.Rect, tuple[int, int]]] = []
    for key, text, spans in _line_layout(words):
        for start, end, _kind in pii.scan_residual(text):
            hit = [w for s, e, w in spans if s < end and start < e]
            if not hit:
                continue
            out.append((fitz.Rect(min(w[0] for w in hit), min(w[1] for w in hit),
                                  max(w[2] for w in hit), max(w[3] for w in hit)), key))
    return out


#: URI schemes that are a contact detail in their own right. The glyphs under
#: such a link are redacted like any other text, but the annotation survives
#: apply_redactions() with the address still in its /A /URI entry -- readable
#: by anything that opens the file, invisible to anyone who looks at the page.
_CONTACT_SCHEMES = ("mailto:", "tel:", "callto:", "sms:", "fax:", "whatsapp:")


def scrub_links(page: fitz.Page) -> int:
    """Delete link annotations that carry a phone number or email address."""
    removed = 0
    for link in list(page.get_links()):
        uri = link.get("uri") or ""
        if not uri:
            continue
        if uri.lower().startswith(_CONTACT_SCHEMES) or pii.scan_residual(uri):
            page.delete_link(link)
            removed += 1
    return removed


#: Info-dictionary keys that carry candidate PII. Word fills /title and
#: /author from the document properties and the original filename, so a
#: masked resume routinely shipped with "Rahul Sharma" in its title bar.
#: /producer and /creator name the software and are left alone.
_PII_METADATA_KEYS = ("title", "author", "subject", "keywords")


def scrub_metadata(doc: fitz.Document) -> int:
    """Blank the PII-bearing document metadata. Returns the fields cleared."""
    meta = doc.metadata or {}
    dirty = [k for k in _PII_METADATA_KEYS if meta.get(k)]
    if dirty:
        doc.set_metadata({**{k: v for k, v in meta.items()
                             if k not in ("format", "encryption")},
                          **{k: "" for k in _PII_METADATA_KEYS}})
    # XMP carries its own copy of the same fields, and a reader that finds
    # both prefers XMP -- clearing only the Info dictionary leaves the name
    # in the file.
    try:
        doc.del_xml_metadata()
    except Exception:
        pass
    return len(dirty)
