"""Portable lexical recall helpers, adapted from Rain (MIT); see NOTICE."""
from __future__ import annotations
from itertools import islice
import re
from agent_memory.terms import content_term_patterns, required_matches
MAX_SCAN_CHARS = 65536
MAX_QUERY_CHARS = 1024
MAX_TERMS = 16
MAX_MATCHES_PER_TERM = 128
_PREFIX = re.compile('(?:\\s*\\[[^\\]\\n]*\\]\\s*)*(?:[\\w@.-]{1,64}:\\s*)?')

def query_excerpt(text: str, query: str, limit: int=2000, *, preserve_head_chars: int=0, min_passage_chars: int=128) -> str:
    """A bounded, verbatim passage, or the byte-compatible legacy prefix.

    Selection is lexical, not a claim that omitted text is irrelevant. No
    query, no additional coverage, an oversized prefix or an exhausted scan
    leaves the old view intact. Only the final display calls this function.
    A caller may keep an opening verbatim (e.g. a KB title and caveats).
    Matches inside that retained opening do not block recovery of a passage.
    """
    text, limit = (str(text or ''), max(0, int(limit)))
    legacy = text[:limit]
    if len(text) <= limit or limit < 128 or (not query):
        return legacy
    patterns = dict(islice(content_term_patterns(str(query)[:MAX_QUERY_CHARS]).items(), MAX_TERMS))
    terms = list(patterns)
    if not terms:
        return legacy
    scan = text[:MAX_SCAN_CHARS]
    preserve_head_chars = max(0, int(preserve_head_chars))
    prefix_end = _PREFIX.match(scan).end()
    if scan[prefix_end:].lstrip().startswith('['):
        return legacy
    prefix_end = max(prefix_end, preserve_head_chars)
    available = limit - prefix_end - 80
    if available < max(64, min(128, int(min_passage_chars))):
        return legacy
    matches = [(m.start(), m.end(), idx) for idx, pattern in enumerate(patterns.values()) for m in islice(pattern.finditer(scan), MAX_MATCHES_PER_TERM) if m.start() >= preserve_head_chars]
    if any((end <= limit for _, end, _ in matches)):
        return legacy
    anchors = {idx for idx, term in enumerate(terms) if '.' in term}
    starts = {max(prefix_end, start - min(240, available // 4)) for start, _, _ in matches}
    best, best_count = (None, 0)
    for start in sorted(starts):
        end = min(len(scan), start + available)
        if start > prefix_end and (not text[start - 1].isspace()):
            boundary = re.search('\\s', text[start:min(start + 80, end)])
            if not boundary:
                continue
            start += boundary.end()
        if end < len(text) and (not text[end].isspace()):
            boundary = list(re.finditer('\\s', text[max(start, end - 80):end]))
            if not boundary:
                continue
            end = max(start, end - 80) + boundary[-1].start()
        found = {idx for lo, hi, idx in matches if start <= lo and hi <= end}
        eligible = bool(anchors & found) if anchors else len(found) >= required_matches(len(terms))
        if eligible and len(found) > best_count:
            best, best_count = ((start, end), len(found))
    if best is None:
        return legacy
    start, end = best
    marker = f'[excerpt chars {start}:{end}/{len(text)}; context omitted] '
    result = text[:prefix_end] + marker + '… ' + text[start:end] + (' …' if end < len(text) else '')
    return result if len(result) <= limit else legacy
