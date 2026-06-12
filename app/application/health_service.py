from typing import Any


class HealthService:
    def __init__(self, **dependencies: object) -> None:
        self._dependencies = dependencies

    def process_health(self) -> dict[str, str]:
        return {"status": "ok"}

    def dependency_health(self) -> dict[str, dict[str, Any]]:
        if not self._dependencies:
            return {
                "sqlite": {"status": "unknown"},
                "qdrant": {"status": "unknown"},
                "ollama": {"status": "unknown"},
            }

        return {
            name: self._dependency_status(dependency)
            for name, dependency in self._dependencies.items()
        }

    @staticmethod
    def _dependency_status(dependency: object) -> dict[str, Any]:
        health = getattr(dependency, "health", None)
        if callable(health):
            result = health()
            if isinstance(result, dict):
                return result
            return {"status": result}
        return {"status": "unknown"}
