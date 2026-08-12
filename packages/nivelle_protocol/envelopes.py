from typing import Any

from pydantic import BaseModel, Field


class DataEnvelope(BaseModel):
    data: Any
    meta: dict[str, Any] = Field(default_factory=dict)
