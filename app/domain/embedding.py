from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingVector:
    chunk_id: str
    model: str
    dimensions: int
    values: list[float]
