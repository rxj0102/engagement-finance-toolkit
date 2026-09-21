"""Reporting: assembles the portfolio Excel workbook.

Every derived metric in the workbook (utilization, realization, ceiling
consumption, status, exception roll-ups) is written as a native Excel
formula referencing other cells in the workbook -- not a value computed in
Python and pasted in -- so the workbook recalculates correctly if the
underlying budgeted/actual/exception figures on the source tabs change.
Only source data (budgeted hours, recorded actuals, exception findings) is
written as static input values, consistent with standard financial-model
convention (inputs in blue, formulas in black).

Tabs produced:
    1. Engagement Summary   -- one row per engagement, formula-driven KPIs
    2. Budget vs Actual     -- one row per engagement x role x month
    3. Sample Invoice       -- one fully worked T&M invoice example
    4. Discrepancy Report   -- prioritized exceptions, conditional formatting
    5. Portfolio Dashboard  -- KPI tiles + native charts
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet

from .budget_actual import BEHIND_PACE_VARIANCE, OVER_BUDGET_THRESHOLD, TRENDING_OVER_VARIANCE
from .engagement_setup import AS_OF_DATE, Engagement, FIRM_ADDRESS, FIRM_NAME
from .invoicing import Invoice

# --------------------------------------------------------------------------
# Styling constants
# --------------------------------------------------------------------------

FONT_NAME = "Arial"
TITLE_FONT = Font(name=FONT_NAME, size=14, bold=True, color="1F2A44")
SUBTITLE_FONT = Font(name=FONT_NAME, size=10, italic=True, color="555555")
HEADER_FONT = Font(name=FONT_NAME, size=10, bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="1F2A44")
INPUT_FONT = Font(name=FONT_NAME, size=10, color="0000FF")
FORMULA_FONT = Font(name=FONT_NAME, size=10, color="000000")
BOLD_FONT = Font(name=FONT_NAME, size=10, bold=True)
LABEL_FONT = Font(name=FONT_NAME, size=10, bold=True, color="1F2A44")
KPI_LABEL_FONT = Font(name=FONT_NAME, size=9, color="555555")
KPI_VALUE_FONT = Font(name=FONT_NAME, size=18, bold=True, color="1F2A44")
THIN_BORDER = Border(bottom=Side(style="thin", color="D9D9D9"))
WRAP = Alignment(wrap_text=True, vertical="top")

SEVERITY_FILLS = {
    "High": PatternFill("solid", fgColor="F8C6C6"),
    "Medium": PatternFill("solid", fgColor="FCE8B2"),
    "Low": PatternFill("solid", fgColor="D9E6F5"),
}
STATUS_FILLS = {
    "Over Budget": PatternFill("solid", fgColor="F8C6C6"),
    "Trending Over Budget": PatternFill("solid", fgColor="FCE8B2"),
    "Behind Pace / Under-Utilized": PatternFill("solid", fgColor="D9E6F5"),
    "On Track": PatternFill("solid", fgColor="D9F2D9"),
}

CURRENCY_FMT = "$#,##0"
PCT_FMT = "0.0%"
DATE_FMT = "yyyy-mm-dd"


def _title(ws: Worksheet, text: str, subtitle: str | None = None, row: int = 1) -> None:
    ws.cell(row=row, column=1, value=text).font = TITLE_FONT
    if subtitle:
        ws.cell(row=row + 1, column=1, value=subtitle).font = SUBTITLE_FONT


def _write_header_row(ws: Worksheet, headers: list[str], row: int, start_col: int = 1) -> None:
    for i, h in enumerate(headers):
        cell = ws.cell(row=row, column=start_col + i, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30
    ws.freeze_panes = ws.cell(row=row + 1, column=start_col)


def _set_widths(ws: Worksheet, widths: dict[str, int]) -> None:
    for col, width in widths.items():
        ws.column_dimensions[col].width = width


# --------------------------------------------------------------------------
# Tab 1: Engagement Summary
# --------------------------------------------------------------------------

_ES_HEADERS = [
    "Engagement ID", "Client", "Engagement Name", "Contract Type", "Engagement Leader",
    "Start Date", "End Date", "Budgeted Hours", "Budgeted $", "Contract Ceiling $",
    "Change Order $", "Approved Ceiling $", "Schedule % Elapsed", "Actual Hours to Date",
    "Actual $ to Date", "Utilization %", "% of Ceiling Consumed", "Budget/Schedule Variance",
    "Status", "Open Exceptions", "Total Exception $ Impact", "Ceiling Basis $ to Date",
]
_ES_FIRST_DATA_ROW = 4


def build_engagement_summary_sheet(
    wb: Workbook,
    engagements: list[Engagement],
    engagement_summary_df: pd.DataFrame,
    as_of: date,
    ba_last_row: int,
    dq_last_row: int,
) -> None:
    ws = wb.create_sheet("Engagement Summary")
    _title(ws, "Engagement Portfolio Summary", f"Synthetic data, as of {as_of.isoformat()} -- for portfolio demonstration only")
    _write_header_row(ws, _ES_HEADERS, row=3)

    ba_rng = lambda col: f"'Budget vs Actual'!${col}${_BA_FIRST_DATA_ROW}:${col}${ba_last_row}"
    dq_rng = lambda col: f"'Discrepancy Report'!${col}${_DQ_FIRST_DATA_ROW}:${col}${dq_last_row}"

    summary_by_id = engagement_summary_df.set_index("engagement_id")
    for i, engagement in enumerate(engagements):
        r = _ES_FIRST_DATA_ROW + i
        schedule_pct = float(summary_by_id.loc[engagement.engagement_id, "schedule_pct_elapsed"])
        ceiling_basis = float(summary_by_id.loc[engagement.engagement_id, "actual_ceiling_basis_to_date"])
        co_amount = round(sum(co.amount for co in engagement.change_orders), 2)

        inputs = {
            "A": engagement.engagement_id,
            "B": engagement.client_name,
            "C": engagement.engagement_name,
            "D": engagement.contract_type.value,
            "E": engagement.engagement_leader,
            "F": engagement.start_date,
            "G": engagement.end_date,
            "H": engagement.total_budget_hours,
            "I": engagement.total_budget_dollars,
            "J": engagement.contract_ceiling_dollars,
            "K": co_amount,
            "M": schedule_pct,
            # Cost-plus contracts are priced/capped on cost + fee, not commercial bill rates,
            # so ceiling consumption is measured on this basis rather than column O (bill-rate
            # valued actuals) -- see budget_actual.build_engagement_summary().
            "V": ceiling_basis,
        }
        for col, val in inputs.items():
            cell = ws[f"{col}{r}"]
            cell.value = val
            cell.font = INPUT_FONT
            cell.border = THIN_BORDER
        ws[f"F{r}"].number_format = DATE_FMT
        ws[f"G{r}"].number_format = DATE_FMT
        ws[f"H{r}"].number_format = "#,##0"
        for col in ("I", "J", "K", "V"):
            ws[f"{col}{r}"].number_format = CURRENCY_FMT
        ws[f"M{r}"].number_format = PCT_FMT

        formulas = {
            "L": f"=J{r}+K{r}",
            "N": f"=SUMIFS({ba_rng('F')},{ba_rng('A')},A{r})",
            "O": f"=SUMIFS({ba_rng('I')},{ba_rng('A')},A{r})",
            "P": f'=IF(H{r}=0,"",N{r}/H{r})',
            "Q": f'=IF(L{r}=0,"",V{r}/L{r})',
            "R": f"=P{r}-M{r}",
            "S": (
                f'=IF(P{r}>{OVER_BUDGET_THRESHOLD},"Over Budget",'
                f'IF(R{r}>{TRENDING_OVER_VARIANCE},"Trending Over Budget",'
                f'IF(R{r}<{BEHIND_PACE_VARIANCE},"Behind Pace / Under-Utilized","On Track")))'
            ),
            "T": f"=COUNTIF({dq_rng('B')},A{r})",
            "U": f"=SUMIF({dq_rng('B')},A{r},{dq_rng('G')})",
        }
        for col, formula in formulas.items():
            cell = ws[f"{col}{r}"]
            cell.value = formula
            cell.font = FORMULA_FONT
            cell.border = THIN_BORDER
        for col in ("O", "U"):
            ws[f"{col}{r}"].number_format = CURRENCY_FMT
        for col in ("P", "Q", "R"):
            ws[f"{col}{r}"].number_format = PCT_FMT
        ws[f"N{r}"].number_format = "#,##0.0"

    last_row = _ES_FIRST_DATA_ROW + len(engagements) - 1
    for status_value, fill in STATUS_FILLS.items():
        ws.conditional_formatting.add(
            f"S{_ES_FIRST_DATA_ROW}:S{last_row}",
            CellIsRule(operator="equal", formula=[f'"{status_value}"'], fill=fill),
        )

    _set_widths(
        ws,
        {
            "A": 12, "B": 30, "C": 34, "D": 20, "E": 14, "F": 12, "G": 12, "H": 13,
            "I": 14, "J": 15, "K": 13, "L": 15, "M": 14, "N": 15, "O": 14, "P": 12,
            "Q": 15, "R": 14, "S": 22, "T": 12, "U": 16, "V": 18,
        },
    )


# --------------------------------------------------------------------------
# Tab 2: Budget vs Actual
# --------------------------------------------------------------------------

_BA_HEADERS = [
    "Engagement ID", "Client", "Role", "Month", "Budgeted Hours", "Actual Hours",
    "Utilization %", "Budgeted $", "Actual $ (Standard Rate)", "Actual $ (Recorded Rate)",
    "Rate Realization %",
]
_BA_FIRST_DATA_ROW = 4


def build_budget_actual_sheet(
    wb: Workbook, engagements: list[Engagement], budget_actual_df: pd.DataFrame, as_of: date
) -> int:
    """Returns the last data row written, so other sheets can size their ranges."""
    ws = wb.create_sheet("Budget vs Actual")
    _title(ws, "Budget vs Actual -- by Engagement, Role, Month", f"Synthetic data, as of {as_of.isoformat()}")
    _write_header_row(ws, _BA_HEADERS, row=3)

    client_lookup = {e.engagement_id: e.client_name for e in engagements}
    df = budget_actual_df.sort_values(["engagement_id", "month", "role"]).reset_index(drop=True)

    for i, row in df.iterrows():
        r = _BA_FIRST_DATA_ROW + i
        inputs = {
            "A": row["engagement_id"],
            "B": client_lookup.get(row["engagement_id"], ""),
            "C": row["role"],
            "D": row["month"],
            "E": round(float(row["budgeted_hours"]), 2),
            "F": round(float(row["actual_hours"]), 2),
            "H": round(float(row["budgeted_dollars"]), 2),
            "I": round(float(row["actual_dollars_standard"]), 2),
            "J": round(float(row["actual_dollars_recorded"]), 2),
        }
        for col, val in inputs.items():
            cell = ws[f"{col}{r}"]
            cell.value = val
            cell.font = INPUT_FONT
        ws[f"D{r}"].number_format = DATE_FMT
        for col in ("E", "F"):
            ws[f"{col}{r}"].number_format = "#,##0.0"
        for col in ("H", "I", "J"):
            ws[f"{col}{r}"].number_format = CURRENCY_FMT

        ws[f"G{r}"] = f'=IF(E{r}=0,"",F{r}/E{r})'
        ws[f"K{r}"] = f'=IF(I{r}=0,"",J{r}/I{r})'
        for col in ("G", "K"):
            ws[f"{col}{r}"].font = FORMULA_FONT
            ws[f"{col}{r}"].number_format = PCT_FMT

    last_row = _BA_FIRST_DATA_ROW + len(df) - 1
    _set_widths(ws, {"A": 12, "B": 28, "C": 12, "D": 12, "E": 13, "F": 12, "G": 12, "H": 13, "I": 18, "J": 18, "K": 15})
    return last_row


# --------------------------------------------------------------------------
# Tab 3: Sample Invoice
# --------------------------------------------------------------------------


def _pick_sample_invoice(invoices: list[Invoice]) -> Invoice:
    tm_with_expense = [
        inv for inv in invoices
        if inv.contract_type == "Time & Materials" and any(li.category == "Expense" for li in inv.line_items)
        and any(li.category == "Labor" for li in inv.line_items)
    ]
    if tm_with_expense:
        return max(tm_with_expense, key=lambda inv: inv.total_amount)
    return max(invoices, key=lambda inv: inv.total_amount)


def build_sample_invoice_sheet(wb: Workbook, engagements: list[Engagement], invoices: list[Invoice]) -> None:
    ws = wb.create_sheet("Sample Invoice")
    eng_lookup = {e.engagement_id: e for e in engagements}
    invoice = _pick_sample_invoice(invoices)
    engagement = eng_lookup[invoice.engagement_id]

    ws["A1"] = FIRM_NAME
    ws["A1"].font = Font(name=FONT_NAME, size=16, bold=True, color="1F2A44")
    ws["A2"] = FIRM_ADDRESS
    ws["A2"].font = SUBTITLE_FONT
    ws["A4"] = "CLIENT INVOICE"
    ws["A4"].font = Font(name=FONT_NAME, size=13, bold=True)
    ws["A5"] = "DRAFT -- pending engagement leader review"
    ws["A5"].font = Font(name=FONT_NAME, size=9, italic=True, color="B00000")

    info = [
        ("Invoice #:", invoice.invoice_id, "Invoice Date:", invoice.invoice_date),
        ("Bill To:", invoice.client_name, "Billing Period:", f"{invoice.period_start.isoformat()} to {invoice.period_end.isoformat()}"),
        ("Engagement:", engagement.engagement_name, "Engagement ID:", engagement.engagement_id),
        ("Contract Type:", engagement.contract_type.value, "Engagement Leader:", engagement.engagement_leader),
    ]
    start_row = 7
    for i, (l1, v1, l2, v2) in enumerate(info):
        r = start_row + i
        ws[f"A{r}"] = l1
        ws[f"A{r}"].font = LABEL_FONT
        ws[f"B{r}"] = v1
        ws[f"B{r}"].font = FORMULA_FONT
        ws[f"D{r}"] = l2
        ws[f"D{r}"].font = LABEL_FONT
        ws[f"E{r}"] = v2
        ws[f"E{r}"].font = FORMULA_FONT
        if isinstance(v1, date):
            ws[f"B{r}"].number_format = DATE_FMT
        if isinstance(v2, date):
            ws[f"E{r}"].number_format = DATE_FMT

    header_row = start_row + len(info) + 2
    headers = ["Description", "Category", "Role", "Hours", "Rate", "Amount"]
    _write_header_row(ws, headers, row=header_row)

    r = header_row + 1
    for li in invoice.line_items:
        ws.cell(row=r, column=1, value=li.description).font = FORMULA_FONT
        ws.cell(row=r, column=2, value=li.category).font = FORMULA_FONT
        ws.cell(row=r, column=3, value=li.role or "").font = FORMULA_FONT
        if li.hours is not None and li.rate is not None:
            ws.cell(row=r, column=4, value=li.hours).font = INPUT_FONT
            ws.cell(row=r, column=5, value=li.rate).font = INPUT_FONT
            ws.cell(row=r, column=5).number_format = CURRENCY_FMT
            ws.cell(row=r, column=6, value=f"=D{r}*E{r}").font = FORMULA_FONT
        else:
            ws.cell(row=r, column=6, value=li.amount).font = INPUT_FONT
        ws.cell(row=r, column=6).number_format = CURRENCY_FMT
        r += 1

    last_item_row = r - 1
    r += 1
    labels = [
        ("Subtotal -- Labor", "Labor"),
        ("Subtotal -- Expenses", "Expense"),
        ("Fee", "Fee"),
        ("Milestone Fee", "Milestone"),
    ]
    subtotal_rows = []
    for label, category in labels:
        has_category = any(li.category == category for li in invoice.line_items)
        if not has_category:
            continue
        ws.cell(row=r, column=1, value=label).font = BOLD_FONT
        ws.cell(row=r, column=6, value=f'=SUMIF(B{header_row + 1}:B{last_item_row},"{category}",F{header_row + 1}:F{last_item_row})')
        ws.cell(row=r, column=6).font = FORMULA_FONT
        ws.cell(row=r, column=6).number_format = CURRENCY_FMT
        subtotal_rows.append(r)
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="TOTAL AMOUNT DUE").font = Font(name=FONT_NAME, size=12, bold=True)
    total_formula = "=" + "+".join(f"F{sr}" for sr in subtotal_rows)
    total_cell = ws.cell(row=r, column=6, value=total_formula)
    total_cell.font = Font(name=FONT_NAME, size=12, bold=True)
    total_cell.number_format = CURRENCY_FMT
    total_cell.fill = PatternFill("solid", fgColor="D9E6F5")

    r += 3
    ws.cell(row=r, column=1, value=(
        "This draft invoice reflects hours and expenses recorded as approved and correctly coded in the "
        "firm's timekeeping system as of the billing period close. Every line item traces back to specific "
        "timesheet/expense records; see reconcile_invoice_to_source() in invoicing.py."
    )).font = Font(name=FONT_NAME, size=8, italic=True, color="777777")
    ws.cell(row=r, column=1).alignment = WRAP
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
    ws.row_dimensions[r].height = 30

    _set_widths(ws, {"A": 40, "B": 12, "C": 12, "D": 10, "E": 12, "F": 16})


# --------------------------------------------------------------------------
# Tab 4: Discrepancy Report
# --------------------------------------------------------------------------

_DQ_HEADERS = [
    "Discrepancy ID", "Engagement ID", "Client", "Engagement Leader", "Category",
    "Severity", "Dollar Impact", "Description", "Recommended Action", "Detected Date",
]
_DQ_FIRST_DATA_ROW = 4


def build_discrepancy_report_sheet(wb: Workbook, exception_report_df: pd.DataFrame, leader_summary_df: pd.DataFrame, as_of: date) -> int:
    ws = wb.create_sheet("Discrepancy Report")
    _title(
        ws,
        "Discrepancy & Exception Report",
        f"Prioritized by severity, then dollar impact -- as of {as_of.isoformat()}. Synthetic data for demonstration only.",
    )
    _write_header_row(ws, _DQ_HEADERS, row=3)

    for i, row in exception_report_df.reset_index(drop=True).iterrows():
        r = _DQ_FIRST_DATA_ROW + i
        values = [
            row["discrepancy_id"], row["engagement_id"], row["client_name"], row["engagement_leader"],
            row["category"], row["severity"], round(float(row["dollar_impact"]), 2),
            row["description"], row["recommended_action"], row["detected_date"],
        ]
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = INPUT_FONT
            if c in (8, 9):
                cell.alignment = WRAP
        ws.cell(row=r, column=7).number_format = CURRENCY_FMT
        ws.cell(row=r, column=10).number_format = DATE_FMT

    last_row = max(_DQ_FIRST_DATA_ROW, _DQ_FIRST_DATA_ROW + len(exception_report_df) - 1)
    for severity, fill in SEVERITY_FILLS.items():
        ws.conditional_formatting.add(
            f"F{_DQ_FIRST_DATA_ROW}:F{last_row}",
            CellIsRule(operator="equal", formula=[f'"{severity}"'], fill=fill),
        )

    _set_widths(ws, {"A": 16, "B": 12, "C": 28, "D": 14, "E": 26, "F": 10, "G": 14, "H": 55, "I": 45, "J": 12})

    # Escalation summary by engagement leader, placed to the right of the main table.
    leader_col = 13  # column M
    ws.cell(row=3, column=leader_col, value="Escalation Summary by Engagement Leader").font = Font(
        name=FONT_NAME, size=11, bold=True, color="1F2A44"
    )
    leader_headers = ["Leader", "Open Exceptions", "High", "Medium", "Low", "Total $ Impact", "Engagements Affected"]
    for j, h in enumerate(leader_headers):
        cell = ws.cell(row=4, column=leader_col + j, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for i, row in leader_summary_df.reset_index(drop=True).iterrows():
        r = 5 + i
        vals = [
            row["engagement_leader"], int(row["open_exceptions"]), int(row["high_severity"]),
            int(row["medium_severity"]), int(row["low_severity"]), round(float(row["total_dollar_impact"]), 2),
            int(row["engagements_affected"]),
        ]
        for j, v in enumerate(vals):
            cell = ws.cell(row=r, column=leader_col + j, value=v)
            cell.font = FORMULA_FONT
            if j == 5:
                cell.number_format = CURRENCY_FMT
    for j, width in enumerate([16, 15, 8, 10, 8, 15, 18]):
        ws.column_dimensions[ws.cell(row=4, column=leader_col + j).column_letter].width = width

    return last_row


# --------------------------------------------------------------------------
# Tab 5: Portfolio Dashboard
# --------------------------------------------------------------------------


def _kpi_tile(ws: Worksheet, row: int, col: int, label: str, formula: str, number_format: str) -> None:
    label_cell = ws.cell(row=row, column=col, value=label)
    label_cell.font = KPI_LABEL_FONT
    value_cell = ws.cell(row=row + 1, column=col, value=formula)
    value_cell.font = KPI_VALUE_FONT
    value_cell.number_format = number_format


def build_portfolio_dashboard_sheet(
    wb: Workbook,
    n_engagements: int,
    ba_last_row: int,
    dq_last_row: int,
    budget_actual_df: pd.DataFrame,
    as_of: date,
) -> None:
    ws = wb.create_sheet("Portfolio Dashboard")
    _title(ws, "Portfolio Finance Dashboard", f"Engagement Finance Toolkit -- synthetic data, as of {as_of.isoformat()}")

    es_last_row = _ES_FIRST_DATA_ROW + n_engagements - 1
    dq_first_row = _DQ_FIRST_DATA_ROW

    kpis = [
        ("Total Portfolio Budget", f"=SUM('Engagement Summary'!I{_ES_FIRST_DATA_ROW}:I{es_last_row})", CURRENCY_FMT),
        ("Total Actual $ to Date", f"=SUM('Engagement Summary'!O{_ES_FIRST_DATA_ROW}:O{es_last_row})", CURRENCY_FMT),
        (
            "Portfolio Utilization %",
            f"=SUM('Engagement Summary'!N{_ES_FIRST_DATA_ROW}:N{es_last_row})/SUM('Engagement Summary'!H{_ES_FIRST_DATA_ROW}:H{es_last_row})",
            PCT_FMT,
        ),
        ("Engagements Over Budget", f'=COUNTIF(\'Engagement Summary\'!S{_ES_FIRST_DATA_ROW}:S{es_last_row},"Over Budget")', "0"),
        ("Open Exceptions", f"=COUNTA('Discrepancy Report'!A{dq_first_row}:A{dq_last_row})", "0"),
        ("High-Severity Exceptions", f'=COUNTIF(\'Discrepancy Report\'!F{dq_first_row}:F{dq_last_row},"High")', "0"),
    ]
    for i, (label, formula, fmt) in enumerate(kpis):
        col = 1 + i * 3
        _kpi_tile(ws, 3, col, label, formula, fmt)

    # -- Monthly portfolio trend (utilization % and rate realization % over time) --
    trend_header_row = 8
    ws.cell(row=trend_header_row - 1, column=1, value="Monthly Portfolio Trend").font = Font(
        name=FONT_NAME, size=11, bold=True, color="1F2A44"
    )
    trend_headers = ["Month", "Budgeted Hours", "Actual Hours", "Utilization %", "Actual $ (Standard)", "Actual $ (Recorded)", "Realization %"]
    for j, h in enumerate(trend_headers):
        cell = ws.cell(row=trend_header_row, column=1 + j, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL

    ba_rng = lambda col: f"'Budget vs Actual'!${col}${_BA_FIRST_DATA_ROW}:${col}${ba_last_row}"
    months = sorted(budget_actual_df["month"].unique())
    trend_first_row = trend_header_row + 1
    for i, month in enumerate(months):
        r = trend_first_row + i
        ws.cell(row=r, column=1, value=month).number_format = DATE_FMT
        ws.cell(row=r, column=1).font = INPUT_FONT
        ws.cell(row=r, column=2, value=f"=SUMIFS({ba_rng('E')},{ba_rng('D')},A{r})")
        ws.cell(row=r, column=3, value=f"=SUMIFS({ba_rng('F')},{ba_rng('D')},A{r})")
        ws.cell(row=r, column=4, value=f'=IF(B{r}=0,"",C{r}/B{r})')
        ws.cell(row=r, column=5, value=f"=SUMIFS({ba_rng('I')},{ba_rng('D')},A{r})")
        ws.cell(row=r, column=6, value=f"=SUMIFS({ba_rng('J')},{ba_rng('D')},A{r})")
        ws.cell(row=r, column=7, value=f'=IF(E{r}=0,"",F{r}/E{r})')
        for col in (2, 3):
            ws.cell(row=r, column=col).number_format = "#,##0"
            ws.cell(row=r, column=col).font = FORMULA_FONT
        for col in (5, 6):
            ws.cell(row=r, column=col).number_format = CURRENCY_FMT
            ws.cell(row=r, column=col).font = FORMULA_FONT
        for col in (4, 7):
            ws.cell(row=r, column=col).number_format = PCT_FMT
            ws.cell(row=r, column=col).font = FORMULA_FONT
    trend_last_row = trend_first_row + len(months) - 1

    # -- Open exceptions by severity --
    sev_header_row = trend_last_row + 3
    ws.cell(row=sev_header_row - 1, column=1, value="Open Exceptions by Severity").font = Font(
        name=FONT_NAME, size=11, bold=True, color="1F2A44"
    )
    sev_headers = ["Severity", "Count", "$ Impact"]
    for j, h in enumerate(sev_headers):
        cell = ws.cell(row=sev_header_row, column=1 + j, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for i, severity in enumerate(["High", "Medium", "Low"]):
        r = sev_header_row + 1 + i
        ws.cell(row=r, column=1, value=severity).font = INPUT_FONT
        ws.cell(row=r, column=2, value=f'=COUNTIF(\'Discrepancy Report\'!F{dq_first_row}:F{dq_last_row},A{r})').font = FORMULA_FONT
        ws.cell(row=r, column=3, value=f'=SUMIF(\'Discrepancy Report\'!F{dq_first_row}:F{dq_last_row},A{r},\'Discrepancy Report\'!G{dq_first_row}:G{dq_last_row})').font = FORMULA_FONT
        ws.cell(row=r, column=3).number_format = CURRENCY_FMT
    sev_last_row = sev_header_row + 3

    # -- Charts --
    util_chart = BarChart()
    util_chart.title = "Portfolio Utilization % by Engagement"
    util_chart.y_axis.title = "Utilization %"
    util_chart.y_axis.numFmt = "0%"
    util_chart.x_axis.title = "Engagement"
    util_data = Reference(wb["Engagement Summary"], min_col=16, min_row=3, max_row=es_last_row)
    util_cats = Reference(wb["Engagement Summary"], min_col=1, min_row=_ES_FIRST_DATA_ROW, max_row=es_last_row)
    util_chart.add_data(util_data, titles_from_data=True)
    util_chart.set_categories(util_cats)
    util_chart.height, util_chart.width = 9, 18
    ws.add_chart(util_chart, f"A{sev_last_row + 3}")

    trend_chart = LineChart()
    trend_chart.title = "Monthly Utilization % vs. Rate Realization % -- Portfolio"
    trend_chart.y_axis.title = "%"
    trend_chart.y_axis.numFmt = "0%"
    trend_chart.x_axis.title = "Month"
    trend_data = Reference(ws, min_col=4, max_col=4, min_row=trend_header_row, max_row=trend_last_row)
    trend_data2 = Reference(ws, min_col=7, max_col=7, min_row=trend_header_row, max_row=trend_last_row)
    trend_cats = Reference(ws, min_col=1, min_row=trend_first_row, max_row=trend_last_row)
    trend_chart.add_data(trend_data, titles_from_data=True)
    trend_chart.add_data(trend_data2, titles_from_data=True)
    trend_chart.set_categories(trend_cats)
    trend_chart.height, trend_chart.width = 9, 18
    ws.add_chart(trend_chart, f"K{sev_last_row + 3}")

    sev_chart = BarChart()
    sev_chart.title = "Open Exceptions by Severity"
    sev_chart.y_axis.title = "Count"
    sev_data = Reference(ws, min_col=2, min_row=sev_header_row, max_row=sev_last_row)
    sev_cats = Reference(ws, min_col=1, min_row=sev_header_row + 1, max_row=sev_last_row)
    sev_chart.add_data(sev_data, titles_from_data=True)
    sev_chart.set_categories(sev_cats)
    sev_chart.height, sev_chart.width = 9, 18
    ws.add_chart(sev_chart, f"A{sev_last_row + 22}")

    _set_widths(ws, {"A": 16, "B": 15, "C": 14, "D": 13, "E": 16, "F": 16, "G": 13})


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def build_workbook(
    engagements: list[Engagement],
    budget_actual_df: pd.DataFrame,
    engagement_summary_df: pd.DataFrame,
    invoices: list[Invoice],
    exception_report_df: pd.DataFrame,
    leader_summary_df: pd.DataFrame,
    as_of: date = AS_OF_DATE,
    output_path: str = "output/engagement_finance_toolkit.xlsx",
) -> str:
    """Build and save the full portfolio Excel workbook. Returns the output path."""
    wb = Workbook()
    wb.remove(wb.active)

    # Row counts are known from the source DataFrames before any sheet is written, so
    # cross-sheet formulas can use bounded ranges (fast) instead of whole-column refs (slow).
    ba_last_row = _BA_FIRST_DATA_ROW + len(budget_actual_df) - 1
    dq_last_row = max(_DQ_FIRST_DATA_ROW, _DQ_FIRST_DATA_ROW + len(exception_report_df) - 1)

    build_engagement_summary_sheet(wb, engagements, engagement_summary_df, as_of, ba_last_row, dq_last_row)
    actual_ba_last_row = build_budget_actual_sheet(wb, engagements, budget_actual_df, as_of)
    build_sample_invoice_sheet(wb, engagements, invoices)
    actual_dq_last_row = build_discrepancy_report_sheet(wb, exception_report_df, leader_summary_df, as_of)
    assert actual_ba_last_row == ba_last_row and actual_dq_last_row == dq_last_row
    build_portfolio_dashboard_sheet(wb, len(engagements), ba_last_row, dq_last_row, budget_actual_df, as_of)

    wb.move_sheet("Portfolio Dashboard", offset=-4)

    wb.save(output_path)
    return output_path
