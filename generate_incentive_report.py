#!/usr/bin/env python3
"""
Generate the monthly Drilling Performance Incentive Report.

Pulls qualifying footage from drilling_master_data and labor hours from
fct__ops_timesheets, then computes credited footage, ft/day, and payout
per the incentive program rules.

Usage:
    python generate_incentive_report.py [--month YYYY-MM] [--output FILE]

Defaults to January 2026 and writes to drilling_incentive_YYYY_MM.xlsx.
"""

import argparse
import re
import sys
from datetime import date
from typing import Optional

from google.cloud import bigquery
from google.oauth2 import service_account

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CREDENTIALS_PATH = r"C:\Users\Alexander Menke\Desktop\bigquery_mcp\gcp-credentials.json"
PROJECT_ID = "dandelion-tf-production"

# Incentive program parameters
FLOOR_FT_PER_DAY = 150
OTE_FT_PER_DAY = 300
DRILLER_OTE_DAILY = 50.00
HELPER_OTE_DAILY = 25.00
DRILLER_SEG1_RATE = DRILLER_OTE_DAILY / (OTE_FT_PER_DAY - FLOOR_FT_PER_DAY)  # $0.3333/ft
HELPER_SEG1_RATE = HELPER_OTE_DAILY / (OTE_FT_PER_DAY - FLOOR_FT_PER_DAY)    # $0.1667/ft
DRILLER_SEG2_RATE = 0.50
HELPER_SEG2_RATE = 0.25
HELPER_MULTIPLIER = 2

# Project name mapping: fct__ops_timesheets job_name -> drilling_master_data primary
PROJECT_NAME_MAP = {
    "East Creek Farm": "East Creek",
    "Bell 10 Old Lane Callum": "Bell - 10 Old Lane (Callum)",
}


def get_client() -> bigquery.Client:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return bigquery.Client(project=PROJECT_ID, credentials=creds)


# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------


def get_qualifying_footage(client: bigquery.Client, month_start: str, month_end: str) -> list[dict]:
    """Get qualifying footage per project for the month.

    Footage counts when both status='Complete' and qa_status IN ('Pass 1','Pass 2'),
    credited in the month the qa_date falls.
    """
    query = """
    SELECT
        primary as project_name,
        SUM(actual_bore_depth) as total_footage,
        COUNT(*) as bore_count
    FROM reporting.drilling_master_data
    WHERE status = 'Complete'
      AND qa_status IN ('Pass 1', 'Pass 2')
      AND qa_date >= @month_start AND qa_date < @month_end
      AND actual_bore_depth IS NOT NULL
    GROUP BY primary
    ORDER BY primary
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
            bigquery.ScalarQueryParameter("month_end", "DATE", month_end),
        ]
    )
    return [dict(row) for row in client.query(query, job_config=job_config).result()]


def get_labor_hours(client: bigquery.Client, month_start: str, month_end: str) -> list[dict]:
    """Get per-person, per-project drilling labor hours for the month.

    Uses fct__ops_timesheets filtered to drilling department.
    Only counts days where the person was assigned to a project (not internal/None).
    """
    query = """
    SELECT
        first_name,
        last_name,
        job_name,
        task_type,
        COUNT(DISTINCT timesheet_date) as working_days,
        SUM(total_hours) as total_hours
    FROM dbt_dandelion_operations.fct__ops_timesheets
    WHERE timesheet_date >= @month_start AND timesheet_date < @month_end
      AND department LIKE '%Drilling%'
      AND job_name IS NOT NULL
      AND job_name != 'Dandelion Internal Project'
    GROUP BY first_name, last_name, job_name, task_type
    ORDER BY last_name, first_name, job_name
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
            bigquery.ScalarQueryParameter("month_end", "DATE", month_end),
        ]
    )
    return [dict(row) for row in client.query(query, job_config=job_config).result()]


def get_total_working_days(client: bigquery.Client, month_start: str, month_end: str) -> list[dict]:
    """Get total working days per person (including internal project days for the denominator)."""
    query = """
    SELECT
        first_name,
        last_name,
        COUNT(DISTINCT timesheet_date) as total_working_days
    FROM dbt_dandelion_operations.fct__ops_timesheets
    WHERE timesheet_date >= @month_start AND timesheet_date < @month_end
      AND department LIKE '%Drilling%'
      AND job_name IS NOT NULL
      AND job_name != 'Dandelion Internal Project'
    GROUP BY first_name, last_name
    ORDER BY last_name
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
            bigquery.ScalarQueryParameter("month_end", "DATE", month_end),
        ]
    )
    return [dict(row) for row in client.query(query, job_config=job_config).result()]


# ---------------------------------------------------------------------------
# Incentive calculation
# ---------------------------------------------------------------------------


def classify_role(task_types: set[str]) -> str:
    """Classify a person as Driller or Helper based on their task types."""
    if "Drilling" in task_types:
        return "Driller"
    if "Driller Helper" in task_types:
        return "Helper"
    return "Unknown"


def map_project_name(timesheet_name: str) -> str:
    """Map timesheet job_name to drilling_master_data primary name."""
    return PROJECT_NAME_MAP.get(timesheet_name, timesheet_name)


def compute_daily_rate(ft_per_day: float, role: str) -> float:
    """Compute the incentive daily rate given ft/day and role."""
    if ft_per_day < FLOOR_FT_PER_DAY:
        return 0.0

    if role == "Driller":
        if ft_per_day <= OTE_FT_PER_DAY:
            return (ft_per_day - FLOOR_FT_PER_DAY) * DRILLER_SEG1_RATE
        else:
            return DRILLER_OTE_DAILY + (ft_per_day - OTE_FT_PER_DAY) * DRILLER_SEG2_RATE
    else:  # Helper
        if ft_per_day <= OTE_FT_PER_DAY:
            return (ft_per_day - FLOOR_FT_PER_DAY) * HELPER_SEG1_RATE
        else:
            return HELPER_OTE_DAILY + (ft_per_day - OTE_FT_PER_DAY) * HELPER_SEG2_RATE


def compute_incentives(footage_data: list[dict], labor_data: list[dict],
                       working_days_data: list[dict]) -> list[dict]:
    """Compute incentive payouts per person."""

    # Build footage lookup by project name
    footage_by_project = {}
    for row in footage_data:
        footage_by_project[row["project_name"]] = row["total_footage"]

    # Build working days lookup
    days_lookup = {}
    for row in working_days_data:
        key = (row["first_name"], row["last_name"])
        days_lookup[key] = row["total_working_days"]

    # Aggregate labor: per person, per project, per role (based on task_type per entry)
    # Each labor row has a task_type; classify role from that specific entry.
    # A person can be a Driller on one project and a Helper on another.
    person_entries = {}  # {(first, last): [(project, role, hours), ...]}
    for row in labor_data:
        key = (row["first_name"], row["last_name"])
        if key not in person_entries:
            person_entries[key] = []

        project = map_project_name(row["job_name"])
        entry_role = classify_role({row["task_type"]} if row["task_type"] else set())
        if entry_role == "Unknown":
            # Non-drilling tasks (Admin, Training, Warehouse, etc.) — skip for attribution
            continue
        person_entries[key].append((project, entry_role, row["total_hours"]))

    # Aggregate per person: hours by (project, role), and determine primary role
    person_data = {}
    for key, entries in person_entries.items():
        proj_role_hours = {}  # {(project, role): hours}
        role_hours = {"Driller": 0.0, "Helper": 0.0}
        for project, role, hours in entries:
            pr = (project, role)
            proj_role_hours[pr] = proj_role_hours.get(pr, 0.0) + hours
            role_hours[role] += hours
        # Primary role = whichever has more hours
        primary_role = "Driller" if role_hours["Driller"] >= role_hours["Helper"] else "Helper"
        person_data[key] = {
            "primary_role": primary_role,
            "proj_role_hours": proj_role_hours,
        }

    # Compute total hours per project per role (for proportional attribution)
    project_role_totals = {}  # {(project, role): total_hours across all people}
    for key, data in person_data.items():
        for (project, role), hours in data["proj_role_hours"].items():
            prkey = (project, role)
            project_role_totals[prkey] = project_role_totals.get(prkey, 0.0) + hours

    # Compute credited footage per person
    results = []
    for (first, last), data in sorted(person_data.items(), key=lambda x: x[0][1]):
        primary_role = data["primary_role"]

        working_days = days_lookup.get((first, last), 0)
        if working_days == 0:
            continue

        total_credited = 0.0
        project_details = []

        for (project, role), hours in data["proj_role_hours"].items():
            project_footage = footage_by_project.get(project, 0.0)
            if project_footage == 0:
                project_details.append({
                    "project": project,
                    "role_on_project": role,
                    "hours": hours,
                    "project_footage": 0.0,
                    "share": 0.0,
                    "credited_footage": 0.0,
                })
                continue

            role_total = project_role_totals.get((project, role), hours)
            share = hours / role_total if role_total > 0 else 0

            if role == "Helper":
                credited = share * HELPER_MULTIPLIER * project_footage
            else:
                credited = share * project_footage

            total_credited += credited
            project_details.append({
                "project": project,
                "role_on_project": role,
                "hours": hours,
                "project_footage": project_footage,
                "share": share,
                "credited_footage": credited,
            })

        ft_per_day = total_credited / working_days if working_days > 0 else 0
        daily_rate = compute_daily_rate(ft_per_day, primary_role)
        monthly_payout = daily_rate * working_days

        results.append({
            "first_name": first,
            "last_name": last,
            "role": primary_role,
            "working_days": working_days,
            "total_credited_footage": round(total_credited, 1),
            "ft_per_day": round(ft_per_day, 1),
            "daily_rate": round(daily_rate, 2),
            "monthly_payout": round(monthly_payout, 2),
            "project_details": project_details,
        })

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_excel(results: list[dict], footage_data: list[dict],
                month_label: str, output_path: str):
    """Write the incentive report to an Excel file."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, numbers
    except ImportError:
        print("ERROR: openpyxl is required. Install with: pip install openpyxl")
        sys.exit(1)

    wb = openpyxl.Workbook()

    # --- Summary sheet ---
    ws = wb.active
    ws.title = "Incentive Summary"

    header_font = Font(bold=True, size=12)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_text = Font(bold=True, color="FFFFFF")
    money_fmt = '#,##0.00'
    number_fmt = '#,##0.0'

    ws.merge_cells("A1:H1")
    ws["A1"] = f"Drilling Performance Incentive Report — {month_label}"
    ws["A1"].font = Font(bold=True, size=14)

    headers = ["Name", "Role", "Working Days", "Credited Footage (ft)",
               "Ft / Working Day", "Daily Rate ($)", "Monthly Payout ($)"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col, value=h)
        cell.font = header_text
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for i, r in enumerate(results, 4):
        ws.cell(row=i, column=1, value=f"{r['first_name']} {r['last_name']}")
        ws.cell(row=i, column=2, value=r["role"])
        ws.cell(row=i, column=3, value=r["working_days"])
        ws.cell(row=i, column=4, value=r["total_credited_footage"]).number_format = number_fmt
        ws.cell(row=i, column=5, value=r["ft_per_day"]).number_format = number_fmt
        ws.cell(row=i, column=6, value=r["daily_rate"]).number_format = money_fmt
        ws.cell(row=i, column=7, value=r["monthly_payout"]).number_format = money_fmt

    # Totals row
    total_row = len(results) + 4
    ws.cell(row=total_row, column=1, value="TOTAL").font = Font(bold=True)
    ws.cell(row=total_row, column=7,
            value=sum(r["monthly_payout"] for r in results)).number_format = money_fmt
    ws.cell(row=total_row, column=7).font = Font(bold=True)

    # Auto-width (skip merged cells)
    for col in ws.columns:
        cells = [c for c in col if not isinstance(c, openpyxl.cell.cell.MergedCell)]
        if not cells:
            continue
        max_len = max(len(str(cell.value or "")) for cell in cells)
        ws.column_dimensions[cells[0].column_letter].width = min(max_len + 3, 35)

    # --- Project detail sheet ---
    ws2 = wb.create_sheet("Project Details")

    headers2 = ["Name", "Primary Role", "Role on Project", "Project", "Hours on Project",
                 "Project Total Footage", "Share (%)", "Credited Footage"]
    for col, h in enumerate(headers2, 1):
        cell = ws2.cell(row=1, column=col, value=h)
        cell.font = header_text
        cell.fill = header_fill

    row_num = 2
    for r in results:
        for pd in r["project_details"]:
            ws2.cell(row=row_num, column=1, value=f"{r['first_name']} {r['last_name']}")
            ws2.cell(row=row_num, column=2, value=r["role"])
            ws2.cell(row=row_num, column=3, value=pd.get("role_on_project", r["role"]))
            ws2.cell(row=row_num, column=4, value=pd["project"])
            ws2.cell(row=row_num, column=5, value=round(pd["hours"], 1))
            ws2.cell(row=row_num, column=6, value=round(pd["project_footage"], 1))
            ws2.cell(row=row_num, column=7,
                     value=round(pd["share"] * 100, 1)).number_format = '0.0'
            ws2.cell(row=row_num, column=8,
                     value=round(pd["credited_footage"], 1)).number_format = number_fmt
            row_num += 1

    for col in ws2.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws2.column_dimensions[col[0].column_letter].width = min(max_len + 3, 40)

    # --- Qualifying footage sheet ---
    ws3 = wb.create_sheet("Qualifying Footage")

    headers3 = ["Project", "Total Footage (ft)", "Bore Count"]
    for col, h in enumerate(headers3, 1):
        cell = ws3.cell(row=1, column=col, value=h)
        cell.font = header_text
        cell.fill = header_fill

    for i, f in enumerate(footage_data, 2):
        ws3.cell(row=i, column=1, value=f["project_name"])
        ws3.cell(row=i, column=2, value=f["total_footage"]).number_format = number_fmt
        ws3.cell(row=i, column=3, value=f["bore_count"])

    total_row3 = len(footage_data) + 2
    ws3.cell(row=total_row3, column=1, value="TOTAL").font = Font(bold=True)
    ws3.cell(row=total_row3, column=2,
             value=sum(f["total_footage"] for f in footage_data)).number_format = number_fmt
    ws3.cell(row=total_row3, column=2).font = Font(bold=True)

    for col in ws3.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws3.column_dimensions[col[0].column_letter].width = min(max_len + 3, 40)

    wb.save(output_path)
    print(f"Report saved to: {output_path}")


def print_summary(results: list[dict]):
    """Print a text summary to stdout."""
    print(f"\n{'Name':<25} {'Role':<10} {'Days':>5} {'Footage':>10} {'Ft/Day':>8} {'$/Day':>8} {'Payout':>10}")
    print("-" * 80)
    for r in results:
        print(f"{r['first_name'] + ' ' + r['last_name']:<25} {r['role']:<10} "
              f"{r['working_days']:>5} {r['total_credited_footage']:>10.1f} "
              f"{r['ft_per_day']:>8.1f} {r['daily_rate']:>8.2f} "
              f"${r['monthly_payout']:>9.2f}")
    print("-" * 80)
    total = sum(r["monthly_payout"] for r in results)
    print(f"{'TOTAL':<25} {'':<10} {'':<5} {'':<10} {'':<8} {'':<8} ${total:>9.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Generate Drilling Incentive Report")
    parser.add_argument("--month", default="2026-01",
                        help="Month to generate report for (YYYY-MM format)")
    parser.add_argument("--output", default=None,
                        help="Output Excel file path")
    args = parser.parse_args()

    # Parse month
    match = re.match(r"(\d{4})-(\d{2})", args.month)
    if not match:
        print("ERROR: Month must be in YYYY-MM format")
        sys.exit(1)

    year, month = int(match.group(1)), int(match.group(2))
    month_start = f"{year}-{month:02d}-01"
    if month == 12:
        month_end = f"{year + 1}-01-01"
    else:
        month_end = f"{year}-{month + 1:02d}-01"
    month_label = date(year, month, 1).strftime("%B %Y")

    output_path = args.output or f"drilling_incentive_{year}_{month:02d}.xlsx"

    print(f"Generating incentive report for {month_label}...")

    client = get_client()

    print("  Fetching qualifying footage...")
    footage_data = get_qualifying_footage(client, month_start, month_end)
    total_ft = sum(f["total_footage"] for f in footage_data)
    total_bores = sum(f["bore_count"] for f in footage_data)
    print(f"  Found {total_bores} qualifying bores, {total_ft:,.0f} total ft across {len(footage_data)} projects")

    print("  Fetching labor hours...")
    labor_data = get_labor_hours(client, month_start, month_end)

    print("  Fetching working days...")
    working_days_data = get_total_working_days(client, month_start, month_end)

    print("  Computing incentives...")
    results = compute_incentives(footage_data, labor_data, working_days_data)

    print_summary(results)
    write_excel(results, footage_data, month_label, output_path)


if __name__ == "__main__":
    main()
