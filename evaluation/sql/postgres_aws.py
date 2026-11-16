"""Compatibility preflight entry point. SQL storage now uses the SeBS CLI."""
import argparse
from sebs.storage.aws_postgresql import preflight


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight"])
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()
    preflight(args.region)


if __name__ == "__main__":
    main()
