#!/usr/bin/env python
import os
import sys

# This directory holds the project package. Python normally puts a script's
# directory on sys.path itself, but the bundled Windows runtime (an embeddable
# CPython with a ._pth file) runs in isolated mode and does not, so say it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vallotton.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
