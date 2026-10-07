"""Portable lexical recall helpers, adapted from Rain (MIT); see NOTICE."""
from __future__ import annotations
import re
STOP = frozenset('\nчто чего чем чём кто кого кому где когда почему зачем как какой какая какие какое который которая\nкоторые которую которого было была были будет есть был быть делала делал делали случилось произошло\nпроисходило говорили говорила говорил сказала сказал писала писал обсуждали обсуждала обсуждал\nспрашивал спрашивала рассказывал рассказывала упоминал упоминала помню помнишь вспомни вспомнить\nнапомни знаешь знаю сделала сделал вчера сегодня про по в во на с со и а у за о об к от до из же ли это\nэтот эта эти того этого мы вы он она они мне меня нас них там тут вот уже ещё еще или для при без над\nпод ну бы ведь тоже также очень можно нужно надо мой моя мои моё твой его её их наш ваш себя сам сама\nсами все всё так да нет только мной мною нами тобой тобою вами\nwhat happened did do does done i we you the a an on at in about of to for was were is are there my me\nwhich who whom how when where why and or but with from by this that these those it its be been have\nhas had not no yes so if then than just also can could would should will our your their them they he\nshe his her us any some all tell told remember recall know knew said say talk talked discuss discussed\n'.split())
_TOKEN_RE = re.compile('[\\w@#-]+(?:\\.[\\w-]+)*(?:\\+[\\w-]+(?:\\.[\\w-]+)*)?')
_RUSSIAN_RE = re.compile('[а-яё]+', re.IGNORECASE)
MIN_TERM_CHARS = 3
TRANSLIT = {'хабр': 'habr', 'хабра': 'habr', 'хабре': 'habr', 'хабром': 'habr', 'гитхаб': 'github', 'гитхаба': 'github', 'гитхабе': 'github', 'ютуб': 'youtube', 'ютуба': 'youtube', 'ютубе': 'youtube', 'телеграм': 'telegram', 'телеграма': 'telegram', 'телеграме': 'telegram', 'телега': 'telegram', 'телеге': 'telegram', 'архив': 'arxiv', 'архиве': 'arxiv', 'арксив': 'arxiv', 'реддит': 'reddit', 'реддите': 'reddit', 'твиттер': 'twitter', 'твиттере': 'twitter'}

def query_tokens(query: str) -> list[str]:
    """Shared lexical units; callers keep their own stop-word/length policy."""
    return [raw.lstrip('@#') for raw in _TOKEN_RE.findall(str(query or '').lower())]

def content_terms(query: str) -> list[str]:
    """Lower-cased content words of *query*, in order, deduplicated.

    Leading ``@``/``#`` come off (a handle is matched against ``who``, which
    never carries the sigil); stop-words and one- or two-letter tokens go.
    Dotted versions, filenames and domains remain one literal term.
    """
    out: list[str] = []
    for term in query_tokens(query):
        if len(term) < MIN_TERM_CHARS or term in STOP or term in out:
            continue
        out.append(term)
        latin = TRANSLIT.get(term)
        if latin and latin not in out:
            out.append(latin)
    return out

def content_term_patterns(query: str) -> dict[str, re.Pattern]:
    """One condition per content term, with transliterations as alternatives.

    Expansion must not make «Хабре» require both «хабре» and «habr», or let
    those two spellings satisfy two independent words of a longer question.
    Keep content_terms' flat expansion for callers that only need keywords.
    """
    groups: dict[str, list[str]] = {}
    for term in content_terms(query):
        key = stem(TRANSLIT.get(term, term))
        groups.setdefault(key, []).append(term_pattern(stem(term)).pattern)
    return {key: re.compile('|'.join(dict.fromkeys(parts)), re.IGNORECASE) for key, parts in groups.items()}

def stem(term: str) -> str:
    """A prefix that survives Russian inflection («статья»/«статью» → «стать»).

    Only Russian words are cut. Latin names are not Russian inflections:
    PostGIS must not become PostgreSQL. Numbers and mixed identifiers stay whole.
    """
    term = str(term or '')
    if not _RUSSIAN_RE.fullmatch(term) or len(term) <= 4:
        return term
    return term[:-1] if len(term) <= 6 else term[:-2]

def required_matches(n_terms: int) -> int:
    """All of one or two terms; at least half (never fewer than two) of more."""
    if n_terms <= 2:
        return max(0, int(n_terms))
    return max(2, (int(n_terms) + 1) // 2)

def match_score(hay: str, stems: list[str]) -> int:
    """Count bounded terms, never arbitrary substrings of unrelated words."""
    return sum((1 for s in stems if s and term_pattern(s).search(hay)))

def term_pattern(term: str) -> re.Pattern:
    """Whole identifiers; Russian prefixes may carry Cyrillic endings.

    A leading word boundary prevents ипотека → гипотеза. A trailing one
    prevents an article ID or handle from matching a different, longer ID.
    Compile once per query term, not once for every source row.
    """
    if '.' in term:
        return re.compile('(?<!\\w)(?<!\\w[.+-])' + re.escape(term) + '(?!\\w|[.+-]\\w)', re.IGNORECASE)
    suffix = '[а-яё]*' if _RUSSIAN_RE.fullmatch(term) and len(term) >= 4 else ''
    return re.compile('(?<!\\w)' + re.escape(term) + suffix + '(?!\\w)', re.IGNORECASE)
