from app.services.text.extractor import extract_content


def test_extracts_title_author_and_published_date(load_html):
    content = extract_content(load_html("article.html"), url="https://example.com/a")
    assert content.title == "Understanding Widget Latency | Example Journal"
    assert content.author == "Jordan Rivera"
    assert content.published == "2026-03-14T09:00:00Z"
    assert content.url == "https://example.com/a"


def test_extracts_the_article_body(load_html):
    text = extract_content(load_html("article.html"), url="https://example.com/a").text
    assert "queue contention in the dispatch layer" in text
    assert "sharded the dispatch queue" in text


def test_discards_scripts_styles_navigation_and_chrome(load_html):
    text = extract_content(load_html("article.html"), url="https://example.com/a").text
    assert "tracking pixel" not in text
    assert "display: none" not in text
    assert "Related stories" not in text
    assert "cookies" not in text.lower()


def test_falls_back_to_the_body_when_there_is_no_article(load_html):
    content = extract_content(load_html("minimal.html"), url="https://example.com/m")
    assert content.title == "Bare"
    assert "one short paragraph" in content.text


def test_page_without_content_yields_empty_text(load_html):
    content = extract_content(load_html("empty.html"), url="https://example.com/e")
    assert content.title == "Nothing Here"
    assert content.text == ""


def test_missing_metadata_is_none():
    content = extract_content("<html><body><p>Body text goes here now.</p></body></html>", url="u")
    assert content.title is None
    assert content.author is None
    assert content.published is None


def test_empty_document_is_handled():
    content = extract_content("", url="https://example.com/")
    assert content.text == ""
    assert content.title is None


def test_word_count_is_reported(load_html):
    assert extract_content(load_html("article.html"), url="u").word_count > 20


BODY = "<p>Some body content here to clear the threshold.</p>"


def test_author_falls_back_to_a_byline_element():
    html = f'<html><body><span class="byline">By Sam Doe</span>{BODY}</body></html>'
    assert extract_content(html, url="u").author == "By Sam Doe"


def test_published_falls_back_to_a_time_element():
    html = f'<html><body><time datetime="2026-01-02">Jan 2</time>{BODY}</body></html>'
    assert extract_content(html, url="u").published == "2026-01-02"


def test_empty_meta_content_is_skipped_in_favour_of_the_next_source():
    html = (
        '<html><head><meta name="author" content="  ">'
        '<meta property="article:author" content="Real Person"></head>'
        f"<body>{BODY}</body></html>"
    )
    assert extract_content(html, url="u").author == "Real Person"


def test_empty_byline_element_does_not_win():
    html = (
        '<html><body><span class="byline"></span>'
        f'<span class="author">Sam</span>{BODY}</body></html>'
    )
    assert extract_content(html, url="u").author == "Sam"


def test_empty_time_datetime_is_ignored():
    html = f'<html><body><time datetime="  ">soon</time>{BODY}</body></html>'
    assert extract_content(html, url="u").published is None


def test_container_with_too_little_text_is_skipped():
    html = f"<html><body><article>hi</article><main>{BODY}</main></body></html>"
    assert "Some body content" in extract_content(html, url="u").text


def test_whitespace_only_title_becomes_none():
    html = f"<html><head><title>   </title></head><body>{BODY}</body></html>"
    assert extract_content(html, url="u").title is None
