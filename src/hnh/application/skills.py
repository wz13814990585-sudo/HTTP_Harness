from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from hnh.domain.errors import DependencyUnavailable, InvalidOperation, PayloadTooLarge


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    skill_id: str
    revision: str
    content: str

    def source_ref(self) -> dict[str, str]:
        return {"kind": "skill", "id": self.skill_id, "revision": self.revision}


class SkillLoader:
    """Trusted allowlist of guidance files; text is never an authority source."""

    def __init__(
        self,
        root: Path,
        paths: dict[str, str],
        agent_skills: dict[str, tuple[str, ...]],
        *,
        max_bytes: int = 16384,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.root = root.resolve(strict=True)
        self.paths = dict(paths)
        self.agent_skills = dict(agent_skills)
        self.max_bytes = max_bytes
        for ids in self.agent_skills.values():
            if len(ids) != len(set(ids)) or any(skill_id not in self.paths for skill_id in ids):
                raise ValueError("agent skill configuration references unknown or duplicate IDs")

    def load_for_agent(self, agent_id: str) -> tuple[LoadedSkill, ...]:
        return tuple(self.load(skill_id) for skill_id in self.agent_skills.get(agent_id, ()))

    def load(self, skill_id: str) -> LoadedSkill:
        name = self.paths.get(skill_id)
        if name is None:
            raise InvalidOperation("skill is not registered")
        candidate = self.root / name
        if candidate.is_symlink():
            raise InvalidOperation("symlink skill paths are forbidden")
        try:
            path = candidate.resolve(strict=True)
            path.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise DependencyUnavailable("approved skill file") from exc
        if not path.is_file():
            raise DependencyUnavailable("approved skill file")
        with path.open("rb") as source:
            raw = source.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise PayloadTooLarge()
        content = raw.decode("utf-8", errors="strict")
        return LoadedSkill(skill_id, hashlib.sha256(raw).hexdigest(), content)
