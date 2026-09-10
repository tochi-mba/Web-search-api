"""Tunable thresholds and fixed defaults shared across the service.

These live in one module rather than being scattered as literals so that the
truncation contract (`MAX_CONTENT_CHARS` and friends) is discoverable, testable
and documentable in one place.
"""

from __future__ import annotations

#: Hard ceiling on the number of characters of scraped text ever handed to an
#: LLM. The effective limit for a given request is the smaller of this and what
#: the target model's context window can accommodate.
MAX_CONTENT_CHARS = 40_000

#: Ceiling on caller-supplied ``additional_notes``. Notes are injected into the
#: prompt verbatim, so they need their own bound.
MAX_NOTES_CHARS = 4_000

#: Ceiling on a single SERP snippet before it is trimmed.
MAX_SNIPPET_CHARS = 1_000

#: Rough characters-per-token ratio used to convert a model's context window
#: into a character budget. Deliberately conservative: over-estimating tokens
#: truncates a little early, which is safe, whereas under-estimating overflows.
CHARS_PER_TOKEN = 4

#: Tokens held back for the model's own answer when computing the input budget.
RESERVED_OUTPUT_TOKENS = 4_000

#: Tokens assumed to be consumed by the system prompt, instructions and notes.
PROMPT_OVERHEAD_TOKENS = 1_500

#: Namespaced ``provider:model`` identifier used when a caller does not choose.
DEFAULT_MODEL = "anthropic:claude-opus-5"

#: How long a probed provider/model catalogue stays fresh.
MODEL_CACHE_TTL_SECONDS = 300.0

#: Upper bound on results requested from a single search query.
MAX_RESULTS_PER_QUERY = 50

#: Upper bound on queries in one batch request.
MAX_QUERIES_PER_REQUEST = 20

#: Upper bound on URLs in one scrape request.
MAX_URLS_PER_REQUEST = 20

#: Header used to correlate logs with a single request.
REQUEST_ID_HEADER = "X-Request-ID"
