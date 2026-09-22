"""Opt-in real-model file-read/transform/artifact-write task suite."""

from __future__ import annotations

from hnh.evaluation_tasks import LiveTaskConfig, run_live_tasks


def main() -> int:
    try:
        output = run_live_tasks(LiveTaskConfig.from_environment())
    except (FileExistsError, ValueError) as exc:
        print(f"live task evaluation not started: {exc}")
        return 2
    print(f"raw task suite rows: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
