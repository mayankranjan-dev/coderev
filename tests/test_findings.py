import pytest

from reviewer import findings as F
from reviewer.findings import Finding, parse_review


def one(line):
    result = parse_review(line)
    assert len(result.findings) == 1, (result.findings, result.failures)
    return result.findings[0]


# --- canonical contract -------------------------------------------------------


def test_canonical_line():
    f = one("CRITICAL: pkg/mod.py:40-42 Query built from user input. Correction: Use parameters.")
    assert f == Finding("CRITICAL", "pkg/mod.py", 40, 42, "Query built from user input.", "Use parameters.")


def test_single_line_number_sets_both_ends():
    f = one("WARNING: app.py:7 Missing check Correction: Add it")
    assert (f.line_start, f.line_end) == (7, 7)


def test_correction_stays_with_its_finding():
    result = parse_review(
        "CRITICAL: a.py:1 first Correction: fix one\nNITPICK: b.py:2 second Correction: fix two"
    )
    assert [f.correction for f in result.findings] == ["fix one", "fix two"]


def test_missing_correction_is_none():
    assert one("NITPICK: a.py:3 Rename this").correction is None


def test_aliases_for_matching_spec():
    f = one("WARNING: a.py:3 msg Correction: c")
    assert f.path == "a.py" and f.text == "msg" and f.located


# --- tolerated variants -------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "**CRITICAL:** a.py:1 msg Correction: c",
        "**CRITICAL**: a.py:1 msg Correction: c",
        "__CRITICAL__: a.py:1 msg Correction: c",
        "`CRITICAL`: a.py:1 msg Correction: c",
        "[CRITICAL] a.py:1 msg Correction: c",
        "1. CRITICAL: a.py:1 msg Correction: c",
        "12) CRITICAL: a.py:1 msg Correction: c",
        "- CRITICAL: a.py:1 msg Correction: c",
        "* CRITICAL: a.py:1 msg Correction: c",
        "\u2022 CRITICAL: a.py:1 msg Correction: c",
        "- **CRITICAL:** a.py:1 msg Correction: c",
        "critical: a.py:1 msg Correction: c",
        "Critical: a.py:1 msg Correction: c",
        "CRITICAL - a.py:1 msg Correction: c",
        "   CRITICAL:   a.py:1 msg Correction: c   ",
    ],
)
def test_tag_variants(line):
    f = one(line)
    assert (f.severity, f.file, f.line_start, f.message, f.correction) == ("CRITICAL", "a.py", 1, "msg", "c")


@pytest.mark.parametrize(
    "line,path,start,end",
    [
        ("WARNING: `pkg/mod.py:40-42` msg", "pkg/mod.py", 40, 42),
        ("WARNING: (pkg/mod.py:41) msg", "pkg/mod.py", 41, 41),
        ("WARNING: pkg/mod.py:L40-L42 msg", "pkg/mod.py", 40, 42),
        ("WARNING: pkg/mod.py:40\u201342 msg", "pkg/mod.py", 40, 42),
        ("WARNING: pkg/mod.py line 41 msg", "pkg/mod.py", 41, 41),
        ("WARNING: in pkg/mod.py, lines 40-42 msg", "pkg/mod.py", 40, 42),
        ("WARNING: b/pkg/mod.py:41 msg", "b/pkg/mod.py", 41, 41),
        ("WARNING: pkg\\mod.py:41 msg", "pkg\\mod.py", 41, 41),
        ("WARNING: pkg/mod.py:42-40 msg", "pkg/mod.py", 42, 40),
        ("WARNING: pkg/mod.py:0 msg", "pkg/mod.py", 0, 0),
        ("WARNING: snippet:3 msg", "snippet", 3, 3),
        ("WARNING: README:3 msg", None, None, None),
    ],
)
def test_location_variants(line, path, start, end):
    f = one(line)
    assert (f.file, f.line_start, f.line_end) == (path, start, end)


def test_parser_keeps_raw_numbers_for_the_matcher_to_judge():
    # Reversed and zero ranges are matcher decisions (MATCHING_SPEC 4.3),
    # so the parser must not "fix" them.
    assert (one("CRITICAL: a.py:9-3 m").line_start, one("CRITICAL: a.py:9-3 m").line_end) == (9, 3)


def test_unlocated_finding_is_kept():
    f = one("CRITICAL: Hardcoded credential in config. Correction: Read it from the environment.")
    assert f.file is None and f.line_start is None
    assert f.message.startswith("Hardcoded")
    assert not f.located


def test_path_without_line_is_kept_unlocated():
    f = one("WARNING: pkg/mod.py: broad except Correction: narrow it")
    assert f.file == "pkg/mod.py" and f.line_start is None and f.message == "broad except"


def test_dotted_name_is_not_mistaken_for_a_path():
    f = one("CRITICAL: subprocess.run: shell=True with user input Correction: pass a list")
    assert f.file is None


def test_bold_correction_label():
    assert one("NITPICK: a.py:1 m **Correction:** c").correction == "c"


# --- sentinel, empty, malformed ----------------------------------------------


@pytest.mark.parametrize("text", ["NO_ISSUES_FOUND", "  no_issues_found  ", "**NO_ISSUES_FOUND**", "NO ISSUES FOUND."])
def test_sentinel(text):
    result = parse_review(text)
    assert result.status == F.NO_ISSUES and result.findings == [] and result.failures == []


@pytest.mark.parametrize("text", ["", "   ", "\n\n\t\n", None, 42, b"CRITICAL: a.py:1 m"])
def test_empty_or_non_string(text):
    result = parse_review(text)
    assert result.status == F.EMPTY and result.findings == []


def test_garbage_is_malformed_and_recorded():
    result = parse_review("Here is my review!\nThe code looks fine overall.\n\x00\x01 ~~~")
    assert result.status == F.MALFORMED
    assert [fail.line_number for fail in result.failures] == [1, 2, 3]
    assert all(fail.reason == "no severity tag" for fail in result.failures)


def test_rubric_echo_is_not_a_finding():
    result = parse_review("CRITICAL = security vulnerabilities or correctness bugs")
    assert result.findings == [] and result.status == F.MALFORMED


def test_prose_starting_with_severity_word_is_not_a_finding():
    result = parse_review("Critical issues were found in this change")
    assert result.findings == []
    assert result.failures[0].reason == "severity tag without separator"


def test_unknown_severity_is_a_failure():
    result = parse_review("INFO: a.py:1 something")
    assert result.findings == [] and len(result.failures) == 1


def test_tag_without_message_is_a_failure():
    result = parse_review("WARNING:")
    assert result.findings == [] and result.failures[0].reason == "severity tag with no message"


def test_correction_on_its_own_line_is_a_failure_not_attached():
    result = parse_review("CRITICAL: a.py:1 bug\nCorrection: fix")
    assert result.findings[0].correction is None
    assert len(result.failures) == 1


def test_mixed_output_keeps_findings_and_records_noise():
    text = "Sure, here is the review:\n```\nCRITICAL: a.py:1 bug Correction: fix\n```\nThanks!"
    result = parse_review(text)
    assert result.status == F.OK
    assert len(result.findings) == 1
    assert [fail.line_number for fail in result.failures] == [1, 5]


def test_findings_beat_sentinel():
    result = parse_review("NO_ISSUES_FOUND\nWARNING: a.py:2 m")
    assert result.status == F.OK and result.sentinel_seen


def test_counts():
    result = parse_review("CRITICAL: a.py:1 x\ncritical: a.py:2 y\nWARNING: z\nNITPICK: a.py:3 w")
    assert result.counts() == {"critical_count": 2, "warning_count": 1, "nitpick_count": 1}


def test_very_long_failure_text_is_truncated():
    result = parse_review("x" * 5000)
    assert len(result.failures[0].text) == F.MAX_FAILURE_TEXT


def test_windows_newlines():
    result = parse_review("CRITICAL: a.py:1 x Correction: y\r\nNITPICK: b.py:2 z\r\n")
    assert [f.file for f in result.findings] == ["a.py", "b.py"]
    assert result.findings[0].correction == "y"


def test_round_trip_dict():
    f = Finding("WARNING", "a.py", 1, 2, "m", "c")
    assert Finding.from_dict(f.to_dict()) == f


def test_parser_never_raises_on_adversarial_input():
    nasty = "\n".join(["CRITICAL:" + "(" * 2000, "**" * 3000, "WARNING: " + ":" * 4000 + "1-2", "1." * 1000])
    parse_review(nasty)  # must not raise
