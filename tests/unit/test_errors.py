import pytest

from app.core import errors


def test_domain_error_carries_problem_fields():
    exc = errors.UpstreamError("boom", detail="upstream said no")
    assert exc.title == "boom"
    assert exc.detail == "upstream said no"
    assert exc.status == 502
    assert exc.code == "upstream_error"


def test_detail_defaults_to_title():
    assert errors.ValidationProblem("bad input").detail == "bad input"


@pytest.mark.parametrize(
    ("exc_class", "status", "code"),
    [
        (errors.ValidationProblem, 400, "validation_problem"),
        (errors.AuthError, 401, "auth_error"),
        (errors.ForbiddenUrlError, 403, "forbidden_url_error"),
        (errors.NotFoundError, 404, "not_found_error"),
        (errors.RateLimitedError, 429, "rate_limited_error"),
        (errors.UpstreamError, 502, "upstream_error"),
        (errors.SearchBlockedError, 502, "search_blocked_error"),
        (errors.ProviderUnavailableError, 503, "provider_unavailable_error"),
        (errors.TimeoutProblem, 504, "timeout_problem"),
    ],
)
def test_status_and_code_mapping(exc_class, status, code):
    exc = exc_class("t")
    assert exc.status == status
    assert exc.code == code


def test_to_problem_produces_rfc9457_shape():
    problem = errors.UpstreamError("boom", detail="d", instance="/v1/scrape").to_problem()
    assert problem == {
        "type": "https://web-search-api.dev/problems/upstream_error",
        "title": "boom",
        "status": 502,
        "detail": "d",
        "code": "upstream_error",
        "instance": "/v1/scrape",
    }


def test_to_problem_omits_absent_instance():
    assert "instance" not in errors.AuthError("nope").to_problem()


def test_to_error_payload_is_the_batch_item_shape():
    payload = errors.TimeoutProblem("slow", detail="took too long").to_error_payload()
    assert payload == {"code": "timeout_problem", "title": "slow", "detail": "took too long"}


def test_subclass_may_declare_an_explicit_code():
    class WeirdlyNamedThing(errors.DomainError):
        status = 418
        code = "teapot"

    assert WeirdlyNamedThing("t").code == "teapot"


def test_subclass_inherits_status_from_its_parent():
    assert errors.SearchBlockedError("t").status == 502
