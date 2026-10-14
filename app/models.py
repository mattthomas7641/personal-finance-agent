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
    balance_current: Optional[float] = None  # from Plaid; for credit/loan accounts this is what you owe
    balance_updated: Optional[datetime] = None


class PlaidItem(SQLModel, table=True):
    """A connected bank login. access_token never leaves the server or reaches the LLM."""
    id: str = Field(primary_key=True)
    access_token: str  # "keychain:<item_id>": the real token lives in the macOS Keychain
    institution: str = ""
    cursor: str = ""
    env: str = "sandbox"
    # Health, for update mode: ok | login_required | expiring | error
    status: str = "ok"
    error_code: str = ""
    error_message: str = ""
    consent_expires: Optional[datetime] = None
    last_synced: Optional[datetime] = None
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


class Budget(SQLModel, table=True):
    category: str = Field(primary_key=True)
    monthly_limit: float
    source: str = "manual"  # manual | plan (applied from a savings plan)


class Plan401k(SQLModel, table=True):
    """What the bank data can't see: numbers from your pay stub / benefits portal."""
    id: int = Field(default=1, primary_key=True)
    annual_salary: float = 0
    pay_frequency: str = "auto"  # auto | weekly | biweekly | semimonthly | monthly
    contribution_pct: float = 0  # pre-tax + Roth deferral, % of gross pay
    ytd_contributed: float = 0  # your own deferrals this calendar year (from your latest pay stub)
    ytd_as_of: Optional[dt.date] = None  # pay date of that stub
    match_rate_pct: float = 0  # employer matches this % of what you put in...
    match_up_to_pct: float = 0  # ...on contributions up to this % of salary
    has_true_up: bool = False  # employer tops up the match at year end if you max early
    birth_year: Optional[int] = None
    marginal_tax_rate_pct: float = 22  # rough federal+state bracket, for take-home impact estimates
    updated_at: datetime = Field(default_factory=now)


class RetirementPlan(SQLModel, table=True):
    """Assumptions for retirement, Roth IRA and Coast FIRE tracking. All editable on the Retirement page."""
    id: int = Field(default=1, primary_key=True)
    birth_year: Optional[int] = None  # falls back to the 401(k) plan's birth year
    retirement_age: int = 60
    annual_spending: Optional[float] = None  # in retirement, today's dollars; None = your last 12 months of spending
    withdrawal_rate_pct: float = 4.0
    expected_return_pct: float = 5.0  # after inflation
    # Roth IRA
    filing_status: str = "single"  # single | mfj | mfs
    magi: Optional[float] = None  # modified adjusted gross income; None = estimate from 401(k) salary minus deferrals
    roth_target: bool = True  # include maxing the Roth in the budget plan
    roth_contributed: float = 0  # this tax year, if not auto-detected
    roth_detect_pattern: str = ""  # e.g. "FIDELITY" or "VANGUARD ROTH": sum matching transfers this year instead
    include_gap_in_plan: bool = False  # add the retirement shortfall to the budget plan
    fire_target_low: Optional[float] = None  # "I want $1.5M-$2.0M invested by retirement_age"
    fire_target_high: Optional[float] = None
    harsh: bool = True  # blunt spending feedback on the Plan page
    updated_at: datetime = Field(default_factory=now)


class RetirementAccount(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    kind: str = "other"  # 401k | roth_ira | traditional_ira | brokerage | hsa | other
    balance: float = 0
    monthly_contribution: float = 0  # ignored for 401k (taken from the 401(k) plan) and Roth IRA (from the Roth tracker)
    as_of: Optional[dt.date] = None
    external_id: str = ""  # e.g. "fidelity:1234" (last 4 of the account number) so re-imports update, not duplicate
    holdings: int = 0  # number of positions in the last import


class CashAccount(SQLModel, table=True):
    """Accounts Plaid can't connect (e.g. a HYSA whose bank blocks aggregators). Balance entered by hand."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    kind: str = "savings"  # savings | checking | cd | cash | other
    balance: float = 0
    apy_pct: float = 0
    as_of: Optional[dt.date] = None
    detect_pattern: str = ""  # text in your checking transactions when you move money here, e.g. "MARCUS" or "ALLY"


class Goal(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    kind: str = "savings"  # savings | retirement
    target_amount: float
    target_date: dt.date
    current_amount: float = 0
    monthly_contribution: float = 0
    expected_return_pct: float = 5.0  # retirement goals only; your assumption, after inflation
    account_id: Optional[int] = None  # linked CashAccount: "saved so far" follows its balance
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
    model: str = ""  # model(s) that answered, e.g. "claude-haiku-5-5,claude-opus-5-5" after an escalation
    input_tokens: int = 0       # uncached input
    cache_read_tokens: int = 0  # input served from the prompt cache
    output_tokens: int = 0
    seconds: float = 0.0        # wall time, question to answer
    kind: str = "question"      # question | digest
    created_at: datetime = Field(default_factory=now)


class AgentStep(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    idx: int
    kind: str
    name: str = ""
    content: str = ""


class SubscriptionDecision(SQLModel, table=True):
    """What you decided about a recurring charge. 'cancelled' + a later charge = an alert."""
    key: str = Field(primary_key=True)  # normalized descriptor
    status: str = "review"              # keep | review | cancelled
    decided_on: dt.date = Field(default_factory=lambda: dt.date.today())
    note: str = ""


class BalanceSnapshot(SQLModel, table=True):
    """One row per day: totals at that point, so progress has a history and not just a projection."""
    day: dt.date = Field(primary_key=True)
    invested: float = 0.0
    cash: float = 0.0
    debts: float = 0.0
    net_worth: float = 0.0


class Dismissal(SQLModel, table=True):
    """An alert you've seen and dismissed ("Needs a look"); it stays hidden and the digest skips it."""
    key: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=now)


class Proposal(SQLModel, table=True):
    """A change the agent suggests. The agent can't apply it: you approve or reject it on the run page."""
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    kind: str            # budget | retirement_target
    payload: str         # JSON
    reason: str = ""
    status: str = "pending"  # pending | approved | rejected
    created_at: datetime = Field(default_factory=now)
