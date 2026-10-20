"""Questions an agent asks the user: ~/ask/<name>.json in the VM, version 1.

    {"version": 1, "questions": [{"header": "Database", "question": "Which database should the plugin use?",
      "options": [{"label": "SQLite", "description": "One file, no server."}, {"label": "MySQL"}], "multi": false}]}

The file comes from the VM, so it's untrusted: everything is checked here, cleaned to plain text, and
anything that doesn't fit is refused with a reason the chat shows.  Unknown keys are ignored."""

from __future__ import annotations

import json
import re

from .projects import clean_text

FILE_MAX = 16 * 1024   # bytes per file
FILES_MAX = 4          # files per task (more are refused)
QUESTIONS = (1, 4)
OPTIONS = (2, 4)
HEADER_MAX, QUESTION_MAX, LABEL_MAX, DESCRIPTION_MAX, OTHER_MAX = 12, 600, 80, 200, 2000
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.json")


class AskError(ValueError):
    pass


def _line(v, what: str, limit: int, required: bool = True) -> str:
    """One line of plain text: control and invisible characters out, whitespace collapsed."""
    if v is None and not required:
        return ""
    if not isinstance(v, str):
        raise AskError(f"{what} must be text")
    s = " ".join(clean_text(v, 10 * limit).split())
    if required and not s:
        raise AskError(f"{what} is empty")
    if len(s) > limit:
        raise AskError(f"{what} is longer than {limit} characters")
    return s


def parse(name, text) -> list:
    """The questions in one ~/ask file, cleaned, or AskError with the reason."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise AskError("the file name must be letters, digits, dots, dashes or underscores, ending in .json")
    if not isinstance(text, str) or len(text.encode("utf-8", "surrogatepass")) > FILE_MAX:
        raise AskError(f"the file is larger than {FILE_MAX // 1024} KiB")
    try:
        doc = json.loads(text)
    except ValueError:
        raise AskError("the file isn't valid JSON") from None
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise AskError('the file needs "version": 1')
    qs = doc.get("questions")
    if not isinstance(qs, list) or not QUESTIONS[0] <= len(qs) <= QUESTIONS[1]:
        raise AskError(f"it needs {QUESTIONS[0]} to {QUESTIONS[1]} questions")
    out = []
    for i, q in enumerate(qs, 1):
        if not isinstance(q, dict):
            raise AskError(f"question {i} isn't an object")
        opts = q.get("options")
        if not isinstance(opts, list) or not OPTIONS[0] <= len(opts) <= OPTIONS[1]:
            raise AskError(f"question {i} needs {OPTIONS[0]} to {OPTIONS[1]} options")
        if not isinstance(q.get("multi", False), bool):
            raise AskError(f'question {i}: "multi" must be true or false')
        options = []
        for j, o in enumerate(opts, 1):
            if not isinstance(o, dict):
                raise AskError(f"question {i}, option {j} isn't an object")
            options.append({"label": _line(o.get("label"), f"question {i}, option {j}: the label", LABEL_MAX),
                            "description": _line(o.get("description"), f"question {i}, option {j}: the description",
                                                 DESCRIPTION_MAX, required=False)})
        if len({o["label"].lower() for o in options}) < len(options):
            raise AskError(f"question {i} has two options with the same label")
        out.append({"header": _line(q.get("header"), f"question {i}: the header", HEADER_MAX),
                    "question": _line(q.get("question"), f"question {i}: the question", QUESTION_MAX),
                    "options": options, "multi": q.get("multi", False)})
    return out


def answers(questions: list, given) -> list:
    """Your answers, checked against the questions: for each, the labels you picked and your own words."""
    if not isinstance(given, list) or len(given) != len(questions):
        raise AskError("answer every question")
    out = []
    for i, (q, a) in enumerate(zip(questions, given), 1):
        if not isinstance(a, dict):
            raise AskError(f"answer {i} isn't an object")
        picked, other = a.get("picked", []), a.get("other") or ""
        if (not isinstance(picked, list) or not all(isinstance(p, int) and not isinstance(p, bool) for p in picked)
                or len(set(picked)) != len(picked) or any(not 0 <= p < len(q["options"]) for p in picked)):
            raise AskError(f"answer {i} picks an option that isn't there")
        other = _line(other, f"answer {i}", OTHER_MAX, required=False)
        if not picked and not other:
            raise AskError(f"question {i} has no answer yet")
        if not q["multi"] and len(picked) + bool(other) > 1:
            raise AskError(f"question {i} takes one answer")
        out.append({"picked": [q["options"][p]["label"] for p in sorted(picked)], "other": other})
    return out


def answer_message(name: str, questions: list, picked: list) -> str:
    """The message the agent gets: each question quoted, with the answer under it."""
    parts = [f"My answers to your questions in ~/ask/{name}:"]
    for q, a in zip(questions, picked):
        said = ", ".join(a["picked"] + ([f"in my own words: {a['other']}"] if a["other"] else []))
        parts.append(f"> {q['header']}: {q['question']}\n{said}")
    return "\n\n".join(parts)
