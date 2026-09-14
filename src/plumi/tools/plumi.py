import sys

from plumi.app import Plumi


def main() -> int:
    return Plumi().run()


if __name__ == "__main__":
    sys.exit(main())
