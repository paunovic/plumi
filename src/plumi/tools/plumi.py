import logging
import sys

from plumi.app import Plumi


def main() -> int:
    # preflight logs at info
    logging.basicConfig(level=logging.INFO)
    return Plumi().run()


if __name__ == "__main__":
    sys.exit(main())
