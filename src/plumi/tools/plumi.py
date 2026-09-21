import logging
import sys

from plumi.app import Plumi


def main() -> int:
    # the credentials preflight logs its mode at info; without this
    # the default warning threshold would swallow it
    logging.basicConfig(level=logging.INFO)
    return Plumi().run()


if __name__ == "__main__":
    sys.exit(main())
