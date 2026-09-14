"""Common CLI driver for ubuntu24.py package build scripts.

Every script defines three callbacks and hands them to run():

  build(args)             Perform the actual build; raise BuildError (or call
                           lib.ubuntu.fail) on any failure.
  do_check(args)          Print a full version status report and return the
                           process exit code (0 up to date, 1 action needed).
  check_up_to_date(args)  Fast local check used in the default (non --check,
                           non --force) flow. Return an "already installed"
                           message string to skip the build, or None to
                           proceed with it.

run() wires these together with the shared -i/-c/-f flags, handles the
--check/--force mutual exclusion, and reports BuildError as `error: ...`.
"""

from __future__ import annotations

import argparse
import sys

from lib.ubuntu import BuildError


def run(
    build,
    do_check,
    check_up_to_date,
    description: str,
    add_arguments=None,
    install_help: str = "Install the produced .deb after building",
) -> int:
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-i", "--install", action="store_true", help=install_help)
    parser.add_argument("-c", "--check", action="store_true", help="Report version status without building")
    parser.add_argument("-f", "--force", action="store_true", help="Skip up-to-date check and always rebuild")
    if add_arguments:
        add_arguments(parser)
    args = parser.parse_args()

    if args.check and args.force:
        print("error: --check and --force are mutually exclusive", file=sys.stderr)
        return 1

    if args.check:
        return do_check(args)

    # Without --force, skip the build when the installed package is already current.
    if not args.force:
        message = check_up_to_date(args)
        if message:
            print(message)
            return 0

    try:
        build(args)
    except BuildError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0
