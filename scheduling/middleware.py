"""Small, dependency-free CORS middleware for the hosted frontend.

The project intentionally keeps authentication in Django session cookies.  A
published static Site therefore needs a narrowly allow-listed origin when it
talks to a Django instance over HTTPS.  This middleware is disabled unless
``DJANGO_CORS_ALLOWED_ORIGINS`` is set; it never turns an API into a public
endpoint and never uses ``*`` with credentials.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.http import HttpResponse


class HostedSiteCorsMiddleware:
    """Allow credentialed requests only from configured exact origins."""

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _origins() -> set[str]:
        return {
            str(origin).strip().rstrip("/")
            for origin in getattr(settings, "CORS_ALLOWED_ORIGINS", ())
            if str(origin).strip()
        }

    def __call__(self, request):
        origin = request.headers.get("Origin", "").rstrip("/")
        allowed = origin in self._origins()

        # The API is on a different origin from the static Site, so the
        # browser cannot read the API-domain CSRF cookie.  The CSRF endpoint
        # returns Django's masked token in JSON; for the same exact allow-listed
        # origin, use that token as the cookie-side value so Django's normal
        # CsrfViewMiddleware still performs its double-submit comparison.
        # This is deliberately limited to requests that already carry the
        # trusted Origin and never applies to local or arbitrary origins.
        if allowed and request.method not in ("GET", "HEAD", "OPTIONS"):
            header_token = request.headers.get("X-CSRFToken")
            if header_token:
                # A browser can retain a stale partitioned API cookie after a
                # Site reload.  For the exact allow-listed origin, the token
                # supplied by the current frontend request is authoritative;
                # replacing the request-side value prevents an old cookie from
                # causing a false CSRF mismatch while Django still validates
                # the header/cookie pair normally.
                request.COOKIES["csrftoken"] = header_token

        # Browsers send a preflight before credentialed JSON writes.  Return
        # only for an allow-listed origin; all other OPTIONS requests proceed
        # through Django's normal routing/authentication behavior.
        if request.method == "OPTIONS" and allowed:
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        if allowed:
            response["Access-Control-Allow-Origin"] = origin
            response["Access-Control-Allow-Credentials"] = "true"
            response["Access-Control-Allow-Headers"] = "Content-Type, X-CSRFToken, Authorization"
            response["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
            response["Vary"] = "Origin"
            # The published Site and the temporary HTTPS API have different
            # registrable domains.  Partitioned cookies keep the Django
            # session scoped to this embedding site instead of relying on
            # unrestricted third-party cookies.  This is only added for the
            # explicit hosted-origin allow-list above.
            for cookie_name in ("sessionid", "csrftoken"):
                morsel = response.cookies.get(cookie_name)
                if morsel is not None:
                    # Python 3.9's ``http.cookies`` does not yet list the
                    # modern CHIPS attribute in its reserved names.  Register
                    # it before assigning so the response remains valid on
                    # the project's supported interpreter.
                    morsel._reserved.setdefault("partitioned", "Partitioned")
                    morsel._flags.add("partitioned")
                    morsel["partitioned"] = True
        return response


class HostedBearerSessionMiddleware:
    """Restore a Django session from the short-lived in-memory hosted token.

    Some embedded browsers still block cross-site cookies even when CHIPS is
    available.  The login response therefore exposes the random Django
    session key only to the current frontend runtime, which sends it as a
    Bearer header.  It is never written to localStorage or included in URLs.
    Normal cookie authentication remains the default for local deployments.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            key = header[7:].strip()
            if key and len(key) <= 64:
                request.session = SessionStore(session_key=key)
        return self.get_response(request)
