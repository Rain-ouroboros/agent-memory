"""Best-effort secret-pattern redaction, adapted from Rain; not anonymization."""
import re

_SECRET_PATTERNS = (re.compile('sk-[A-Za-z0-9_-]{12,}'), re.compile('\\bAKIA[0-9A-Z]{16}\\b'), re.compile('\\beyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}\\b'), re.compile('\\b\\d{6,}:[A-Za-z0-9_-]{25,}\\b'), re.compile('(?i)\\bbearer\\s+[A-Za-z0-9._-]{12,}'), re.compile('(?i)(token|api[_-]?key|access[_-]?key|private[_-]?key|bot[_-]?token|password|passphrase|secret|mnemonic|seed[_ ]?phrase|authorization)\\s*[:=]\\s*[^\\s,;]+'))

def redact_secret_like(text: str) -> str:
    """Replace secret-looking spans (keys, tokens, bearer headers) in text."""
    redacted = str(text or '')
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub('[REDACTED_SECRET]', redacted)
    return redacted
