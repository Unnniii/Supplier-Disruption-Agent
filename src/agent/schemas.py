"""What each agent must hand back. Pydantic checks the shape; the verifier and
the evaluation check whether it is right. Valid JSON is not a correct answer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ScanResult(BaseModel):
    is_disruption: bool = Field(description="True only if the event could stop or delay industrial production or freight")
    event_type: str | None = Field(None, description="One of the known event types, e.g. fire, flood, earthquake, lockdown, conflict, typhoon, severe_weather, route_blocked, port_strike")
    places: list[str] = Field(default_factory=list, description="Affected places, named as specifically as the article allows")
    routes: list[str] = Field(default_factory=list, description="Blocked shipping routes, e.g. Suez Canal")
    ports: list[str] = Field(default_factory=list, description="Affected ports")
    severity: Literal["low", "medium", "high"] | None = None
    reason: str = ""


class MappedSupplier(BaseModel):
    supplier_id: str
    why: str = Field(description="Which tool result links this supplier to the event")


class MapResult(BaseModel):
    suppliers: list[MappedSupplier] = Field(default_factory=list)


class PartRisk(BaseModel):
    part_id: str
    part_name: str
    plant_id: str
    days_of_cover: float
    is_critical: bool
    used_in_models: str


class SupplierImpact(BaseModel):
    supplier_id: str
    risk_level: Literal["low", "medium", "high"]
    min_days_of_cover: float | None
    parts: list[PartRisk] = Field(default_factory=list)


class ImpactResult(BaseModel):
    impacts: list[SupplierImpact] = Field(default_factory=list)


class SupplierAdvice(BaseModel):
    supplier_id: str
    backup_supplier_id: str | None = Field(None, description="Only if a contract clause you retrieved names it")
    coverage: Literal["full", "partial", "none", "unknown"]
    actions: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list, description="chunk_ids of the clauses you relied on")


class AdviceResult(BaseModel):
    advice: list[SupplierAdvice] = Field(default_factory=list)
