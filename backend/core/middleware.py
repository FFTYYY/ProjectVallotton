"""
Middleware that has to run outside everything else.

Two. One exists for a reason that only shows up at this layer: some response
headers are not written by the view, they are patched on by middleware after
the view has returned, and a view cannot take back what has not been added
yet. The other opens the language scope a request speaks in.
"""

from __future__ import annotations

from core import i18n


class LanguageScope:
    """
    One request, one language.

    Every sentence a view hands back comes from core.ui_text, which asks
    core.i18n which language the account chose. Inside this scope that
    question is answered once, with one query, the first time it is asked;
    without it each sentence would ask the database again, and a request that
    straddled a change of language could answer half in each.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        with i18n.scope():
            return self.get_response(request)


class DoesNotVaryByWhoAsked:
    """
    Let a response say that it is the same for everyone who may see it.

    Django adds ``Vary: Cookie`` to anything whose handler touched the session,
    which is everything behind a login, which is every image in this app. For
    JSON that is right — the answer really does depend on who asked. For a
    generated PNG it is not: the bytes are the same bytes, and the header means
    the browser's cache key is the whole cookie string, so one rotated CSRF
    token throws away every picture the browser had stored.

    Dropping it is safe only because of what sits next to it:
    ``Cache-Control: private`` already forbids shared caches from storing the
    response at all, and a private cache belongs to one browser profile. What
    is left is a cache that can hold the file across a cookie change, which is
    the whole point.

    A view opts in per response, rather than this matching on paths: the view
    is what knows whether what it returned depends on the caller, and a path
    prefix here would be that knowledge written down a second time, in the
    place least likely to be updated when it changes.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if getattr(response, "same_for_everyone", False) and response.has_header("Vary"):
            del response["Vary"]
        return response
