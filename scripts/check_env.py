"""
Compare the variable NAMES in .env with .env.example.

    python scripts/check_env.py [--env .env] [--example .env.example]

Prints the names that .env is missing and the names it has that .env.example
does not list. Values are never printed: each line is reduced to its name
before anything else happens, and the values are not kept.

Exit code: 0 when the names match, 1 when they differ, 2 when a file cannot be
read. A commented-out line (starting with #) is not a variable.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_NAME = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def variable_names(path: Path) -> set:
    """The set of variable names defined in a dotenv-style file."""
    names = set()
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            match = _NAME.match(line)
            if match:
                names.add(match.group(1))
    return names


def compare(env_names: set, example_names: set) -> tuple:
    return sorted(example_names - env_names), sorted(env_names - example_names)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare variable names in .env and .env.example.")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--example", default=str(ROOT / ".env.example"))
    args = parser.parse_args(argv)

    try:
        env_names = variable_names(Path(args.env))
        example_names = variable_names(Path(args.example))
    except OSError as exc:
        print(f"error: cannot read {Path(exc.filename).name if exc.filename else 'file'}: {exc.strerror}", file=sys.stderr)
        return 2

    missing, extra = compare(env_names, example_names)
    print(f"Missing from {Path(args.env).name} (listed in {Path(args.example).name}): {len(missing)}")
    for name in missing:
        print(f"  {name}")
    print(f"Extra in {Path(args.env).name} (not in {Path(args.example).name}): {len(extra)}")
    for name in extra:
        print(f"  {name}")
    return 1 if missing or extra else 0


if __name__ == "__main__":
    sys.exit(main())
