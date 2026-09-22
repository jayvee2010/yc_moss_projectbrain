from pydantic import BaseModel
from typing import List, Optional


class Memory(BaseModel):
    type: str
    title: str
    content: str
    status: str
    entities: List[str]


class MemoryExtraction(BaseModel):
    memories: List[Memory]


class ResolveRequest(BaseModel):
    """Resolution of a detected conflict between a proposal and a current decision."""
    project_id: str
    action: str                          # the proposed action text (from /check)
    resolution: str                      # "supersede" | "keep"
    conflicting_memory_id: Optional[str] = None  # current decision this resolves (optional)
    author: str = "unknown"
    rationale: str = ""                  # optional why, stored on the new decision
    