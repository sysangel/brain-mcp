"""Description one-liner: first sentence, or a word-boundary cap with an ellipsis."""

from brain.remember import derive_description

_HOSTED = (
    "As of 2026-10-19, brain is hosted: Supabase Postgres "
    "(project abcdefghijklmnopqrst, schema brain) is the source of truth "
    "and the MCP runs on Cloud Run at https://brain-xxxxx.a.run.app/mcp"
)


def test_first_sentence_is_kept_whole():
    assert derive_description("Hello world. The rest is longer.") == "Hello world."


def test_long_fact_caps_on_a_word_boundary_with_ellipsis():
    """A 120-char slice of this fact ends on 'truth' and hides that the
    sentence continues. The cap must not look like a finished sentence."""
    description = derive_description(_HOSTED)
    assert len(description) <= 120
    assert description.endswith("…")
    assert "and the MCP" not in description
    body = description[:-1]
    assert _HOSTED.startswith(body)
    assert _HOSTED[len(body)] == " "
    assert not description.endswith("truth")


def test_cap_does_not_split_a_word_when_the_limit_lands_inside_one():
    fact = "word " * 30 + "tail"
    description = derive_description(fact.strip())
    assert description.endswith("…")
    assert len(description) <= 120
    assert not description[:-1].endswith("wor")
