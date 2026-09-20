"""Invariant checks for the engagement finance pipeline.

These aren't exhaustive unit tests of every branch -- they check the things
that would actually matter if this were real financial data: budget-to-
actual math ties out, every draft invoice reconciles exactly to its source
timesheet/expense records, and the discrepancy detectors produce sane,
non-negative findings.
"""

from __future__ import annotations

import pandas as pd
import pytest

from engagement_finance_toolkit.budget_actual import build_budget_actual_by_role_month, build_engagement_summary
from engagement_finance_toolkit.discrepancy_detection import build_exception_report, run_all_detectors
from engagement_finance_toolkit.engagement_setup import ContractType, generate_portfolio
from engagement_finance_toolkit.invoicing import generate_invoices, reconcile_invoice_to_source
from engagement_finance_toolkit.time_expense_tracking import (
    expenses_to_dataframe,
    generate_expenses,
    generate_timesheets,
    timesheets_to_dataframe,
)


@pytest.fixture(scope="module")
def pipeline():
    engagements, staff = generate_portfolio()
    timesheet_df = timesheets_to_dataframe(generate_timesheets(engagements, staff))
    expense_df = expenses_to_dataframe(generate_expenses(engagements, staff))
    budget_actual_df = build_budget_actual_by_role_month(engagements, timesheet_df)
    engagement_summary_df = build_engagement_summary(engagements, timesheet_df)
    invoices = generate_invoices(engagements, timesheet_df, expense_df)
    findings = run_all_detectors(engagements, timesheet_df, expense_df, invoices, engagement_summary_df)
    exception_report_df = build_exception_report(findings)
    return {
        "engagements": engagements,
        "staff": staff,
        "timesheet_df": timesheet_df,
        "expense_df": expense_df,
        "budget_actual_df": budget_actual_df,
        "engagement_summary_df": engagement_summary_df,
        "invoices": invoices,
        "exception_report_df": exception_report_df,
    }


def test_portfolio_size_and_mix(pipeline):
    engagements = pipeline["engagements"]
    assert len(engagements) == 18
    counts = pd.Series([e.contract_type for e in engagements]).value_counts()
    assert counts[ContractType.TIME_AND_MATERIALS] == 7
    assert counts[ContractType.COST_PLUS] == 6
    assert counts[ContractType.FIXED_FEE] == 5


def test_portfolio_generation_is_deterministic():
    engagements_a, _ = generate_portfolio(seed=42)
    engagements_b, _ = generate_portfolio(seed=42)
    ids_a = [(e.engagement_id, e.total_budget_hours, e.total_budget_dollars) for e in engagements_a]
    ids_b = [(e.engagement_id, e.total_budget_hours, e.total_budget_dollars) for e in engagements_b]
    assert ids_a == ids_b


def test_budget_actual_hours_tie_out(pipeline):
    """Sum of actual hours in the role/month grid must equal the raw timesheet total."""
    grid_total = pipeline["budget_actual_df"]["actual_hours"].sum()
    raw_total = pipeline["timesheet_df"]["hours"].sum()
    assert grid_total == pytest.approx(raw_total, abs=0.01)


def test_engagement_summary_hours_tie_out(pipeline):
    """Sum of actual hours in the engagement summary must also equal the raw timesheet total."""
    summary_total = pipeline["engagement_summary_df"]["actual_hours_to_date"].sum()
    raw_total = pipeline["timesheet_df"]["hours"].sum()
    assert summary_total == pytest.approx(raw_total, abs=0.5)  # rounding to 1 dp per engagement


def test_every_invoice_reconciles_to_source(pipeline):
    failures = []
    for inv in pipeline["invoices"]:
        result = reconcile_invoice_to_source(inv, pipeline["timesheet_df"], pipeline["expense_df"])
        if not result["ties_out"]:
            failures.append(result)
    assert not failures, f"{len(failures)} invoice(s) failed reconciliation: {failures[:3]}"


def test_no_negative_invoice_amounts(pipeline):
    for inv in pipeline["invoices"]:
        assert inv.total_amount >= 0
        for li in inv.line_items:
            assert li.amount >= 0


def test_discrepancy_categories_are_known(pipeline):
    expected_categories = {
        "Missing Task Code", "Missing Approval", "Rate Mismatch", "Out-of-Period Entry",
        "Unbilled Time Aging", "Unbilled Expense Aging", "Budget Overrun without Change Order",
        "Budget Overrun (Exceeds Approved Change Order)", "Approaching Budget Ceiling",
    }
    report = pipeline["exception_report_df"]
    assert not report.empty
    assert set(report["category"].unique()) <= expected_categories
    assert set(report["severity"].unique()) <= {"High", "Medium", "Low"}


def test_exception_report_sorted_by_severity_then_impact(pipeline):
    report = pipeline["exception_report_df"]
    severity_rank = {"High": 0, "Medium": 1, "Low": 2}
    ranks = report["severity"].map(severity_rank).tolist()
    assert ranks == sorted(ranks)


def test_fixed_fee_milestones_sum_to_full_fee(pipeline):
    for engagement in pipeline["engagements"]:
        if engagement.contract_type != ContractType.FIXED_FEE:
            continue
        total_pct = sum(m.pct_of_fee for m in engagement.milestones)
        assert total_pct == pytest.approx(100.0, abs=0.1)
