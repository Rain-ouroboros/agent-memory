"""Rule-based kind classification, adapted from Rain; a heuristic, not a truth check."""
import re

_DECISION_RE = re.compile('\\b(decid\\w*|reshi\\w*|реши\\w*|chose|chosen|will|going to|plan to|agreed)\\b', re.IGNORECASE)

_PREFERENCE_RE = re.compile("\\b(prefer\\w*|предпоч\\w*|like|dislike|always|never|don't|avoid)\\b", re.IGNORECASE)

_BELIEF_RE = re.compile('\\b(believ\\w*|думаю|i think|suspect|expect|assume)\\b', re.IGNORECASE)

_EVENT_RE = re.compile('(\\b\\d{4}-\\d{2}-\\d{2}\\b|\\bmet\\b|\\bhappened\\b|\\bshipped\\b|\\bdeployed\\b|\\bcalled\\b)', re.IGNORECASE)

def classify_kind(text: str) -> str:
    t = str(text or '')
    if _DECISION_RE.search(t):
        return 'decision'
    if _PREFERENCE_RE.search(t):
        return 'preference'
    if _BELIEF_RE.search(t):
        return 'belief'
    if _EVENT_RE.search(t):
        return 'event'
    return 'fact'
