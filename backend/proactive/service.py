"""主动交流应用入口，只装配准备与生成，不复制其状态。"""
from __future__ import annotations
from typing import Any
from backend.proactive.preparation import ProactivePreparation
from backend.proactive.generation import ProactiveGenerator
from backend.proactive.prepared import PreparedProactiveTurn

class ProactiveService:
    def __init__(self, preparation: ProactivePreparation, generator: ProactiveGenerator) -> None:
        self.preparation = preparation
        self.generator = generator

    def execute(self, body: dict[str, Any]) -> dict[str, Any]:
        prepared = self.preparation.prepare(body)
        if not isinstance(prepared, PreparedProactiveTurn):
            return prepared
        return self.generator.generate(prepared)
