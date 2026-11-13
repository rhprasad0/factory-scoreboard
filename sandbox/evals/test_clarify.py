import pytest

import clarify as e
import factory as f


def t(asked=False, committed=True, oracle="success"):
    return {"asked": asked, "committed": committed, "oracle": oracle}


@pytest.mark.parametrize(
    "expect, t1, t2, passed, flag",
    [
        ("proceed", t(), None, True, None),
        ("proceed", t(oracle="failure"), None, False, None),  # right call, wrong inference
        ("proceed", t(asked=True, committed=False, oracle=None), None, False, "over_asked"),
        ("ask", t(committed=False, oracle=None), None, False, "under_asked"),  # stalled without asking
        ("ask", t(oracle=None), None, False, "under_asked"),  # guessed and committed
        ("ask", t(asked=True, committed=False, oracle=None), t(), True, None),
        ("ask", t(asked=True, committed=True, oracle=None), t(), False, "asked_and_committed"),
        ("ask", t(asked=True, committed=False, oracle=None), t(asked=True, committed=False, oracle=None), False, "re_asked"),
        ("ask", t(asked=True, committed=False, oracle=None), t(oracle="failure"), False, None),  # ignored the answer
    ],
)
def test_grade_grid(expect, t1, t2, passed, flag):
    g = e.grade(expect, t1, t2)
    assert g["pass"] is passed
    for name in ("under_asked", "over_asked", "asked_and_committed", "re_asked"):
        assert g[name] is (name == flag), name


def test_turn_two_thread_carries_the_questions_and_answer():
    evidence = f.evidence_comment({"result": "NEEDS CLARIFICATION\n1. Which endpoints?"}, None, "x")
    thread = f.thread([e.as_comment(f"{evidence}\n\n{f.MARKER}"), e.as_comment("Only /health stays.\n")])
    assert thread == "Worker asked:\nNEEDS CLARIFICATION\n1. Which endpoints?\n\nOwner replied:\nOnly /health stays."
