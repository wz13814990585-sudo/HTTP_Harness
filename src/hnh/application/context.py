from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from hnh.application.capabilities import CapabilityRegistry
from hnh.application.decision import StrategyHint
from hnh.application.run_controller import ActionSnapshot, ClaimedJob, RunController
from hnh.application.security import redact, scope_fingerprint
from hnh.application.skills import SkillLoader
from hnh.domain.errors import MigrationRequired, ResourceNotFound
from hnh.domain.identity import TrustedContext
from hnh.ports.models import ContextSnapshot


class ContextBuilder:
    def __init__(
        self,
        controller: RunController,
        registry: CapabilityRegistry,
        *,
        max_inline_chars: int = 4096,
        skill_loader: SkillLoader | None = None,
        observation_projector: Callable[[ActionSnapshot, dict[str, Any]], dict[str, Any]]
        | None = None,
    ) -> None:
        if max_inline_chars < 64:
            raise ValueError("max_inline_chars must be at least 64")
        self.controller = controller
        self.registry = registry
        self.max_inline_chars = max_inline_chars
        self.skill_loader = skill_loader
        self.observation_projector = observation_projector

    def build(
        self,
        context: TrustedContext,
        run_id: str,
        turn_id: int,
        *,
        strategy_hint: StrategyHint = "simple",
        claimed_job: ClaimedJob | None = None,
        worker_id: str | None = None,
    ) -> ContextSnapshot:
        context = self.controller.effective_context(context, run_id)
        try:
            existing = self.controller.get_context_snapshot(context, run_id, turn_id)
        except ResourceNotFound:
            existing = None
        if existing is not None:
            if existing.content.get("scope_fingerprint") != scope_fingerprint(context.scopes):
                raise MigrationRequired("context snapshot authorization scope changed")
            return ContextSnapshot(
                id=existing.id,
                run_id=existing.run_id,
                turn_id=existing.turn_id,
                content=existing.content,
                source_refs=existing.source_refs,
                content_hash=existing.content_hash,
            )
        run = self.controller.get_run(context, run_id)
        skills = () if self.skill_loader is None else self.skill_loader.load_for_agent(run.agent_id)
        skill_refs = [skill.source_ref() for skill in skills]
        self.controller.pin_run_skills(
            context, run_id, skill_refs, claimed_job=claimed_job, worker_id=worker_id
        )
        actions = self.controller.list_actions(context, run_id)
        observations: list[dict[str, Any]] = []
        source_refs: list[dict[str, Any]] = []
        source_refs.extend(skill_refs)
        for action in actions:
            if action.result is None:
                continue
            # A trusted evaluation can normalize the model-visible observation
            # without changing the committed Action receipt or source reference.
            visible_result = (
                action.result
                if self.observation_projector is None
                else self.observation_projector(action, action.result)
            )
            sanitized = redact(visible_result)
            encoded = json.dumps(sanitized, ensure_ascii=False, sort_keys=True)
            source_ref = {
                "kind": "action",
                "id": action.id,
                "url": f"/v1/actions/{action.id}",
                "version": action.version,
                "request_hash": action.request_hash,
            }
            source_refs.append(source_ref)
            if len(encoded) > self.max_inline_chars:
                observations.append(
                    {
                        "action_id": action.id,
                        "capability_id": action.capability_id,
                        "status": action.status.value,
                        "summary": encoded[: self.max_inline_chars] + "…",
                        "truncated": True,
                        "source_ref": source_ref,
                    }
                )
            else:
                observations.append(
                    {
                        "action_id": action.id,
                        "capability_id": action.capability_id,
                        "status": action.status.value,
                        "result": sanitized,
                        "truncated": False,
                        "source_ref": source_ref,
                    }
                )
        catalog = self.registry.list(context, None, 100)
        recent_events = self.controller.get_recent_events(context, run_id)
        content = {
            "goal": run.goal,
            "scope_fingerprint": scope_fingerprint(context.scopes),
            "strategy_hint": strategy_hint,
            "run": {
                "id": run.id,
                "status": run.status.value,
                "workspace_id": run.workspace_id,
            },
            "budget": self.controller.budget_status(context, run_id),
            "input_artifact_ids": list(run.artifact_refs),
            "skills": [
                {
                    "id": skill.skill_id,
                    "revision": skill.revision,
                    "trust": "untrusted_guidance_not_authority",
                    "content": skill.content,
                }
                for skill in skills
            ],
            "observations": observations,
            "recent_events": [
                {
                    "seq": event.seq,
                    "type": event.type,
                    "action_id": event.action_id,
                    "data": redact(event.data),
                }
                for event in recent_events
            ],
            "capabilities": [
                {
                    "id": item.id,
                    "revision": item.revision,
                    "method": item.method,
                    "path_template": item.path_template,
                    "description": item.description,
                }
                for item in catalog.items
            ],
        }
        saved = self.controller.save_context_snapshot(
            context,
            run_id,
            turn_id,
            content,
            source_refs,
            claimed_job=claimed_job,
            worker_id=worker_id,
        )
        return ContextSnapshot(
            id=saved.id,
            run_id=saved.run_id,
            turn_id=saved.turn_id,
            content=saved.content,
            source_refs=saved.source_refs,
            content_hash=saved.content_hash,
        )
