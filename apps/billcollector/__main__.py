"""Entry point for ``python -m billcollector``."""

import sys

if __name__ == "__main__":
    from billcollector import config

    try:
        from billcollector.cli import main

        sys.exit(main())
    except config.ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)
