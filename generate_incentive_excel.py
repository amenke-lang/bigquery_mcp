#!/usr/bin/env python3
"""
Generate the Drilling Performance Incentive workbook with full formula traceability.

Pulls raw data from BigQuery, writes it to Excel sheets, then builds
calculation and summary sheets using Excel formulas only.

Usage:
    python generate_incentive_excel.py [--month YYYY-MM] [--output FILE]
"""

import argparse
import re
import sys
from datetime import date

from google.cloud import bigquery
from google.oauth2 import service_account

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
    from openpyxl.utils import get_column_letter
except ImportError:
    print("ERROR: openpyxl required. Install with: pip install openpyxl")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CREDENTIALS_PATH = r"C:\Users\Alexander Menke\Desktop\bigquery_mcp\gcp-credentials.json"
PROJECT_ID = "dandelion-tf-production"


def get_client() -> bigquery.Client:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=["https://www.googleapis.com/auth/bigquery"],
    )
    return bigquery.Client(project=PROJECT_ID, credentials=creds)


# ---------------------------------------------------------------------------
# Data queries
# ---------------------------------------------------------------------------


def fetch_footage_detail(client, month_start, month_end) -> list[dict]:
    """Bore records where either actual_finish or qa_date falls in the month."""
    query = """
    SELECT
        job_unit_unique_id_ as bore_id,
        primary as project_name,
        homesite,
        bore_depth as planned_depth,
        actual_bore_depth,
        COALESCE(actual_bore_depth, bore_depth) as depth,
        status,
        qa_status,
        actual_start,
        actual_finish,
        qa_date,
        task_resource as rig,
        -- Qualify for incentive: Complete + QA passed + QA date in month + has depth
        CASE
          WHEN status = 'Complete'
            AND qa_status IN ('Pass 1', 'Pass 2')
            AND qa_date >= @month_start AND qa_date < @month_end
            AND COALESCE(actual_bore_depth, bore_depth) IS NOT NULL
            AND COALESCE(actual_bore_depth, bore_depth) > 0
          THEN TRUE ELSE FALSE
        END as qualifies
    FROM reporting.drilling_master_data
    WHERE (
        (actual_finish >= @month_start AND actual_finish < @month_end)
        OR (qa_date >= @month_start AND qa_date < @month_end)
      )
    ORDER BY primary, COALESCE(qa_date, actual_finish), bore_id
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
            bigquery.ScalarQueryParameter("month_end", "DATE", month_end),
        ]
    )
    rows = []
    for row in client.query(query, job_config=job_config).result():
        d = dict(row)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v
        rows.append(d)
    return rows


def fetch_labor_detail(client, month_start, month_end) -> list[dict]:
    """Daily labor records for drilling department."""
    query = """
    SELECT
        first_name,
        last_name,
        job_name,
        task_type,
        timesheet_date,
        regular_hours,
        overtime_hours,
        pto_hours,
        holiday_hours,
        sick_hours,
        vacation_hours,
        lwp_hours,
        bereavement_hours,
        total_hours
    FROM dbt_dandelion_operations.fct__ops_timesheets
    WHERE timesheet_date >= @month_start AND timesheet_date < @month_end
      AND (department LIKE '%Drilling%' OR department LIKE '%Trenching%')
    ORDER BY last_name, first_name, timesheet_date
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("month_start", "DATE", month_start),
            bigquery.ScalarQueryParameter("month_end", "DATE", month_end),
        ]
    )
    rows = []
    for row in client.query(query, job_config=job_config).result():
        d = dict(row)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v
        rows.append(d)
    return rows


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------

HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
TITLE_FONT = Font(bold=True, size=14)
SECTION_FONT = Font(bold=True, size=11, color="2F5496")
BOLD = Font(bold=True)
MONEY_FMT = '$#,##0.00'
NUMBER_FMT = '#,##0.0'
PCT_FMT = '0.0%'
THIN_BORDER = Border(
    bottom=Side(style="thin", color="D9D9D9"),
)


def write_header_row(ws, row, headers):
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True)


def auto_width(ws, min_width=6, max_width=16):
    for col_cells in ws.columns:
        valid = [c for c in col_cells if not isinstance(c, openpyxl.cell.cell.MergedCell)]
        if not valid:
            continue
        # Size to data rows only (skip headers), so headers wrap
        data_cells = [c for c in valid if c.row > 1]
        if data_cells:
            length = max(len(str(c.value or "")) for c in data_cells)
        else:
            length = 6
        ws.column_dimensions[valid[0].column_letter].width = max(min_width, min(length + 1, max_width))


# ---------------------------------------------------------------------------
# Sheet builders
# ---------------------------------------------------------------------------


def build_parameters_sheet(wb, month_label, month_start, month_end):
    """Sheet with all program parameters — formulas reference these cells."""
    ws = wb.create_sheet("Parameters")

    ws["A1"] = "Drilling Performance Incentive Program — Parameters"
    ws["A1"].font = TITLE_FONT

    params = [
        ("Floor (ft/day)", 150, "B4"),
        ("OTE Target (ft/day)", 300, "B5"),
        ("Driller Monthly OTE ($)", 1000, "B6"),
        ("Helper Monthly OTE ($)", 500, "B7"),
        ("Working Days for OTE", 20, "B8"),
        ("Driller Daily OTE ($)", None, "B9"),  # formula
        ("Helper Daily OTE ($)", None, "B10"),  # formula
        ("Driller Seg1 Rate ($/ft above floor)", None, "B11"),  # formula
        ("Helper Seg1 Rate ($/ft above floor)", None, "B12"),  # formula
        ("Driller Seg2 Rate ($/ft above OTE)", 0.50, "B13"),
        ("Helper Seg2 Rate ($/ft above OTE)", 0.25, "B14"),
        ("Helper Crew Multiplier", 2, "B15"),
        ("Report Month", month_label, "B16"),
        ("Month Start", month_start, "B17"),
        ("Month End", month_end, "B18"),
    ]

    for i, (label, value, cell_ref) in enumerate(params):
        row = 4 + i
        ws.cell(row=row, column=1, value=label).font = BOLD
        if value is not None:
            ws.cell(row=row, column=2, value=value)

    # Formulas
    ws["B9"] = "=B6/B8"
    ws["B9"].number_format = MONEY_FMT
    ws["B10"] = "=B7/B8"
    ws["B10"].number_format = MONEY_FMT
    ws["B11"] = "=B9/(B5-B4)"
    ws["B11"].number_format = '$#,##0.0000'
    ws["B12"] = "=B10/(B5-B4)"
    ws["B12"].number_format = '$#,##0.0000'
    ws["B6"].number_format = MONEY_FMT
    ws["B7"].number_format = MONEY_FMT
    ws["B13"].number_format = '$#,##0.00'
    ws["B14"].number_format = '$#,##0.00'
    ws["B17"].number_format = 'YYYY-MM-DD'
    ws["B18"].number_format = 'YYYY-MM-DD'

    # Project name mapping
    ws.cell(row=21, column=1, value="Project Name Mapping (Timesheet → Footage)").font = SECTION_FONT
    ws.cell(row=22, column=1, value="Timesheet Name").font = BOLD
    ws.cell(row=22, column=2, value="Footage Name").font = BOLD
    mappings = [
        ("East Creek Farm", "East Creek"),
        ("Bell 10 Old Lane Callum", "Bell - 10 Old Lane (Callum)"),
    ]
    for i, (ts_name, ft_name) in enumerate(mappings, 23):
        ws.cell(row=i, column=1, value=ts_name)
        ws.cell(row=i, column=2, value=ft_name)

    auto_width(ws)
    return ws


def build_footage_sheet(wb, footage_data):
    """Raw bore-level footage data from BigQuery."""
    ws = wb.create_sheet("Footage Data")

    headers = ["Bore ID", "Project", "Homesite",
               "Planned Depth (ft)", "Actual Depth (ft)", "Depth Used (ft)",
               "Status", "QA Status", "Actual Start", "Actual Finish",
               "QA Date", "Rig", "Qualifies for Incentive"]
    write_header_row(ws, 1, headers)

    for i, row in enumerate(footage_data, 2):
        ws.cell(row=i, column=1, value=row["bore_id"])
        ws.cell(row=i, column=2, value=row["project_name"])
        ws.cell(row=i, column=3, value=row.get("homesite"))
        ws.cell(row=i, column=4, value=row.get("planned_depth"))
        ws.cell(row=i, column=5, value=row.get("actual_bore_depth"))
        ws.cell(row=i, column=6, value=row.get("depth"))
        ws.cell(row=i, column=7, value=row["status"])
        ws.cell(row=i, column=8, value=row["qa_status"])
        ws.cell(row=i, column=9, value=row.get("actual_start"))
        ws.cell(row=i, column=10, value=row.get("actual_finish"))
        ws.cell(row=i, column=11, value=row.get("qa_date"))
        ws.cell(row=i, column=12, value=row.get("rig"))
        # M: Qualifies = Complete + QA passed + QA date in month + depth > 0
        ws.cell(row=i, column=13,
                value=(
                    f'=IF(AND(G{i}="Complete",'
                    f'OR(H{i}="Pass 1",H{i}="Pass 2"),'
                    f'K{i}>=Parameters!$B$17,'
                    f'K{i}<Parameters!$B$18,'
                    f'F{i}>0),"Yes","No")'
                ))

    # Add totals
    last_row = len(footage_data) + 1
    total_row = last_row + 1
    ws.cell(row=total_row, column=3, value="ALL FOOTAGE").font = BOLD
    ws.cell(row=total_row, column=6,
            value=f"=SUM(F2:F{last_row})").font = BOLD
    ws.cell(row=total_row, column=6).number_format = NUMBER_FMT

    qual_row = total_row + 1
    ws.cell(row=qual_row, column=3, value="QUALIFYING FOOTAGE").font = BOLD
    ws.cell(row=qual_row, column=6,
            value=f'=SUMIFS(F2:F{last_row},M2:M{last_row},"Yes")').font = BOLD
    ws.cell(row=qual_row, column=6).number_format = NUMBER_FMT

    auto_width(ws)
    return ws, last_row


def build_footage_summary_sheet(wb, footage_data, footage_last_row):
    """Summarize footage by project using SUMIFS formulas."""
    ws = wb.create_sheet("Footage by Project")

    headers = ["Project Name (Footage)", "Total Footage (ft)", "Bore Count"]
    write_header_row(ws, 1, headers)

    # List all projects so the sheet updates dynamically if qualifying status changes
    projects = sorted(set(r["project_name"] for r in footage_data))
    for i, proj in enumerate(projects, 2):
        ws.cell(row=i, column=1, value=proj)
        # Only sum qualifying bores (Depth Used = col F, Qualifies = col M)
        ws.cell(row=i, column=2,
                value=f'=SUMIFS(\'Footage Data\'!F:F,\'Footage Data\'!B:B,A{i},\'Footage Data\'!M:M,"Yes")')
        ws.cell(row=i, column=2).number_format = NUMBER_FMT
        ws.cell(row=i, column=3,
                value=f'=COUNTIFS(\'Footage Data\'!B:B,A{i},\'Footage Data\'!M:M,"Yes")')

    total_row = len(projects) + 2
    ws.cell(row=total_row, column=1, value="TOTAL").font = BOLD
    ws.cell(row=total_row, column=2,
            value=f"=SUM(B2:B{total_row - 1})").font = BOLD
    ws.cell(row=total_row, column=2).number_format = NUMBER_FMT
    ws.cell(row=total_row, column=3,
            value=f"=SUM(C2:C{total_row - 1})").font = BOLD

    auto_width(ws)
    return ws, projects


def build_labor_sheet(wb, labor_data):
    """Raw daily labor data from BigQuery."""
    ws = wb.create_sheet("Labor Data")

    # Columns A-S (19 columns), then helper flags T-W
    headers = [
        "First Name", "Last Name", "Full Name",        # A B C
        "Job Name", "Mapped Project",                   # D E
        "Task Type", "Role",                            # F G
        "Date",                                         # H
        "Regular Hrs", "OT Hrs",                        # I J
        "PTO Hrs", "Holiday Hrs", "Sick Hrs",           # K L M
        "Vacation Hrs", "LWP Hrs", "Bereavement Hrs",   # N O P
        "Total Hrs",                                    # Q
        "Hour Type",                                    # R
        "Work Day",                                     # S  (1 if regular or OT > 0)
        "Unique Day", "Unique Proj Day", "Any Day",     # T U V
        "PTO/Vac Day", "Holiday Day",                   # W X
    ]
    write_header_row(ws, 1, headers)

    project_name_map = {
        "East Creek Farm": "East Creek",
        "Bell 10 Old Lane Callum": "Bell - 10 Old Lane (Callum)",
    }

    for i, row in enumerate(labor_data, 2):
        ws.cell(row=i, column=1, value=row["first_name"])       # A
        ws.cell(row=i, column=2, value=row["last_name"])        # B
        ws.cell(row=i, column=3, value=f"=A{i}&\" \"&B{i}")    # C

        job = row.get("job_name") or ""
        ws.cell(row=i, column=4, value=row.get("job_name"))     # D
        mapped = project_name_map.get(job, job)
        ws.cell(row=i, column=5, value=mapped if job else None) # E

        task = row.get("task_type")
        ws.cell(row=i, column=6, value=task)                    # F

        # Role classification
        if task == "Drilling":
            role = "Driller"
        elif task == "Driller Helper":
            role = "Helper"
        else:
            role = ""
        ws.cell(row=i, column=7, value=role)                    # G

        ws.cell(row=i, column=8, value=row.get("timesheet_date"))   # H
        ws.cell(row=i, column=9, value=row.get("regular_hours"))    # I
        ws.cell(row=i, column=10, value=row.get("overtime_hours"))  # J
        ws.cell(row=i, column=11, value=row.get("pto_hours"))       # K
        ws.cell(row=i, column=12, value=row.get("holiday_hours"))   # L
        ws.cell(row=i, column=13, value=row.get("sick_hours"))      # M
        ws.cell(row=i, column=14, value=row.get("vacation_hours"))  # N
        ws.cell(row=i, column=15, value=row.get("lwp_hours"))       # O
        ws.cell(row=i, column=16, value=row.get("bereavement_hours"))  # P
        ws.cell(row=i, column=17, value=row.get("total_hours"))     # Q

        # R: Hour Type — classify what kind of time this entry represents
        ws.cell(row=i, column=18,
                value=(
                    f'=IF(I{i}+J{i}>0,"Work",'
                    f'IF(L{i}>0,"Holiday",'
                    f'IF(K{i}>0,"PTO",'
                    f'IF(N{i}>0,"Vacation",'
                    f'IF(M{i}>0,"Sick",'
                    f'IF(O{i}>0,"LWP",'
                    f'IF(P{i}>0,"Bereavement","Other")))))))'
                ))

        # S: Work Day — 1 only if this entry has actual work hours
        ws.cell(row=i, column=19,
                value=f'=IF(I{i}+J{i}>0,1,0)')

        # T: Unique Day flag — first person+date on a real project with
        # a drilling role AND actual work hours.
        ws.cell(row=i, column=20,
                value=(
                    f'=IF(AND(G{i}<>"",E{i}<>"",S{i}=1),'
                    f'IF(COUNTIFS(C$2:C{i},C{i},H$2:H{i},H{i},G$2:G{i},"<>""",E$2:E{i},"<>""",S$2:S{i},1)=1,1,0),'
                    f'0)'
                ))

        # U: Unique Project Day flag — first person+date+project+role
        # combo with actual work hours. Used for splitting footage by days.
        ws.cell(row=i, column=21,
                value=(
                    f'=IF(AND(G{i}<>"",E{i}<>"",S{i}=1),'
                    f'IF(COUNTIFS(C$2:C{i},C{i},H$2:H{i},H{i},E$2:E{i},E{i},G$2:G{i},G{i},S$2:S{i},1)=1,1,0),'
                    f'0)'
                ))

        # V: Any Day flag — first person+date with actual work hours.
        # Used for total days worked count.
        ws.cell(row=i, column=22,
                value=(
                    f'=IF(S{i}=1,'
                    f'IF(COUNTIFS(C$2:C{i},C{i},H$2:H{i},H{i},S$2:S{i},1)=1,1,0),'
                    f'0)'
                ))

        # W: PTO/Vacation Day flag — 1 if first person+date with PTO or vacation hours.
        ws.cell(row=i, column=23,
                value=(
                    f'=IF(K{i}+N{i}>0,'
                    f'IF(COUNTIFS(C$2:C{i},C{i},H$2:H{i},H{i})=1,1,0),'
                    f'0)'
                ))

        # X: Holiday Day flag — 1 if first person+date with holiday hours.
        ws.cell(row=i, column=24,
                value=(
                    f'=IF(L{i}>0,'
                    f'IF(COUNTIFS(C$2:C{i},C{i},H$2:H{i},H{i})=1,1,0),'
                    f'0)'
                ))

    auto_width(ws)
    labor_last_row = len(labor_data) + 1
    return ws, labor_last_row


def build_project_hours_sheet(wb, labor_data, projects):
    """Per-person, per-project hours with role, using SUMIFS formulas."""
    ws = wb.create_sheet("Project Hours")

    headers = ["Name", "Project", "Role", "Days on Project",
               "Total Role Days on Project", "Share",
               "Project Footage", "Crew Multiplier", "Credited Footage"]
    write_header_row(ws, 1, headers)

    # Determine unique (person, project, role) combos from labor data
    project_name_map = {
        "East Creek Farm": "East Creek",
        "Bell 10 Old Lane Callum": "Bell - 10 Old Lane (Callum)",
    }

    combos = set()
    for row in labor_data:
        task = row.get("task_type")
        if task == "Drilling":
            role = "Driller"
        elif task == "Driller Helper":
            role = "Helper"
        else:
            continue
        job = row.get("job_name") or ""
        if not job or job == "Dandelion Internal Project":
            continue
        mapped = project_name_map.get(job, job)
        name = f"{row['first_name']} {row['last_name']}"
        combos.add((name, mapped, role))

    combos = sorted(combos, key=lambda x: (x[0], x[1]))

    for i, (name, project, role) in enumerate(combos, 2):
        ws.cell(row=i, column=1, value=name)
        ws.cell(row=i, column=2, value=project)
        ws.cell(row=i, column=3, value=role)

        # D: Days on project = SUMIFS on Unique Proj Day (name, project, role)
        ws.cell(row=i, column=4,
                value=f"=SUMIFS('Labor Data'!U:U,'Labor Data'!C:C,A{i},'Labor Data'!E:E,B{i},'Labor Data'!G:G,C{i})")
        ws.cell(row=i, column=4).number_format = '0'

        # E: Total role days on project = SUMIFS (project, role)
        ws.cell(row=i, column=5,
                value=f"=SUMIFS('Labor Data'!U:U,'Labor Data'!E:E,B{i},'Labor Data'!G:G,C{i})")
        ws.cell(row=i, column=5).number_format = '0'

        # F: Share = days / total role days
        ws.cell(row=i, column=6, value=f"=IF(E{i}>0,D{i}/E{i},0)")
        ws.cell(row=i, column=6).number_format = PCT_FMT

        # G: Project footage = VLOOKUP from Footage by Project
        ws.cell(row=i, column=7,
                value=f"=IFERROR(VLOOKUP(B{i},'Footage by Project'!A:B,2,FALSE),0)")
        ws.cell(row=i, column=7).number_format = NUMBER_FMT

        # H: Crew multiplier = IF Helper then Parameters!B15, else 1
        ws.cell(row=i, column=8,
                value=f"=IF(C{i}=\"Helper\",Parameters!$B$15,1)")

        # I: Credited footage = share * multiplier * project footage
        ws.cell(row=i, column=9, value=f"=F{i}*H{i}*G{i}")
        ws.cell(row=i, column=9).number_format = NUMBER_FMT

    auto_width(ws)
    return ws, combos


def build_summary_sheet(wb, labor_data, combos):
    """Final incentive summary with all Excel formulas."""
    ws = wb.active
    ws.title = "Incentive Summary"

    project_name_map = {
        "East Creek Farm": "East Creek",
        "Bell 10 Old Lane Callum": "Bell - 10 Old Lane (Callum)",
    }

    ws.merge_cells("A1:K1")
    ws["A1"] = "=CONCATENATE(\"Drilling Performance Incentive Report — \",Parameters!B16)"
    ws["A1"].font = TITLE_FONT

    headers = ["Name", "Primary Role",
               "Total Days Worked", "Days on Projects", "Days Not on Projects",
               "PTO/Vacation Days", "Holiday Days",
               "Total Credited Footage", "Ft / Day (on projects)",
               "Daily Rate ($)", "Monthly Payout ($)"]
    write_header_row(ws, 3, headers)

    # Get unique people who had Drilling or Driller Helper tasks on real projects
    people = set()
    for row in labor_data:
        task = row.get("task_type")
        if task not in ("Drilling", "Driller Helper"):
            continue
        job = row.get("job_name") or ""
        if not job or job == "Dandelion Internal Project":
            continue
        name = f"{row['first_name']} {row['last_name']}"
        people.add(name)
    people = sorted(people)

    ph_last_row = len(combos) + 1

    for i, name in enumerate(people, 4):
        r = i  # current row

        # A: Name
        ws.cell(row=r, column=1, value=name)

        # B: Primary role — based on which role has more days
        ws.cell(row=r, column=2,
                value=f'=IF(SUMIFS(\'Project Hours\'!D:D,\'Project Hours\'!A:A,A{r},\'Project Hours\'!C:C,"Driller")>=SUMIFS(\'Project Hours\'!D:D,\'Project Hours\'!A:A,A{r},\'Project Hours\'!C:C,"Helper"),"Driller","Helper")')

        # C: Total days worked (any work day in Labor Data for this person)
        ws.cell(row=r, column=3,
                value=f"=SUMIFS('Labor Data'!V:V,'Labor Data'!C:C,A{r})")

        # D: Days on projects (work days with a drilling role on a real project)
        ws.cell(row=r, column=4,
                value=f"=SUMIFS('Labor Data'!T:T,'Labor Data'!C:C,A{r})")

        # E: Days not on projects = Total - On Projects
        ws.cell(row=r, column=5, value=f"=C{r}-D{r}")

        # F: PTO/Vacation days
        ws.cell(row=r, column=6,
                value=f"=SUMIFS('Labor Data'!W:W,'Labor Data'!C:C,A{r})")

        # G: Holiday days
        ws.cell(row=r, column=7,
                value=f"=SUMIFS('Labor Data'!X:X,'Labor Data'!C:C,A{r})")

        # H: Total credited footage = SUM from Project Hours
        ws.cell(row=r, column=8,
                value=f"=SUMIFS('Project Hours'!I:I,'Project Hours'!A:A,A{r})")
        ws.cell(row=r, column=8).number_format = NUMBER_FMT

        # I: Ft / Day (on projects)
        ws.cell(row=r, column=9, value=f"=IF(D{r}>0,H{r}/D{r},0)")
        ws.cell(row=r, column=9).number_format = NUMBER_FMT

        # J: Daily Rate — piecewise formula
        # If < floor: 0
        # If floor <= F <= OTE: (F - floor) * seg1_rate
        # If F > OTE: daily_ote + (F - OTE) * seg2_rate
        ws.cell(row=r, column=10,
                value=(
                    f'=IF(I{r}<Parameters!$B$4,0,'
                    f'IF(B{r}="Driller",'
                    f'IF(I{r}<=Parameters!$B$5,'
                    f'(I{r}-Parameters!$B$4)*Parameters!$B$11,'
                    f'Parameters!$B$9+(I{r}-Parameters!$B$5)*Parameters!$B$13),'
                    f'IF(I{r}<=Parameters!$B$5,'
                    f'(I{r}-Parameters!$B$4)*Parameters!$B$12,'
                    f'Parameters!$B$10+(I{r}-Parameters!$B$5)*Parameters!$B$14)))'
                ))
        ws.cell(row=r, column=10).number_format = MONEY_FMT

        # K: Monthly Payout = Daily Rate * Days on Projects
        ws.cell(row=r, column=11, value=f"=J{r}*D{r}")
        ws.cell(row=r, column=11).number_format = MONEY_FMT

    # Totals
    last_person_row = len(people) + 3
    total_row = last_person_row + 1
    ws.cell(row=total_row, column=1, value="TOTAL").font = BOLD
    ws.cell(row=total_row, column=11,
            value=f"=SUM(K4:K{last_person_row})").font = BOLD
    ws.cell(row=total_row, column=11).number_format = MONEY_FMT
    ws.cell(row=total_row, column=8,
            value=f"=SUM(H4:H{last_person_row})").font = BOLD
    ws.cell(row=total_row, column=8).number_format = NUMBER_FMT

    auto_width(ws)
    return ws


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Generate Drilling Incentive Excel Workbook")
    parser.add_argument("--month", default="2026-01",
                        help="Month in YYYY-MM format")
    parser.add_argument("--output", default=None,
                        help="Output Excel file path")
    args = parser.parse_args()

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

    default_dir = r"C:\Users\Alexander Menke\Documents\Drilling Bonus"
    output_path = args.output or f"{default_dir}\\drilling_incentive_{year}_{month:02d}.xlsx"

    print(f"Generating incentive workbook for {month_label}...")

    client = get_client()

    print("  Fetching footage data...")
    footage_data = fetch_footage_detail(client, month_start, month_end)
    print(f"    {len(footage_data)} qualifying bores")

    print("  Fetching labor data...")
    labor_data = fetch_labor_detail(client, month_start, month_end)
    print(f"    {len(labor_data)} labor entries")

    print("  Building workbook...")
    wb = openpyxl.Workbook()

    # Build sheets in order
    # Pass date objects so Excel stores them as dates
    ms_date = date(year, month, 1)
    if month == 12:
        me_date = date(year + 1, 1, 1)
    else:
        me_date = date(year, month + 1, 1)
    params_ws = build_parameters_sheet(wb, month_label, ms_date, me_date)
    footage_ws, footage_last_row = build_footage_sheet(wb, footage_data)
    footage_summary_ws, projects = build_footage_summary_sheet(wb, footage_data, footage_last_row)
    labor_ws, labor_last_row = build_labor_sheet(wb, labor_data)
    project_hours_ws, combos = build_project_hours_sheet(wb, labor_data, projects)
    summary_ws = build_summary_sheet(wb, labor_data, combos)

    # Reorder sheets: Summary first
    sheet_order = ["Incentive Summary", "Parameters", "Project Hours",
                   "Footage by Project", "Footage Data", "Labor Data"]
    for idx, name in enumerate(sheet_order):
        wb.move_sheet(name, offset=idx - wb.sheetnames.index(name))

    wb.save(output_path)
    print(f"\nWorkbook saved to: {output_path}")
    print(f"\nSheets:")
    for name in wb.sheetnames:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
