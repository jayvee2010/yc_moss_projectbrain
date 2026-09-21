from pydantic import BaseModel
from typing import List


class Memory(BaseModel):
    type: str
    title: str
    content: str
    status: str
    entities: List[str]


class MemoryExtraction(BaseModel):
    memories: List[Memory]
    