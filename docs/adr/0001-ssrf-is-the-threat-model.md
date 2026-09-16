# 0001 — SSRF is the threat model, and the check is on resolved addresses

**Status:** Accepted

## Context

This service exists to fetch URLs that somebody else chose. `POST /v1/scrape` takes a list
of them outright, and `POST /v1/search` follows links off a result page. That is the
textbook setup for server-side request forgery: without controls, a caller points it at
`169.254.169.254` and reads cloud instance credentials, or sweeps an internal network using
this service as a proxy that is already inside the perimeter.

The tempting implementation is a deny list of URL patterns — reject anything matching
`localhost`, `127.*`, `10.*`, `192.168.*`. It is tempting because it is one regular
expression and needs no network. It is also wrong in every direction at once: it misses
IPv6 entirely, it misses the decimal, octal and hexadecimal spellings of an IPv4 address,
it misses a hostname on the public internet whose A record points at `10.0.0.1`, and it
blocks perfectly good public hosts whose names happen to contain a blocked string.

## Decision

**Resolve the name, then check the addresses.** `app/services/urlsafety.py` decides nothing
from the text of a URL except its scheme and whether it has a host at all.

The rules, each earning its place:

- **Scheme allowlist.** `http` and `https` only, so `file:`, `ftp:`, `gopher:` and
  `javascript:` are gone before anything else runs.
- **Literal addresses are checked without resolving.** A URL that already names an IP is
  parsed directly; no DNS lookup happens, and a test asserts the resolver was never called.
- **Every address a name resolves to must pass.** If any one of them is blocked, the whole
  host is blocked. Mixed records — one public address and one private — are how you would
  otherwise smuggle an internal target past a check that only looked at the first answer.
- **The address test is the standard library's**, `is_global` plus an explicit multicast
  check, rather than a hand-written table of ranges. The ranges are IANA's and the standard
  library tracks them; a hand-rolled table is out of date the day it is written and
  invariably forgets IPv6. Loopback, RFC 1918, link-local, reserved, unspecified and
  multicast are all covered in both families.
- **Cloud metadata hostnames are blocked by name as well as by address.** Their addresses
  are link-local and so already blocked — but the name check is what keeps them blocked
  when an operator turns on `WSA_ALLOW_PRIVATE_NETWORKS`, and there is no legitimate reason
  for this service to read them in any configuration.
- **The check runs again on every redirect hop.** This is the control most implementations
  miss, and the reason redirects are followed by hand rather than by httpx: validating only
  the caller's URL is worthless if that URL is allowed to answer `302 Location:
  http://127.0.0.1/`. The shared HTTP client is constructed with `follow_redirects=False`
  so the safe path is the only path, and each hop is re-validated before it is fetched,
  inside a redirect budget.

The guard runs before robots.txt is consulted and before a page is handed to the browser,
so no code path reaches a URL that has not been through it.

## Consequences

The service can be pointed at caller-supplied URLs at all, which is the entire product.

**A host is refused outright when any of its addresses is blocked.** A site with one public
address and one private one is unfetchable here. That is the intended trade and it will
occasionally refuse something legitimate.

**Every fetch costs a resolution**, and every redirect hop costs another.

**The address that was checked is not provably the address that gets connected to.** The
guard resolves the name, approves the answer, and then hands the *URL* to httpx, which
resolves it again. A hostile name server that answers differently the second time — DNS
rebinding — is not stopped by this design. The window is small and the deployment model is
a handful of known callers, which is why it is accepted rather than closed.

`WSA_ALLOW_PRIVATE_NETWORKS` exists for deployments crawling their own infrastructure.
Metadata hosts stay blocked even then.

## What would change this

Closing the rebinding gap means connecting to the address that was checked: a custom
transport that dials the verified IP while sending the original `Host` header and doing the
TLS handshake against the original name. That is the correct fix, it is a real amount of
code, and it becomes worth writing the day this service is exposed to callers who are not
already trusted.

A requirement to fetch non-HTTP schemes would not change the decision so much as end it;
the allowlist is doing more work than any other single line here.
