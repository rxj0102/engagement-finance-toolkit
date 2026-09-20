"""Engagement portfolio setup: contract terms, role budgets, milestones, and staffing.

This is the system-of-record layer every downstream module reads from: the
budgeted hours/dollars by labor category that time & expense actuals get
measured against, the milestone schedule fixed-fee invoices are drawn from,
and the staffing roster that time entries get logged by.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

# --------------------------------------------------------------------------
# Fiscal calendar
# --------------------------------------------------------------------------

FISCAL_YEAR_START = date(2024, 10, 1)
FISCAL_YEAR_END = date(2025, 9, 30)
AS_OF_DATE = date(2025, 8, 15)

# Synthetic practice name used on the sample invoice / workbook -- fictional, not affiliated
# with any real firm.
FIRM_NAME = "Meridian Advisory Partners"
FIRM_ADDRESS = "1200 Market Street, Suite 900, Washington, DC 20005"


class ContractType(str, Enum):
    """The three billing mechanics common on government/public-sector engagements."""

    FIXED_FEE = "Fixed-Fee"
    TIME_AND_MATERIALS = "Time & Materials"
    COST_PLUS = "Cost-Plus-Fixed-Fee"


class Role(str, Enum):
    """Labor categories used for budgeting, billing, and staffing."""

    PARTNER = "Partner"
    MANAGER = "Manager"
    CONSULTANT = "Consultant"
    ASSOCIATE = "Associate"


ROLE_ORDER: list[Role] = [Role.PARTNER, Role.MANAGER, Role.CONSULTANT, Role.ASSOCIATE]

# Standard rate card: what the practice bills clients per hour, by labor category.
STANDARD_BILL_RATES: dict[Role, float] = {
    Role.PARTNER: 385.0,
    Role.MANAGER: 275.0,
    Role.CONSULTANT: 210.0,
    Role.ASSOCIATE: 155.0,
}

# Fully-loaded internal cost per hour, by labor category (drives margin/realization).
STANDARD_COST_RATES: dict[Role, float] = {
    Role.PARTNER: 190.0,
    Role.MANAGER: 150.0,
    Role.CONSULTANT: 115.0,
    Role.ASSOCIATE: 85.0,
}

# Typical staffing pyramid weights for a professional-services engagement team.
ROLE_STAFFING_WEIGHTS: dict[Role, float] = {
    Role.PARTNER: 0.06,
    Role.MANAGER: 0.16,
    Role.CONSULTANT: 0.44,
    Role.ASSOCIATE: 0.34,
}


@dataclass(frozen=True)
class StaffMember:
    """A person in the practice's staffing pool who can be assigned to engagements."""

    staff_id: str
    name: str
    role: Role

    @property
    def standard_bill_rate(self) -> float:
        return STANDARD_BILL_RATES[self.role]

    @property
    def standard_cost_rate(self) -> float:
        return STANDARD_COST_RATES[self.role]


@dataclass
class RoleBudget:
    """Budgeted hours and dollars for one labor category on one engagement."""

    role: Role
    budgeted_hours: float
    bill_rate: float

    @property
    def budgeted_dollars(self) -> float:
        return round(self.budgeted_hours * self.bill_rate, 2)


@dataclass
class Milestone:
    """A fixed-fee deliverable/billing milestone."""

    name: str
    pct_of_fee: float
    planned_date: date
    status: str = "Not Started"  # Not Started / In Progress / Complete / Invoiced


@dataclass
class ChangeOrder:
    """A client-approved modification to an engagement's contract ceiling.

    This is the authorization that legitimizes spending beyond the original
    budget; an engagement running over its original ceiling with no
    corresponding change order is a control failure, not just a variance.
    """

    change_order_id: str
    amount: float
    approved_date: date
    reason: str


@dataclass
class Engagement:
    """A single client engagement: contract terms, budget, milestones, staffing."""

    engagement_id: str
    client_name: str
    engagement_name: str
    contract_type: ContractType
    engagement_leader: str
    start_date: date
    end_date: date
    role_budgets: dict[Role, RoleBudget]
    assigned_staff: dict[Role, list[str]]
    milestones: list[Milestone] = field(default_factory=list)
    change_orders: list[ChangeOrder] = field(default_factory=list)
    nte_ceiling: float | None = None
    fixed_fee_amount: float | None = None
    cost_plus_fee_pct: float | None = None

    @property
    def total_budget_hours(self) -> float:
        return sum(rb.budgeted_hours for rb in self.role_budgets.values())

    @property
    def total_budget_dollars(self) -> float:
        return round(sum(rb.budgeted_dollars for rb in self.role_budgets.values()), 2)

    @property
    def contract_ceiling_dollars(self) -> float:
        """The originally authorized dollar ceiling, before any change orders."""
        if self.contract_type == ContractType.FIXED_FEE:
            return self.fixed_fee_amount or self.total_budget_dollars
        if self.nte_ceiling is not None:
            return self.nte_ceiling
        return self.total_budget_dollars

    @property
    def approved_ceiling_dollars(self) -> float:
        """The current authorized ceiling, including any approved change orders."""
        return round(self.contract_ceiling_dollars + sum(co.amount for co in self.change_orders), 2)

    def is_active_as_of(self, as_of: date) -> bool:
        return self.start_date <= as_of

    def month_range(self, as_of: date = AS_OF_DATE) -> list[date]:
        """First-of-month dates the engagement has been open for, through as_of."""
        end = min(self.end_date, as_of)
        if self.start_date > end:
            return []
        months = []
        cursor = date(self.start_date.year, self.start_date.month, 1)
        end_marker = date(end.year, end.month, 1)
        while cursor <= end_marker:
            months.append(cursor)
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)
        return months


# --------------------------------------------------------------------------
# Synthetic client / engagement naming
# --------------------------------------------------------------------------

_CLIENTS: list[tuple[str, str]] = [
    ("Commonwealth Department of Transportation", "DOT"),
    ("Bureau of Health & Human Services", "BHHS"),
    ("Federal Logistics & Readiness Agency", "FLRA"),
    ("Ashford County Government", "ACG"),
    ("Metro Regional Transit Authority", "MRTA"),
]

_ENGAGEMENT_TEMPLATES: dict[str, list[str]] = {
    "DOT": [
        "Highway Asset Management Program Support",
        "Capital Program Financial Advisory",
        "Fleet Maintenance Cost Optimization Study",
        "Grants Compliance & Reporting Support",
    ],
    "BHHS": [
        "Medicaid Eligibility System Modernization PMO",
        "Provider Payment Integrity Review",
        "Benefits Program Financial Audit Support",
        "Case Management System Implementation Support",
    ],
    "FLRA": [
        "Logistics Readiness ERP Modernization PMO",
        "Supply Chain Cost Accounting Advisory",
        "Acquisition Support Services",
        "Financial Systems Audit Readiness",
    ],
    "ACG": [
        "County Financial Systems Upgrade Support",
        "Procurement Process Improvement Study",
        "Budget Office Advisory Services",
    ],
    "MRTA": [
        "Capital Projects Cost Control Advisory",
        "Fare System Financial Reconciliation Support",
        "Transit Asset Management Program Support",
    ],
}

_LEADER_POOL = [
    "M. Alvarez", "R. Chen", "S. Whitfield", "D. Okoro", "J. Patel",
]

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Sam", "Drew",
    "Jamie", "Reese", "Avery", "Quinn", "Rowan", "Cameron", "Emerson",
    "Hayden", "Skyler", "Parker", "Elliot", "Dakota", "Blake", "Finley",
    "Harper", "Kendall", "Marlowe", "Sawyer",
]
_LAST_NAMES = [
    "Bennett", "Cho", "Delgado", "Ferris", "Grant", "Huang", "Ibarra",
    "Jansen", "Kowalski", "Larsen", "Mbeki", "Nakamura", "O'Reilly",
    "Prakash", "Quintero", "Reyes", "Sato", "Torres", "Uwimana", "Vance",
    "Weiss", "Xu", "Yilmaz", "Zamora", "Abernathy", "Brennan",
]


def _generate_staff_pool(rng: random.Random) -> list[StaffMember]:
    """Build the practice's staffing pool: a fixed headcount per labor category."""
    headcount = {Role.PARTNER: 4, Role.MANAGER: 6, Role.CONSULTANT: 9, Role.ASSOCIATE: 7}
    names = list(zip(_FIRST_NAMES, _LAST_NAMES))
    rng.shuffle(names)
    staff: list[StaffMember] = []
    idx = 0
    for role in ROLE_ORDER:
        for _ in range(headcount[role]):
            first, last = names[idx]
            idx += 1
            staff_id = f"STF-{idx:03d}"
            staff.append(StaffMember(staff_id=staff_id, name=f"{first} {last}", role=role))
    return staff


def _pick_staff_for_role(rng: random.Random, staff: list[StaffMember], role: Role, count: int) -> list[str]:
    pool = [s.staff_id for s in staff if s.role == role]
    count = min(count, len(pool))
    return sorted(rng.sample(pool, k=count))


def _build_role_budgets(rng: random.Random, total_hours: float) -> dict[Role, RoleBudget]:
    role_budgets: dict[Role, RoleBudget] = {}
    for role in ROLE_ORDER:
        weight = ROLE_STAFFING_WEIGHTS[role] * rng.uniform(0.85, 1.15)
        hours = round(total_hours * weight / sum(ROLE_STAFFING_WEIGHTS.values()), 0)
        role_budgets[role] = RoleBudget(role=role, budgeted_hours=hours, bill_rate=STANDARD_BILL_RATES[role])
    return role_budgets


def _build_milestones(rng: random.Random, start: date, end: date, count: int) -> list[Milestone]:
    duration_days = (end - start).days
    names = ["Kickoff & Mobilization", "Interim Deliverable", "Draft Report", "Final Report & Handoff", "Closeout"]
    chosen = names[: count - 1] + [names[-1]] if count < len(names) else names
    remaining = 100.0
    milestones = []
    weights = [rng.uniform(0.8, 1.2) for _ in chosen]
    total_w = sum(weights)
    for i, (name, w) in enumerate(zip(chosen, weights)):
        pct = round(100.0 * w / total_w, 1)
        if i == len(chosen) - 1:
            pct = round(remaining, 1)
        remaining -= pct
        offset_days = int(duration_days * (i + 1) / len(chosen))
        milestones.append(Milestone(name=name, pct_of_fee=pct, planned_date=start + timedelta(days=offset_days)))
    return milestones


def generate_portfolio(seed: int = 42) -> tuple[list[Engagement], list[StaffMember]]:
    """Generate the synthetic 18-engagement portfolio and its staffing pool.

    Deterministic given ``seed`` so downstream modules and tests can rely on
    a stable, reproducible dataset.
    """
    rng = random.Random(seed)
    staff = _generate_staff_pool(rng)

    contract_mix: list[ContractType] = (
        [ContractType.TIME_AND_MATERIALS] * 7
        + [ContractType.COST_PLUS] * 6
        + [ContractType.FIXED_FEE] * 5
    )
    rng.shuffle(contract_mix)

    engagements: list[Engagement] = []
    eng_counter = 0
    template_cursors = {abbr: 0 for _, abbr in _CLIENTS}

    for i, contract_type in enumerate(contract_mix):
        client_name, abbr = _CLIENTS[i % len(_CLIENTS)]
        templates = _ENGAGEMENT_TEMPLATES[abbr]
        cursor = template_cursors[abbr] % len(templates)
        template_cursors[abbr] += 1
        engagement_name = templates[cursor]

        eng_counter += 1
        engagement_id = f"ENG-{eng_counter:03d}"

        start_offset = rng.randint(0, 270)
        start_date = FISCAL_YEAR_START + timedelta(days=start_offset)
        if contract_type == ContractType.FIXED_FEE:
            duration_days = rng.randint(90, 270)
        else:
            duration_days = rng.randint(180, 540)
        end_date = start_date + timedelta(days=duration_days)

        size_factor = rng.choice([0.6, 1.0, 1.0, 1.6, 2.2])
        total_hours = round(1800 * size_factor * (duration_days / 365))
        role_budgets = _build_role_budgets(rng, total_hours)

        assigned_staff = {
            role: _pick_staff_for_role(rng, staff, role, count=rng.randint(1, 3) if role != Role.PARTNER else 1)
            for role in ROLE_ORDER
        }

        engagement = Engagement(
            engagement_id=engagement_id,
            client_name=client_name,
            engagement_name=engagement_name,
            contract_type=contract_type,
            engagement_leader=rng.choice(_LEADER_POOL),
            start_date=start_date,
            end_date=end_date,
            role_budgets=role_budgets,
            assigned_staff=assigned_staff,
        )

        if contract_type == ContractType.FIXED_FEE:
            engagement.fixed_fee_amount = engagement.total_budget_dollars
            engagement.milestones = _build_milestones(rng, start_date, end_date, count=rng.randint(3, 5))
        elif contract_type == ContractType.COST_PLUS:
            engagement.cost_plus_fee_pct = round(rng.uniform(0.08, 0.12), 3)
            cost_pool = sum(rb.budgeted_hours * STANDARD_COST_RATES[rb.role] for rb in role_budgets.values())
            engagement.nte_ceiling = round(cost_pool * (1 + engagement.cost_plus_fee_pct) * rng.uniform(1.0, 1.08), 2)
        else:  # T&M
            engagement.nte_ceiling = round(engagement.total_budget_dollars * rng.uniform(1.0, 1.1), 2)

        # ~30% of engagements have client-approved scope growth, formalized as a change order.
        if rng.random() < 0.30:
            co_amount = round(engagement.contract_ceiling_dollars * rng.uniform(0.08, 0.22), 2)
            co_date = start_date + timedelta(days=int((end_date - start_date).days * rng.uniform(0.3, 0.7)))
            engagement.change_orders.append(
                ChangeOrder(
                    change_order_id=f"{engagement_id}-CO01",
                    amount=co_amount,
                    approved_date=co_date,
                    reason=rng.choice(
                        [
                            "Expanded scope: additional deliverable requested by client",
                            "Extension of period of performance",
                            "Additional staffing approved for accelerated timeline",
                        ]
                    ),
                )
            )

        engagements.append(engagement)

    return engagements, staff
