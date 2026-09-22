"""Opt-in real-model skills off/on ablation; no synthetic benchmark results."""

from __future__ import annotations

from hnh.evaluation_skills import LiveSkillsConfig, run_live_skills


def main() -> int:
    try:
        output = run_live_skills(LiveSkillsConfig.from_environment())
    except (FileExistsError, ValueError) as exc:
        print(f"live skills ablation not started: {exc}")
        return 2
    print(f"raw skills ablation rows: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
