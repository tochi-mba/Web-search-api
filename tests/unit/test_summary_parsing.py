"""Models are asked for JSON. Not all of them comply."""

from app.services.llm.summarizer import parse_summary_payload


def test_plain_json_is_parsed():
    summary, points = parse_summary_payload(
        '{"executive_summary": "It is fine.", "key_points": ["a", "b"]}'
    )
    assert summary == "It is fine."
    assert points == ["a", "b"]


def test_fenced_json_is_parsed():
    summary, points = parse_summary_payload(
        '```json\n{"executive_summary": "Fenced.", "key_points": ["x"]}\n```'
    )
    assert summary == "Fenced."
    assert points == ["x"]


def test_bare_fence_without_a_language_is_parsed():
    summary, _ = parse_summary_payload('```\n{"executive_summary": "Bare fence."}\n```')
    assert summary == "Bare fence."


def test_json_with_a_chatty_preamble_is_parsed():
    summary, points = parse_summary_payload(
        'Sure! Here is the summary:\n{"executive_summary": "Extracted.", "key_points": ["p"]}'
    )
    assert summary == "Extracted."
    assert points == ["p"]


def test_summary_key_alias_is_accepted():
    summary, _ = parse_summary_payload('{"summary": "Alias key."}')
    assert summary == "Alias key."


def test_missing_key_points_yields_an_empty_list():
    summary, points = parse_summary_payload('{"executive_summary": "No points."}')
    assert summary == "No points."
    assert points == []


def test_non_list_key_points_are_ignored():
    _, points = parse_summary_payload('{"executive_summary": "S", "key_points": "not a list"}')
    assert points == []


def test_key_points_are_stringified_and_stripped():
    _, points = parse_summary_payload('{"executive_summary": "S", "key_points": ["  a  ", 42, ""]}')
    assert points == ["a", "42"]


def test_prose_response_becomes_the_summary():
    summary, points = parse_summary_payload("The model just wrote a paragraph instead.")
    assert summary == "The model just wrote a paragraph instead."
    assert points == []


def test_malformed_json_falls_back_to_prose():
    text = '{"executive_summary": "unterminated'
    assert parse_summary_payload(text)[0] == text


def test_json_array_falls_back_to_prose():
    assert parse_summary_payload('["not", "an", "object"]')[0] == '["not", "an", "object"]'


def test_json_object_with_an_empty_summary_falls_back_to_prose():
    text = '{"executive_summary": "   "}'
    assert parse_summary_payload(text)[0] == text


def test_json_object_with_a_non_string_summary_falls_back_to_prose():
    text = '{"executive_summary": 42}'
    assert parse_summary_payload(text)[0] == text


def test_empty_response_yields_nothing():
    assert parse_summary_payload("") == ("", [])
    assert parse_summary_payload("   \n  ") == ("", [])
