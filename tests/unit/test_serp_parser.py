import pytest

from app.core.errors import SearchBlockedError
from app.services.search.serp_parser import (
    BlockReason,
    detect_block,
    parse_google_serp,
    unwrap_google_redirect,
)


def test_parses_modern_layout(load_html):
    results = parse_google_serp(load_html("google_serp_modern.html"))
    assert len(results) == 3
    assert results[0].title == "The Widget Latency Guide"
    assert results[0].url == "https://example.com/latency-guide"
    assert "diagnosing widget latency" in results[0].snippet
    assert results[0].rank == 1


def test_ranks_are_sequential(load_html):
    results = parse_google_serp(load_html("google_serp_modern.html"))
    assert [r.rank for r in results] == [1, 2, 3]


def test_unwraps_google_redirect_urls(load_html):
    results = parse_google_serp(load_html("google_serp_modern.html"))
    assert results[2].url == "https://third.example.net/paper"


def test_parses_legacy_layout(load_html):
    results = parse_google_serp(load_html("google_serp_legacy.html"))
    assert len(results) == 2
    assert results[0].title == "Legacy Result One"
    assert results[0].url == "https://legacy.example.com/one"
    assert "first legacy result" in results[0].snippet


def test_no_results_page_yields_nothing(load_html):
    assert parse_google_serp(load_html("google_no_results.html")) == []


def test_max_results_is_honoured(load_html):
    assert len(parse_google_serp(load_html("google_serp_modern.html"), max_results=2)) == 2


def test_empty_html_yields_nothing():
    assert parse_google_serp("") == []


def test_results_without_a_usable_link_are_skipped():
    html = '<div id="rso"><div class="g"><h3>No link here</h3></div></div>'
    assert parse_google_serp(html) == []


def test_relative_and_internal_links_are_skipped():
    html = (
        '<div id="rso"><div class="g"><a href="/search?q=more"><h3>Internal</h3></a></div>'
        '<div class="g"><a href="https://real.example.com/x"><h3>Real</h3></a></div></div>'
    )
    results = parse_google_serp(html)
    assert len(results) == 1
    assert results[0].url == "https://real.example.com/x"


def test_missing_snippet_becomes_empty_string():
    html = '<div id="rso"><div class="g"><a href="https://a.com/x"><h3>Title</h3></a></div></div>'
    assert parse_google_serp(html)[0].snippet == ""


def test_duplicate_urls_are_collapsed():
    html = (
        '<div id="rso">'
        '<div class="g"><a href="https://a.com/x"><h3>First</h3></a></div>'
        '<div class="g"><a href="https://a.com/x"><h3>Duplicate</h3></a></div>'
        "</div>"
    )
    assert len(parse_google_serp(html)) == 1


# --- block detection ------------------------------------------------------- #


def test_consent_wall_is_detected(load_html):
    assert detect_block(load_html("google_consent.html")) is BlockReason.CONSENT


def test_captcha_is_detected(load_html):
    assert detect_block(load_html("google_captcha.html")) is BlockReason.CAPTCHA


def test_normal_serp_is_not_a_block(load_html):
    assert detect_block(load_html("google_serp_modern.html")) is None


def test_no_results_page_is_not_a_block(load_html):
    assert detect_block(load_html("google_no_results.html")) is None


def test_parse_raises_when_the_page_is_a_block(load_html):
    with pytest.raises(SearchBlockedError, match="CAPTCHA"):
        parse_google_serp(load_html("google_captcha.html"), raise_on_block=True)


def test_parse_raises_on_a_consent_wall(load_html):
    with pytest.raises(SearchBlockedError, match="consent"):
        parse_google_serp(load_html("google_consent.html"), raise_on_block=True)


# --- redirect unwrapping --------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/url?q=https://x.com/a&sa=U", "https://x.com/a"),
        ("https://www.google.com/url?q=https://y.com/b", "https://y.com/b"),
        ("https://plain.example.com/c", "https://plain.example.com/c"),
        ("/url?noq=1", None),
        ("/search?q=next+page", None),
        ("", None),
    ],
)
def test_unwrap_google_redirect(raw, expected):
    assert unwrap_google_redirect(raw) == expected


def test_non_http_schemes_are_rejected():
    assert unwrap_google_redirect("javascript:void(0)") is None
    assert unwrap_google_redirect("mailto:a@b.com") is None


def test_google_internal_absolute_links_are_rejected():
    assert unwrap_google_redirect("https://www.google.com/search?q=next") is None


def test_result_with_a_link_but_no_heading_is_skipped():
    html = '<div id="rso"><div class="g"><a href="https://a.com/x">bare link</a></div></div>'
    assert parse_google_serp(html) == []


def test_empty_snippet_element_falls_through_to_the_next_selector():
    html = (
        '<div id="rso"><div class="g">'
        '<a href="https://a.com/x"><h3>Title</h3></a>'
        '<div class="VwiC3b"></div><div class="st">The real snippet.</div>'
        "</div></div>"
    )
    assert parse_google_serp(html)[0].snippet == "The real snippet."


def test_raise_on_block_passes_a_normal_page_through(load_html):
    results = parse_google_serp(load_html("google_serp_modern.html"), raise_on_block=True)
    assert len(results) == 3


def test_long_snippets_are_trimmed():
    from app import constants

    long_snippet = "word " * 5000
    html = (
        '<div id="rso"><div class="g">'
        f'<a href="https://a.com/x"><h3>Title</h3></a><div class="st">{long_snippet}</div>'
        "</div></div>"
    )
    assert len(parse_google_serp(html)[0].snippet) <= constants.MAX_SNIPPET_CHARS


def test_max_results_stops_mid_container():
    html = (
        '<div id="rso">'
        + "".join(
            f'<div class="g"><a href="https://a.com/{i}"><h3>T{i}</h3></a></div>' for i in range(10)
        )
        + "</div>"
    )
    assert len(parse_google_serp(html, max_results=3)) == 3
