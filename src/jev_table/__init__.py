"""jev-table — AI columns for CSV files with TypeSafe's Jev."""

__version__ = "0.1.0"


class UsageError(Exception):
    """Bad input, bad options, or a refused run. CLI exit code 2."""
