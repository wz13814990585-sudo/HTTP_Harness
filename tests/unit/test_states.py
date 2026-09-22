from __future__ import annotations

import pytest

from hnh.domain.errors import InvalidTransition
from hnh.domain.states import TERMINAL_RUN_STATUSES, RunStatus, require_run_transition


@pytest.mark.parametrize("terminal", sorted(TERMINAL_RUN_STATUSES, key=str))
@pytest.mark.parametrize("target", list(RunStatus))
def test_terminal_run_cannot_revive(terminal: RunStatus, target: RunStatus) -> None:
    with pytest.raises(InvalidTransition):
        require_run_transition(terminal, target)
