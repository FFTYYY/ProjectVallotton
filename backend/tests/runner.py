"""
The suite's test runner: Django's, kept as one place to hang suite-wide
setup on (today there is none).
"""

from django.test.runner import DiscoverRunner


class Runner(DiscoverRunner):
    pass
