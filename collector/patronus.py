from typing import Any

from .http import JsonClient

# Robot statuses after which neither the robot nor its checks change any more.
TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILURE", "CANCELED", "SKIPPED"})


class Patronus:
    """Read-only part of the Patronus REST API (see ultimate/.claude/skills/patronus/SKILL.md)."""

    def __init__(self, client: JsonClient):
        self._c = client

    def robot(self, robot_id: str) -> dict[str, Any]:
        return self._c.get(f"app/rest/v1/robots/{robot_id}")

    def changes(self, robot_id: str) -> dict[str, Any]:
        return self._c.get(f"app/rest/v1/robots/{robot_id}/changes")

    def teamcity_checks(self, robot_id: str) -> dict[str, Any]:
        return self._c.get(f"app/rest/v1/robots/{robot_id}/teamcity-checks")

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        """One attempt of a check, with the failed tests and whether Patronus ignored each failure."""
        return self._c.get(f"app/rest/v1/teamcity-checks/attempts/{attempt_id}")
