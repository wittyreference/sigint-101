# ABOUTME: Contract test over shipped predict prompts: any prompt that names options must name
# ABOUTME: every chip's option and in chip order, so a learner tapping by position never misses.

import re
from pathlib import Path

import pytest

from sigint101.lessons import load_lessons

PACK_DIR = Path(__file__).resolve().parent.parent / "lesson_packs"
LESSONS = load_lessons(PACK_DIR)

# Function words excluded when matching an option phrase in a prompt to a chip's label.
_STOPWORDS = frozenset("""
a about all an and any are as at be but by can could do does down for from get how in into is it
its just like look may might more most must no not of off on one or our out over so some
stay than that the their them there these they this those to too up very was we were what when
where which who why will with would you your
""".split())

_TOKEN = re.compile(r"[a-z0-9]+")
_ALL_CAPS = re.compile(r"\b[A-Z]{3,}\b")
_PAREN_DASH = re.compile(r"\s+—[^—]+—\s+")
_INTRO_VERB = re.compile(
    r"^(?:[^,]+,\s+)+((?:will|do|does|is|are|can|could|would|should|what|which|how)\b.*)$",
    re.IGNORECASE,
)


def _stem(word: str) -> str:
    """Normalize prompt verbs to match chip labels (rise/rises, fail/fails)."""
    if len(word) >= 5 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    if len(word) >= 5 and word.endswith("ed"):
        return word[:-2]
    if len(word) >= 6 and word.endswith("ing"):
        return word[:-3]
    return word


def _stems(text: str) -> set[str]:
    return {_stem(tok) for tok in _TOKEN.findall(text.lower()) if tok not in _STOPWORDS}


def _question_sentence(prompt: str) -> str:
    """The question sentence of a predict prompt (the sentence ending with '?'), without asides."""
    cleaned = _PAREN_DASH.sub(" ", prompt.strip())
    qmark = cleaned.rfind("?")
    if qmark == -1:
        return cleaned
    before = cleaned[:qmark]
    parts = re.split(r"(?<=[.!])\s+", before)
    return parts[-1].strip()


def _extract_or_options(question: str) -> list[str] | None:
    """Split an 'A, B, or C' / 'A or B' option list in a question, or None for a bare question."""
    if not re.search(r"\bor\b", question, re.IGNORECASE):
        return None
    clause = question
    # If an em-dash or colon introduces the options, inspect only the option list after it.
    for sep in ("—", ":"):
        idx = clause.rfind(sep)
        if idx != -1 and re.search(r"\bor\b", clause[idx + 1:], re.IGNORECASE):
            clause = clause[idx + 1:].strip()
            break
    else:
        match = _INTRO_VERB.match(clause)
        if match:
            clause = match.group(1)

    # Split the final ', or ' or ' or ', then any earlier comma-separated options.
    or_matches = list(re.finditer(r"(?:\s*,\s*|\s+)or\s+", clause, re.IGNORECASE))
    if not or_matches:
        return None
    last_or = or_matches[-1]
    head = clause[:last_or.start()].strip()
    tail = clause[last_or.end():].strip()
    items = [part.strip() for part in head.split(",") if part.strip()]
    if tail:
        items.append(tail)
    return items


def prompt_option_mismatch(prompt: str, choices: list[dict]) -> str | None:
    """Return a reason if `prompt` names options that miss a chip or break chip order, else None."""
    labels = [choice["label"] for choice in choices]
    chip_stems = [_stems(label) for label in labels]
    question = _question_sentence(prompt)

    options = _extract_or_options(question)
    if options is not None:
        if len(options) != len(choices):
            return (
                f"prompt names {len(options)} options ({options}) "
                f"for {len(choices)} chips ({labels})"
            )
        for index, option in enumerate(options):
            opt_stems = _stems(option)
            scores = [len(opt_stems & stems) for stems in chip_stems]
            best = max(scores, default=0)
            if best == 0 or scores[index] < best:
                return (
                    f"option {index + 1} ({option!r}) does not match chip {index + 1} "
                    f"({labels[index]!r}); chip labels are {labels}"
                )
        return None

    # Guard ALL-CAPS option lists that skip 'or' (for example 'BIGGER / SAME / SMALLER').
    caps_hits = []
    for token in _ALL_CAPS.findall(question):
        stemmed = _stem(token.lower())
        if stemmed in _STOPWORDS:
            continue
        matched = [i for i, stems in enumerate(chip_stems) if stemmed in stems]
        if len(matched) == 1 and (not caps_hits or caps_hits[-1] != matched[0]):
            caps_hits.append(matched[0])
    if len(set(caps_hits)) >= 2 and caps_hits != list(range(len(choices))):
        expected = list(range(len(choices)))
        return f"capitalized options match chip order {caps_hits}, expected {expected}"
    return None


# --- rule unit tests: prove both directions (restatements fail, fair rewordings pass) ----------

_CHIPS = [
    {"key": "wider", "label": "Wider"},
    {"key": "same", "label": "Same width"},
    {"key": "narrower", "label": "Narrower"},
]


@pytest.mark.parametrize("prompt", [
    "Will the weaker stripe look NARROWER, the SAME width, or WIDER? Lock it in.",
    "Will it look narrower, wider, or the same width? Lock it in.",
    "Will it look wider or narrower? Lock it in.",
    "Will it look NARROWER / SAME / WIDER? Lock it in.",
    "Compare the two — wider, dimmer, or narrower? Lock it in.",
])
def test_mismatched_prompt_restatements_fail(prompt):
    assert prompt_option_mismatch(prompt, _CHIPS) is not None


@pytest.mark.parametrize("prompt", [
    "How wide will the dimmer stripe look? Lock it in.",
    "Will the dimmer stripe look WIDER, the SAME width, or NARROWER? Lock it in.",
    "Will the dimmer stripe look wider, the same width, or narrower? Lock it in.",
    "Is a SECOND stripe visible beside it? Lock it in.",
])
def test_aligned_or_bare_prompt_rewordings_pass(prompt):
    assert prompt_option_mismatch(prompt, _CHIPS) is None


# --- shipped lesson packs ----------------------------------------------------------------------

@pytest.mark.parametrize("lesson_id", sorted(LESSONS))
def test_every_predict_prompt_matches_its_chips(lesson_id):
    lesson = LESSONS[lesson_id]
    problem = prompt_option_mismatch(lesson.stages["predict"], lesson.choices)
    assert problem is None, f"lesson {lesson_id}: {problem}"
