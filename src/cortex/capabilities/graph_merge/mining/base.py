from __future__ import annotations

from abc import ABC, abstractmethod

from cortex.capabilities.graph_merge.models import _GraphMergeModel
from cortex.capabilities.graph_merge.rewrite.contracts import GraphRewriteProposal
from cortex.schema import GraphState, SchemaDefinition


class MiningDiagnostic(_GraphMergeModel):
    code: str
    component_id: str | None = None
    message: str


class GraphMiningResult(_GraphMergeModel):
    proposals: tuple[GraphRewriteProposal, ...] = ()
    diagnostics: tuple[MiningDiagnostic, ...] = ()


class BaseGraphMiner(ABC):
    miner_id: str

    @abstractmethod
    async def propose(
        self, state: GraphState, schema: SchemaDefinition
    ) -> GraphMiningResult:
        """Locate candidate subgraphs and resolve them into bounded rewrite proposals."""
