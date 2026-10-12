#!/usr/bin/env python3
"""
schedule_dummy_data.py
======================
Dummy-data generator + tiny FastAPI router for the SAMARTH "Production Schedule"
screens (Machine Schedule / Planned Schedule).

The field names follow your existing SQLAlchemy models so the dummy data can later be
replaced by real queries without touching the React code:

  machines            <- Equipments        (machine_id, eqpt_no, eqpt_name, eqpt_make, eqpt_model, stage_id)
  process_mode        <- Processtype       (process_type_batch -> "BATCH" / "SERIES")
  part_number / desc  <- ProductTypeMaster (part_number, description)
  operation_*         <- ProcessFlowMaster / ProductWorkflowMapping (flow_step_id, step_name, step_sequence)
  production_order    <- PcbData.productionOrder
  holidays            <- PublicHoliday     (holiday_date, name)
  shifts              <- StageCapacityDB   (hours_per_shift, shifts_per_day)
  operator_name       <- OperatorStepMapping

How the schedule is produced
----------------------------
A simple forward, finite-capacity scheduler:
  * every production order is a chain of operations (routing) that must run in sequence
  * each operation is placed on the eligible machine that can finish it earliest
  * time is only consumed inside the working window (two shifts) and on working days
    (Sundays and public holidays are skipped)
  * SERIES  operation duration = qty x minutes_per_unit
    BATCH   operation duration = ceil(qty / batch_size) x cycle_minutes
  * the PLAN is produced first (release order); the CURRENT machine schedule is a second
    run where a few orders were re-prioritised by the planner, so the two tabs differ.

Usage
-----
  python schedule_dummy_data.py                      # writes schedule_dummy.json
  python schedule_dummy_data.py --json out.json --seed 7
  python schedule_dummy_data.py --serve --port 2000  # stand-alone API (CORS enabled)

  # or plug into your existing Data_Server.py:
  #   from schedule_dummy_data import router as schedule_router
  #   app.include_router(schedule_router)
"""
import argparse
import json
import math
import random
from datetime import datetime, timedelta

# --------------------------------------------------------------------------------------
# Configuration (everything the front-end needs to draw the axis comes from here too)
# --------------------------------------------------------------------------------------
SHIFT_START_HOUR = 6          # 06:00
SHIFT_END_HOUR = 20           # 20:00  -> 14 working hours / day
SHIFTS = [
    {"name": "Shift 1", "start": "06:00", "end": "13:00"},
    {"name": "Shift 2", "start": "13:00", "end": "20:00"},
]
WEEK_OFF_JS = [0]             # JS convention: 0 = Sunday  (python weekday(): Sunday = 6)
ISO = "%Y-%m-%dT%H:%M:%S"

# --------------------------------------------------------------------------------------
# Master data
# --------------------------------------------------------------------------------------
# (machine_id, work_centre, machine_name, resource_type, make, model)
MACHINES = [
    ("AIQA-01", "AIQA", "AIQA NDT machine",        "MACHINE", "Olympus",   "OmniScan X3"),
    ("AMVB-01", "AMVB", "AMVB machine",            "MACHINE", "Vacuum Co", "VB-200"),
    ("ASSY-01", "ASSY", "Assy - post bonding",     "MACHINE", "BEL",       "PB-100"),
    ("ASSY-02", "ASSY", "Post bonding activity",   "MACHINE", "BEL",       "PB-110"),
    ("ATCL-01", "ATCL", "Auto clave machine",      "MACHINE", "Scholz",    "AC-5000"),
    ("ATFN-01", "ATFN", "ATFN testing",            "MACHINE", "Keysight",  "E5071C"),
    ("MFMT1-01", "MFMT1", "MEMT1 machine",         "MACHINE", "Heller",    "1809 MK5"),
    ("MFMT1-02", "MFMT1", "Reflow oven",           "MACHINE", "Heller",    "1913 MK5"),
    ("MFMT2-01", "MFMT2", "MEMT2 machine",         "MACHINE", "Koh Young", "Zenith"),
    ("PLCC-01", "PLCC", "PLCC machine make",       "MACHINE", "Nordson",   "Select Coat"),
    ("PLCC-02", "PLCC", "Curing oven",             "MACHINE", "Binder",    "FD 115"),
    ("SMT1-01", "SMT1", "SMT Line 1",              "MACHINE", "Fuji",      "NXT III"),
    ("SMT2-01", "SMT2", "SMT Line 2",              "MACHINE", "Fuji",      "NXT III"),
    ("MANL-01", "MANL", "Manual inspection bench", "MANUAL",  "-",         "-"),
    ("MANL-02", "MANL", "Manual rework bench",     "MANUAL",  "-",         "-"),
]

OPERATORS = ["Anita Rao", "Rahul Nair", "Sunil Kumar", "Priya Shetty", "Imran Khan", "Divya Menon"]

# Routings: (operation_no, operation_name, eligible work centres, mode, minutes_per_unit,
#            batch_size, cycle_minutes)
PRODUCTS = {
    "112190024139": {
        "description": "PR SEALING ASSEMBLY",
        "routing": [
            (10, "PR SEALING",      ["ASSY"],          "SERIES", 6.0, 1, 0),
            (20, "POST BONDING",    ["ASSY"],          "SERIES", 5.0, 1, 0),
            (30, "AUTO CLAVE CURE", ["ATCL"],          "BATCH",  0.0, 20, 150),
            (40, "NDT INSPECTION",  ["AIQA"],          "SERIES", 3.0, 1, 0),
            (50, "FINAL TEST",      ["ATFN"],          "SERIES", 4.0, 1, 0),
        ],
    },
    "112190024140": {
        "description": "RF MODULE PCB ASSY",
        "routing": [
            (10, "SMT PRINT & PLACE", ["SMT1", "SMT2"], "SERIES", 3.5, 1, 0),
            (20, "REFLOW",            ["MFMT1"],        "BATCH",  0.0, 12, 55),
            (30, "AOI INSPECTION",    ["MFMT2"],        "SERIES", 2.5, 1, 0),
            (40, "MANUAL TOUCH-UP",   ["MANL"],         "SERIES", 4.0, 1, 0),
            (50, "CONFORMAL COAT",    ["PLCC"],         "SERIES", 2.0, 1, 0),
            (60, "CURING",            ["PLCC"],         "BATCH",  0.0, 25, 90),
        ],
    },
    "112190024141": {
        "description": "POWER SUPPLY CARD",
        "routing": [
            (10, "SMT PRINT & PLACE", ["SMT1", "SMT2"], "SERIES", 4.5, 1, 0),
            (20, "REFLOW",            ["MFMT1"],        "BATCH",  0.0, 10, 60),
            (30, "VISUAL INSPECTION", ["MANL"],         "SERIES", 3.0, 1, 0),
            (40, "FUNCTIONAL TEST",   ["ATFN"],         "SERIES", 5.0, 1, 0),
        ],
    },
    "112190024142": {
        "description": "ANTENNA FEED ASSEMBLY",
        "routing": [
            (10, "BONDING",         ["ASSY"],   "SERIES", 7.0, 1, 0),
            (20, "VACUUM BAGGING",  ["AMVB"],   "BATCH",  0.0, 8, 70),
            (30, "AUTO CLAVE CURE", ["ATCL"],   "BATCH",  0.0, 20, 150),
            (40, "NDT INSPECTION",  ["AIQA"],   "SERIES", 3.5, 1, 0),
            (50, "REWORK / TRIM",   ["MANL"],   "SERIES", 2.0, 1, 0),
        ],
    },
}

# (production_order, part_number, quantity, release offset in hours from "today 06:00")
PRODUCTION_ORDERS = [
    ("83086577", "112190024139", 24, -96),
    ("83086578", "112190024140", 40, -90),
    ("83086579", "112190024141", 30, -72),
    ("83086580", "112190024142", 16, -60),
    ("83086581", "112190024140", 60, -36),
    ("83086582", "112190024139", 32, -24),
    ("83086583", "112190024141", 45, -12),
    ("83086584", "112190024142", 20, 0),
    ("83086585", "112190024140", 50, 12),
    ("83086586", "112190024141", 80, 24),
    ("83086587", "112190024139", 48, 36),
    ("83086588", "112190024142", 36, 48),
    ("83086589", "112190024140", 90, 60),
    ("83086590", "112190024141", 70, 84),
    ("83086591", "112190024139", 60, 96),
    ("83086592", "112190024142", 40, 120),
    ("83086593", "112190024140", 100, 144),
]


# --------------------------------------------------------------------------------------
# Working-time helpers
# --------------------------------------------------------------------------------------
class Calendar:
    def __init__(self, holidays):
        self.holidays = {h["holiday_date"] for h in holidays}

    def is_working_day(self, d):
        # python: Monday=0 ... Sunday=6
        if d.weekday() == 6:
            return False
        return d.strftime("%Y-%m-%d") not in self.holidays

    def window(self, d):
        base = datetime(d.year, d.month, d.day)
        return base + timedelta(hours=SHIFT_START_HOUR), base + timedelta(hours=SHIFT_END_HOUR)

    def next_working_start(self, dt):
        """Earliest moment >= dt that lies inside a working window."""
        while True:
            if not self.is_working_day(dt):
                dt = datetime(dt.year, dt.month, dt.day) + timedelta(days=1, hours=SHIFT_START_HOUR)
                continue
            ws, we = self.window(dt)
            if dt < ws:
                return ws
            if dt >= we:
                dt = datetime(dt.year, dt.month, dt.day) + timedelta(days=1)
                continue
            return dt

    def add_working_minutes(self, start, minutes):
        cur = self.next_working_start(start)
        remaining = float(minutes)
        while True:
            _, we = self.window(cur)
            avail = (we - cur).total_seconds() / 60.0
            if remaining <= avail:
                return cur + timedelta(minutes=remaining)
            remaining -= avail
            cur = self.next_working_start(datetime(cur.year, cur.month, cur.day) + timedelta(days=1))


# --------------------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------------------
def op_duration_minutes(step, qty):
    _, _, _, mode, per_unit, batch_size, cycle = step
    if mode == "BATCH":
        return math.ceil(qty / batch_size) * cycle
    return qty * per_unit


def run_scheduler(cal, machines, orders, order_sequence):
    """Return {op_id: {...}} for the given PO ordering."""
    by_wc = {}
    for m in machines:
        by_wc.setdefault(m["work_centre"], []).append(m)
    free_at = {m["machine_id"]: datetime.min for m in machines}
    result = {}

    for po_no in order_sequence:
        po = orders[po_no]
        prev_end = po["release"]
        for step in PRODUCTS[po["part_number"]]["routing"]:
            op_no, _, work_centres, *_ = step
            minutes = op_duration_minutes(step, po["quantity"])
            best = None
            for wc in work_centres:
                for m in by_wc[wc]:
                    s = cal.next_working_start(max(prev_end, free_at[m["machine_id"]]))
                    e = cal.add_working_minutes(s, minutes)
                    if best is None or e < best[2]:
                        best = (m, s, e)
            m, s, e = best
            free_at[m["machine_id"]] = e
            prev_end = e
            result[f"{po_no}-{op_no}"] = {"machine": m, "start": s, "end": e}
    return result


# --------------------------------------------------------------------------------------
# Dataset builder
# --------------------------------------------------------------------------------------
def build_dataset(seed=42, now=None):
    rnd = random.Random(seed)
    now = now or datetime.now().replace(microsecond=0)
    today = datetime(now.year, now.month, now.day)

    holidays = [
        {"holiday_date": (today + timedelta(days=6)).strftime("%Y-%m-%d"), "name": "Dummy Public Holiday"},
        {"holiday_date": (today + timedelta(days=20)).strftime("%Y-%m-%d"), "name": "Dummy Festival Holiday"},
    ]
    for h in holidays:
        h["year"] = int(h["holiday_date"][:4])
    cal = Calendar(holidays)

    machines = [
        {
            "machine_id": mid, "work_centre": wc, "machine_name": name, "resource_type": rtype,
            "eqpt_no": f"EQ-{i + 1:03d}", "eqpt_name": name, "eqpt_make": make, "eqpt_model": model,
            "stage_id": (i % 6) + 1,
        }
        for i, (mid, wc, name, rtype, make, model) in enumerate(MACHINES)
    ]

    orders = {}
    for po_no, part, qty, rel_h in PRODUCTION_ORDERS:
        release = today + timedelta(hours=SHIFT_START_HOUR + rel_h)
        orders[po_no] = {"production_order": po_no, "part_number": part, "quantity": qty, "release": release}

    seq_plan = [p[0] for p in PRODUCTION_ORDERS]
    # planner re-prioritised a few orders -> current schedule differs from plan
    seq_now = seq_plan[:3] + [seq_plan[5], seq_plan[4]] + [seq_plan[3]] + seq_plan[6:]

    plan = run_scheduler(cal, machines, orders, seq_plan)
    cur = run_scheduler(cal, machines, orders, seq_now)

    operations = []
    pbatch = {pn: {st[0]: st for st in p["routing"]} for pn, p in PRODUCTS.items()}
    for op_id, c in cur.items():
        po_no, op_no = op_id.rsplit("-", 1)
        op_no = int(op_no)
        po = orders[po_no]
        step = pbatch[po["part_number"]][op_no]
        p = plan[op_id]
        qty = po["quantity"]

        if c["end"] <= now:
            status, done = "COMPLETED", qty
        elif c["start"] <= now:
            frac = (now - c["start"]).total_seconds() / max((c["end"] - c["start"]).total_seconds(), 1)
            status, done = "IN_PROGRESS", max(1, int(qty * frac))
        else:
            status, done = "SCHEDULED", 0

        operations.append({
            "op_id": op_id,
            "production_order": po_no,
            "part_number": po["part_number"],
            "part_description": PRODUCTS[po["part_number"]]["description"],
            "operation_no": op_no,
            "flow_step_id": op_no // 10,
            "step_name": step[1],
            "operation_name": step[1],
            "process_mode": step[3],                      # BATCH / SERIES  (Processtype)
            "quantity": qty,
            "completed_qty": done,
            "duration_hours": round(op_duration_minutes(step, qty) / 60.0, 2),   # working hours
            "status": status,
            "operator_name": rnd.choice(OPERATORS),
            # current machine schedule
            "machine_id": c["machine"]["machine_id"],
            "start_time": c["start"].strftime(ISO),
            "end_time": c["end"].strftime(ISO),
            # plan
            "planned_machine_id": p["machine"]["machine_id"],
            "planned_start": p["start"].strftime(ISO),
            "planned_end": p["end"].strftime(ISO),
        })
    operations.sort(key=lambda o: o["start_time"])

    return {
        "generated_at": now.strftime(ISO),
        "calendar": {
            "shift_start": SHIFT_START_HOUR,
            "shift_end": SHIFT_END_HOUR,
            "shifts": SHIFTS,
            "week_off": WEEK_OFF_JS,
            "holidays": holidays,
        },
        "machines": machines,
        "production_orders": [
            {
                "production_order": po_no,
                "part_number": o["part_number"],
                "description": PRODUCTS[o["part_number"]]["description"],
                "quantity": o["quantity"],
                "release_date": o["release"].strftime(ISO),
            }
            for po_no, o in orders.items()
        ],
        "operations": operations,
    }


# --------------------------------------------------------------------------------------
# FastAPI router  (GET /schedule/data, POST /schedule/update)
# --------------------------------------------------------------------------------------
try:
    from fastapi import APIRouter, FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    from typing import List, Optional

    router = APIRouter(prefix="/schedule", tags=["schedule"])
    _STATE = {"data": None}

    class OpChange(BaseModel):
        op_id: str
        machine_id: Optional[str] = None
        start_time: Optional[str] = None
        end_time: Optional[str] = None

    class UpdatePayload(BaseModel):
        changes: List[OpChange]

    def _data(regenerate=False):
        if regenerate or _STATE["data"] is None:
            _STATE["data"] = build_dataset()
        return _STATE["data"]

    @router.get("/data")
    def get_schedule_data(regenerate: bool = False):
        """Everything both schedule tabs need in a single call."""
        return _data(regenerate)

    @router.post("/update")
    def update_schedule(payload: UpdatePayload):
        """Receives manual overrides from the Machine Schedule 'Update' button (kept in memory)."""
        ops = {o["op_id"]: o for o in _data()["operations"]}
        updated = 0
        for ch in payload.changes:
            op = ops.get(ch.op_id)
            if not op:
                continue
            for field in ("machine_id", "start_time", "end_time"):
                val = getattr(ch, field)
                if val:
                    op[field] = val
            updated += 1
        return {"updated": updated}

    def create_app():
        app = FastAPI(title="Schedule dummy API")
        app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"])
        app.include_router(router)
        return app
except ImportError:  # fastapi not installed -> JSON export still works
    router = None
    create_app = None


# --------------------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", default="schedule_dummy.json", help="output file (default schedule_dummy.json)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--serve", action="store_true", help="run a stand-alone API instead of writing a file")
    ap.add_argument("--port", type=int, default=2000)
    args = ap.parse_args()

    if args.serve:
        import uvicorn
        uvicorn.run(create_app(), host="0.0.0.0", port=args.port)
    else:
        ds = build_dataset(seed=args.seed)
        with open(args.json, "w") as f:
            json.dump(ds, f, indent=2)
        print(f"wrote {args.json}: {len(ds['machines'])} machines, "
              f"{len(ds['production_orders'])} production orders, {len(ds['operations'])} operations")
