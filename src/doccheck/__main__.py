"""Allow ``python -m doccheck`` as well as the ``doccheck`` console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
