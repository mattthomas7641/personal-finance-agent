from __future__ import annotations

import datetime as dt
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def now() -> datetime:
    return datetime.now(timezone.utc)


class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    type: str = ""  # depository | credit | ...
    mask: str = ""
    source: str = "csv"  # plaid | csv | demo
    item_id: Optional[str] = None


class PlaidItem(SQLModel, table=True):
    """A connected bank login. access_token never leaves the server or reaches the LLM."""
    id: str = Field(primary_key=True)
    access_token: str  # "keychain:<item_id>": the real token lives in the macOS Keychain
    institution: str = ""
    cursor: str = ""
    env: str = "sandbox"
    created_at: datetime = Field(default_factory=now)


class Txn(SQLModel, table=True):
    """amount > 0 is money out (spending); amount < 0 is money in. Same convention as Plaid."""
    id: str = Field(primary_key=True)
    account_id: str = Field(index=True)
    date: dt.date = Field(index=True)
    amount: float
    raw_name: str  # bank descriptor, e.g. "SQ *BLUE BOTTLE 0412"
    merchant: str = ""  # cleaned display name
    category: str = Field(default="", index=True)
    category_source: str = ""  # rule | memory | plaid | llm | user
    plaid_category: str = ""  # Plaid personal_finance_category mapped to our taxonomy, if any
    pending: bool = False
    source: str = "csv"


class Rule(SQLModel, table=True):
    """User corrections. Matched against the normalized descriptor before any LLM call."""
    id: Optional[int] = Field(default=None, primary_key=True)
    pattern: str = Field(index=True, unique=True)
    category: str
    merchant: str = ""
    created_at: datetime = Field(default_factory=now)


class Outbound(SQLModel, table=True):
    """Local log of every piece of text sent to the Claude API (after redaction)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: str  # categorize | question | tool_result
    text: str
    chars: int = 0
    created_at: datetime = Field(default_factory=now)


class AgentRun(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    question: str
    parent_id: Optional[int] = None  # previous question in a follow-up thread
    status: str = "running"
    answer: str = ""
    verified: bool = False
    charts: str = "[]"  # JSON list of {title, kind, labels, values}
    turns: int = 0
    cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=now)


class AgentStep(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    idx: int
    kind: str
    name: str = ""
    content: str = ""
