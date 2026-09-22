"""Opt-in four-case echo-only live comparison; no synthetic results."""

from __future__ import annotations

from hnh.evaluation_live import LiveEchoConfig, run_live_echo


def main() -> int:
    try:
        output = run_live_echo(LiveEchoConfig.from_environment())
    except (FileExistsError, ValueError) as exc:
        # Configuration errors contain variable names, never secret values.
        print(f"live evaluation not started: {exc}")
        return 2
    print(f"raw evaluation rows: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
