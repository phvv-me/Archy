from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Immutable base for typed graph facts and services."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
